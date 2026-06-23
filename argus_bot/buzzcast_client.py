"""BuzzCast API client for anonymous tourist access."""
from __future__ import annotations
import aiohttp
import base64
import json
import random
import time
import uuid
from typing import Any


BASE_URL = "https://api.buzzcast.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class BuzzCastClient:
    """Anonymous tourist client for BuzzCast API."""
    
    def __init__(self):
        self.device_id = uuid.uuid4().hex
        self.tourist_vid: int | None = None
    
    def _api_common(self) -> str:
        """Generate api_common header (base64 encoded JSON)."""
        params = {
            "language": "en",
            "platform": "pwa",
            "unixTime": int(time.time() * 1000),
            "nonce": random.randint(100000, 999999),
            "versionCode": "3145",
            "device": "Chrome",
            "deviceType": "mobile",
            "pwaPlat": 1,
            "deviceId": f"pwa_{self.device_id}",
            "appGeneratedId": f"pwa_{self.device_id}",
            "vid": self.tourist_vid or 0,
        }
        json_str = json.dumps(params, separators=(",", ":"))
        return base64.b64encode(json_str.encode()).decode()
    
    def _headers(self) -> dict[str, str]:
        """Common headers for API requests."""
        return {
            "Content-Type": "application/json",
            "Origin": "https://www.buzzcast.com",
            "Referer": "https://www.buzzcast.com/",
            "User-Agent": UA,
            "api_common": self._api_common(),
            "systoken": "",
        }
    
    async def init_tourist(self, session: aiohttp.ClientSession) -> int:
        """Initialize tourist session and return tourist vid."""
        url = f"{BASE_URL}/api/user/pwa/event"
        payload = {"webId": self.device_id}
        
        async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                raise RuntimeError(f"Tourist init failed: {data}")
            self.tourist_vid = data["data"]["id"]
            return self.tourist_vid
    
    async def search_user(self, session: aiohttp.ClientSession, user_id: str | int) -> dict[str, Any] | None:
        """Search user by ID or nickname. Returns user profile or None."""
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{BASE_URL}/api/token/social/search/findUserByNickNameOrId"
        payload = {"content": str(user_id)}
        
        async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                return None
            users = data.get("data", [])
            if not users:
                return None
            # Find exact match
            for user in users:
                if str(user.get("userId")) == str(user_id):
                    return user
            # Fallback to first result
            return users[0] if users else None
    
    async def get_live_channels(self, session: aiohttp.ClientSession, page: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        """Get list of live channels (recommend feed)."""
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{BASE_URL}/api/token/live/info/getChannelListByRecommend"
        payload = {
            "limit": limit,
            "page": page,
            "liveType": 0,
            "sex": 0,
            "countryId": 0,
            "type": 0,
        }
        
        async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                return []
            room_data = data.get("data", {})
            return room_data.get("roomList", room_data.get("list", []))
    
    async def find_user_stream(self, session: aiohttp.ClientSession, user_id: str | int, max_pages: int = 5) -> dict[str, Any] | None:
        """Find stream for specific user. Returns stream info or None."""
        target_id = str(user_id)
        
        for page in range(1, max_pages + 1):
            channels = await self.get_live_channels(session, page=page)
            for channel in channels:
                ch_user_id = str(channel.get("userId", ""))
                ch_user_id_long = str(channel.get("userIdLong", ""))
                if ch_user_id == target_id or ch_user_id_long == target_id:
                    return channel
        
        return None
    
    async def get_stream_url(self, session: aiohttp.ClientSession, user_id: str | int) -> tuple[str | None, dict[str, Any] | None]:
        """
        Get HLS stream URL for user.
        Returns (stream_url, stream_info) or (None, user_profile) if offline.
        """
        # First check if user exists and is online
        user_profile = await self.search_user(session, user_id)
        if user_profile is None:
            return None, None
        
        live_state = user_profile.get("liveState", 0)
        if live_state == 0:
            # User is offline
            return None, user_profile
        
        # User is online, find their stream
        stream_info = await self.find_user_stream(session, user_id)
        if stream_info is None:
            # Live but not in listing (shouldn't happen)
            return None, user_profile
        
        # Priority: hlsUrl > playUrl > videoUrl > webRtcUrl > flvUrl
        url = (
            stream_info.get("hlsUrl") or
            stream_info.get("playUrl") or
            stream_info.get("videoUrl") or
            stream_info.get("webRtcUrl") or
            stream_info.get("flvUrl")
        )
        
        return url, stream_info
