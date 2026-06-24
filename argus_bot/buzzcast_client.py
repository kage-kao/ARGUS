"""BuzzCast (FaceCast) anonymous tourist client.

Implements the authoritative web-PWA guest flow reverse-engineered from
BuzzCast 3.2.75 (see BUZZCAST FULL DOC / REPORT.md):

1. Tourist token  : POST  https://api.buzzcast.com/api/user/pwa/event   {"webId": "pwa_<did>"}
2. Is-live + liveId: GET   https://api.buzzcast.com/api/token/live/info/getLiveByUserId?userId=<id>
3. Stream URL      : POST  https://dhcxzil.buzzcast.com/faceshow/tokens/live/broadcast/detail

Authoritative live verdict: result.status == 1 AND result.live != null.
Stream URLs live in result.live.{flvUrl,hlsUrl,rtmpUrl,webrtcUrl}.

Every request carries the url-encoded `api_common` header and an empty
`systoken` (guest). SOCKS5 proxies are rotated on HTTP 429 to avoid the
"Tourist init failed: {'code': 429}" rate limit.
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import random
import time
import urllib.parse
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector

from .proxy_manager import get_proxy_manager

API_URL = "https://api.buzzcast.com"
FACE_URL = "https://dhcxzil.buzzcast.com/faceshow"
UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"


class BuzzCastClient:
    """Anonymous tourist client with SOCKS5 proxy rotation.

    Optional env overrides (avoid re-init / rate limits):
      - DID : device id (localStorage._did)
      - VID : tourist token id (localStorage.touristToken)
    """

    def __init__(self) -> None:
        self.device_id = os.environ.get("DID", "378f9de3-0b0a-4e6d-8969-890939d9d5b6").strip()
        vid_env = os.environ.get("VID", "").strip()
        self.tourist_vid: int | None = int(vid_env) if vid_env.isdigit() else None
        self.proxy_manager = get_proxy_manager()
        self.current_proxy: str | None = None
        self._vid_lock = asyncio.Lock()
        self.max_init_retries = int(os.environ.get("BUZZCAST_INIT_RETRIES", "15"))
        self.max_call_retries = int(os.environ.get("BUZZCAST_CALL_RETRIES", "12"))

    # ---- signing / headers --------------------------------------------------
    def _api_common(self, vid: Any) -> str:
        params = {
            "language": "en",
            "platform": "pwa",
            "unixTime": int(time.time() * 1000),
            "nonce": random.randint(100000, 999999),
            "versionCode": "3145",
            "vid": vid,
            "device": "Chrome",
            "deviceType": "mobile",
            "pwaPlat": 1,
            "deviceId": f"pwa_{self.device_id}",
            "appGeneratedId": f"pwa_{self.device_id}",
        }
        b64 = base64.b64encode(json.dumps(params, separators=(",", ":")).encode()).decode()
        return urllib.parse.quote(b64, safe="")

    def _headers(self, vid: Any) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.buzzcast.com",
            "Referer": "https://www.buzzcast.com/",
            "User-Agent": UA,
            "api_common": self._api_common(vid),
            "systoken": "",
        }

    # ---- low-level request --------------------------------------------------
    async def _request(self, method: str, url: str, *, params: dict | None = None,
                       json_body: dict | None = None, vid: Any = "",
                       proxy: str | None = None, timeout: int = 25) -> dict[str, Any]:
        connector = ProxyConnector.from_url(proxy) if proxy else aiohttp.TCPConnector()
        to = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(connector=connector, timeout=to) as s:
            async with s.request(method, url, params=params, json=json_body,
                                 headers=self._headers(vid)) as r:
                return await r.json(content_type=None)

    def _next_proxy(self) -> str | None:
        return self.proxy_manager.get_random_proxy()

    async def _call(self, method: str, url: str, *, params: dict | None = None,
                    json_body: dict | None = None) -> dict[str, Any]:
        """Make an authenticated tourist call, rotating proxies on 429/errors."""
        vid = await self._ensure_vid()
        last: Any = None
        refreshed = False
        for attempt in range(self.max_call_retries):
            proxy = self.current_proxy if attempt == 0 else self._next_proxy()
            try:
                data = await self._request(method, url, params=params,
                                           json_body=json_body, vid=vid, proxy=proxy)
            except Exception as e:  # noqa: BLE001
                last = e
                if proxy:
                    self.proxy_manager.mark_failed(proxy)
                self.current_proxy = None
                await asyncio.sleep(0.2)
                continue

            code = data.get("code")
            if code in (429, 105, 100001):  # rate-limited / captcha / verify timeout
                last = data
                if proxy:
                    self.proxy_manager.mark_failed(proxy)
                self.current_proxy = None
                if not refreshed and attempt >= 3:
                    # rotate to a brand-new tourist token (fresh vid + IP)
                    vid = await self._refresh_vid()
                    refreshed = True
                await asyncio.sleep(0.2)
                continue

            if proxy:
                self.proxy_manager.mark_success(proxy)
                self.current_proxy = proxy
            return data

        raise RuntimeError(f"BuzzCast request failed after retries: {last}")

    # ---- tourist token ------------------------------------------------------
    async def _ensure_vid(self) -> int:
        if self.tourist_vid is not None:
            return self.tourist_vid
        return await self._refresh_vid()

    async def _refresh_vid(self) -> int:
        async with self._vid_lock:
            await self.proxy_manager.ensure_loaded()
            url = f"{API_URL}/api/user/pwa/event"
            body = {"webId": f"pwa_{self.device_id}"}
            last: Any = None
            for attempt in range(self.max_init_retries):
                # attempt 0 = direct (fast, usually fine); then rotate proxies
                proxy = None if attempt == 0 else self._next_proxy()
                try:
                    data = await self._request("POST", url, json_body=body, vid="",
                                               proxy=proxy, timeout=(15 if proxy is None else 10))
                except Exception as e:  # noqa: BLE001
                    last = e
                    if proxy:
                        self.proxy_manager.mark_failed(proxy)
                    await asyncio.sleep(0.2)
                    continue
                code = data.get("code")
                if code != 1 or not data.get("data"):
                    last = data
                    if proxy:
                        self.proxy_manager.mark_failed(proxy)
                    await asyncio.sleep(0.2)
                    continue
                self.tourist_vid = int(data["data"]["id"])
                if proxy:
                    self.proxy_manager.mark_success(proxy)
                    self.current_proxy = proxy
                print(f"[BuzzCast] ✅ tourist vid={self.tourist_vid} (proxy={'yes' if proxy else 'direct'})")
                return self.tourist_vid
            raise RuntimeError(
                f"Tourist init failed after retries: {last}. "
                f"Проверь доступность прокси-листа или задай VID/DID в .env"
            )

    # ---- public API (session arg kept for backwards-compat; unused) --------
    async def init_tourist(self, session: aiohttp.ClientSession | None = None) -> int:
        return await self._ensure_vid()

    async def is_live_and_liveid(self, session: aiohttp.ClientSession | None,
                                 user_id: str | int) -> int:
        """Step 2. Returns liveId (may be stale) or 0 if offline."""
        data = await self._call(
            "GET", f"{API_URL}/api/token/live/info/getLiveByUserId",
            params={"userId": str(user_id)},
        )
        if data.get("code") != 1:
            print(f"[BuzzCast] getLiveByUserId error: {data}")
            return 0
        d = data.get("data")
        try:
            return int(d) if d else 0
        except (TypeError, ValueError):
            return 0

    async def broadcast_detail(self, session: aiohttp.ClientSession | None,
                               live_id: int) -> dict[str, Any] | None:
        """Step 3. Authoritative live check; loops liveType 0..3."""
        last: dict[str, Any] | None = None
        for live_type in (0, 1, 2, 3):
            body = {"liveId": str(live_id), "liveType": live_type,
                    "startTime": "", "drmFlag": 0}
            try:
                data = await self._call(
                    "POST", f"{FACE_URL}/tokens/live/broadcast/detail", json_body=body)
            except Exception as e:  # noqa: BLE001
                print(f"[BuzzCast] broadcast_detail liveType={live_type} error: {e}")
                continue
            res = data.get("result") if "result" in data else data.get("data")
            if res and (res.get("status") == 1 or res.get("live")):
                print(f"[BuzzCast] ✅ active stream (liveType={live_type})")
                return res
            last = res
        return last

    @staticmethod
    def _pick_stream_url(live_info: dict[str, Any]) -> str | None:
        """Return a recordable URL, preferring FLV/HLS.

        BuzzCast's broadcast/detail often returns only `webrtcUrl` + `streamId`
        with flv/hls/rtmp null. The Tencent CDN serves the same stream over
        HTTP-FLV using the SAME txSecret query, so we derive it:
            webrtc://pull.../live/<id>?txSecret=..  ->  https://pull.../live/<id>.flv?txSecret=..
        """
        flv = live_info.get("flvUrl")
        if flv:
            return flv
        hls = live_info.get("hlsUrl")
        if hls:
            return hls

        signed = live_info.get("webrtcUrl") or live_info.get("rtmpUrl")
        stream_id = live_info.get("streamId")
        if signed and stream_id:
            s = urllib.parse.urlsplit(signed)
            netloc = s.netloc or "pull.buzzcast.com"
            return urllib.parse.urlunsplit(
                ("https", netloc, f"/live/{stream_id}.flv", s.query, ""))

        return live_info.get("rtmpUrl") or live_info.get("webrtcUrl")

    async def get_stream_url(self, session: aiohttp.ClientSession | None,
                             user_id: str | int) -> tuple[str | None, dict[str, Any] | None]:
        """Resolve (stream_url, stream_info). Offline → (None, {profile-stub})."""
        print(f"[BuzzCast] ===== get_stream_url({user_id}) =====")
        stub = {"userNickName": str(user_id), "userId": user_id, "infoName": str(user_id)}

        live_id = await self.is_live_and_liveid(session, user_id)
        if not live_id:
            print(f"[BuzzCast] ❌ {user_id} not live (liveId 0/null)")
            return None, stub

        print(f"[BuzzCast] getLiveByUserId -> liveId={live_id}")
        result = await self.broadcast_detail(session, live_id)
        if not result or result.get("status") != 1 or not result.get("live"):
            status = result.get("status") if result else "no-result"
            print(f"[BuzzCast] ❌ {user_id} NOT live (status={status}); finish.* URLs are stale")
            return None, stub

        live_info = result["live"]
        stream_url = self._pick_stream_url(live_info)
        anchor = live_info.get("anchor") or {}
        if anchor.get("nickName"):
            live_info.setdefault("userNickName", anchor["nickName"])
        if stream_url:
            print(f"[BuzzCast] ✅ {user_id} LIVE | {stream_url[:80]}...")
            return stream_url, live_info
        print("[BuzzCast] ⚠️ live but no stream URL in response")
        return None, live_info

    async def search_user(self, session: aiohttp.ClientSession | None,
                          user_id: str | int) -> dict[str, Any] | None:
        """Best-effort profile lookup. Returns a stub if the guest API is gated."""
        try:
            data = await self._call(
                "POST", f"{API_URL}/api/token/social/search/findUserByNickNameOrId",
                json_body={"content": str(user_id)},
            )
        except Exception:  # noqa: BLE001
            return {"userNickName": str(user_id), "userId": user_id, "liveState": 0}
        if data.get("code") == 1 and data.get("data"):
            users = data["data"]
            target = str(user_id).strip()
            for u in users:
                ids = {str(u.get(k, "")).strip()
                       for k in ("userId", "userIdLong", "id", "twinklingNumber")}
                if target in ids:
                    return u
            return users[0]
        # guest session usually can't search → graceful stub
        return {"userNickName": str(user_id), "userId": user_id, "liveState": 0}
