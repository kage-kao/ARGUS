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
                print(f"[BuzzCast] search_user API error: code={data.get('code')}, message={data.get('message')}")
                return None
            users = data.get("data", [])
            if not users:
                print(f"[BuzzCast] search_user: no users found for '{user_id}'")
                return None
            
            target_id = str(user_id)
            # Find exact match - check multiple ID fields
            for user in users:
                user_id_str = str(user.get("userId", ""))
                user_id_long_str = str(user.get("userIdLong", ""))
                id_str = str(user.get("id", ""))
                
                if user_id_str == target_id or user_id_long_str == target_id or id_str == target_id:
                    print(f"[BuzzCast] Found user: {user.get('userNickName')} (ID: {user_id_str}), liveState={user.get('liveState', 0)}")
                    return user
            
            # Fallback: if we searched by exact ID and got results, first one is likely correct
            print(f"[BuzzCast] No exact match, returning first result: {users[0].get('userNickName')}")
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
    
    async def get_game_live_channels(self, session: aiohttp.ClientSession, page: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        """Get list of game live channels."""
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{BASE_URL}/api/token/live/info/getGameLiveList"
        payload = {
            "limit": limit,
            "page": page,
        }
        
        async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                return []
            room_data = data.get("data", {})
            return room_data.get("roomList", room_data.get("list", []))
    
    async def get_pk_live_channels(self, session: aiohttp.ClientSession, page: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        """Get list of PK (battle) live channels."""
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{BASE_URL}/api/token/live/info/getPKLiveList"
        payload = {
            "limit": limit,
            "page": page,
        }
        
        async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                return []
            room_data = data.get("data", {})
            return room_data.get("roomList", room_data.get("list", []))
    
    async def find_user_stream(self, session: aiohttp.ClientSession, user_id: str | int, max_pages: int = 15) -> dict[str, Any] | None:
        """Find stream for specific user. Returns stream info or None."""
        target_id = str(user_id)
        
        print(f"[BuzzCast] Searching for user {target_id} stream in up to {max_pages} pages...")
        
        # Try recommended channels first
        for page in range(1, max_pages + 1):
            channels = await self.get_live_channels(session, page=page)
            print(f"[BuzzCast] Recommend page {page}: found {len(channels)} live channels")
            
            for channel in channels:
                ch_user_id = str(channel.get("userId", ""))
                ch_user_id_long = str(channel.get("userIdLong", ""))
                if ch_user_id == target_id or ch_user_id_long == target_id:
                    print(f"[BuzzCast] Found stream in Recommend! liveId={channel.get('liveId')}, online={channel.get('onlineNum')}")
                    return channel
        
        # Try game live channels
        print(f"[BuzzCast] Not in recommend, trying Game Live...")
        for page in range(1, min(max_pages, 5) + 1):
            channels = await self.get_game_live_channels(session, page=page)
            print(f"[BuzzCast] Game page {page}: found {len(channels)} live channels")
            
            for channel in channels:
                ch_user_id = str(channel.get("userId", ""))
                ch_user_id_long = str(channel.get("userIdLong", ""))
                if ch_user_id == target_id or ch_user_id_long == target_id:
                    print(f"[BuzzCast] Found stream in Game Live! liveId={channel.get('liveId')}")
                    return channel
        
        # Try PK live channels
        print(f"[BuzzCast] Not in game live, trying PK Live...")
        for page in range(1, min(max_pages, 5) + 1):
            channels = await self.get_pk_live_channels(session, page=page)
            print(f"[BuzzCast] PK page {page}: found {len(channels)} live channels")
            
            for channel in channels:
                ch_user_id = str(channel.get("userId", ""))
                ch_user_id_long = str(channel.get("userIdLong", ""))
                if ch_user_id == target_id or ch_user_id_long == target_id:
                    print(f"[BuzzCast] Found stream in PK Live! liveId={channel.get('liveId')}")
                    return channel
        
        print(f"[BuzzCast] User {target_id} not found in any live channels")
        return None
    
    async def get_stream_url(self, session: aiohttp.ClientSession, user_id: str | int) -> tuple[str | None, dict[str, Any] | None]:
        """
        Get HLS stream URL for user.
        Returns (stream_url, stream_info) or (None, user_profile) if offline.
        """
        print(f"[BuzzCast] get_stream_url for user {user_id}")
        
        # First check if user exists and is online via search API
        user_profile = await self.search_user(session, user_id)
        
        if user_profile is None:
            print(f"[BuzzCast] User {user_id} not found via search API, trying direct listing search...")
            # Fallback: search directly in live listings
            stream_info = await self.find_user_stream(session, user_id)
            if stream_info:
                print(f"[BuzzCast] Found user {user_id} directly in live listings!")
                # Extract URL
                url = (
                    stream_info.get("hlsUrl") or
                    stream_info.get("playUrl") or
                    stream_info.get("videoUrl") or
                    stream_info.get("webRtcUrl") or
                    stream_info.get("flvUrl")
                )
                if url:
                    print(f"[BuzzCast] Stream URL found: {url[:80]}...")
                return url, stream_info
            else:
                print(f"[BuzzCast] User {user_id} not found anywhere")
                return None, None
        
        live_state = user_profile.get("liveState", 0)
        nick = user_profile.get("userNickName", "Unknown")
        print(f"[BuzzCast] User {nick} (ID: {user_id}) - liveState={live_state}")
        
        if live_state == 0:
            # User exists but claims to be offline, but let's check live listings anyway
            # Sometimes liveState is not updated immediately
            print(f"[BuzzCast] User {user_id} shows offline (liveState=0), checking live listings anyway...")
            stream_info = await self.find_user_stream(session, user_id)
            if stream_info:
                print(f"[BuzzCast] Found stream despite liveState=0!")
                url = (
                    stream_info.get("hlsUrl") or
                    stream_info.get("playUrl") or
                    stream_info.get("videoUrl") or
                    stream_info.get("webRtcUrl") or
                    stream_info.get("flvUrl")
                )
                if url:
                    print(f"[BuzzCast] Stream URL found: {url[:80]}...")
                    return url, stream_info
            
            print(f"[BuzzCast] User {user_id} is confirmed offline")
            return None, user_profile
        
        # User claims to be online, find their stream
        stream_info = await self.find_user_stream(session, user_id)
        if stream_info is None:
            # Live but not in listing (could be private or in different category)
            print(f"[BuzzCast] User {user_id} has liveState={live_state} but stream not found in listings")
            return None, user_profile
        
        # Priority: hlsUrl > playUrl > videoUrl > webRtcUrl > flvUrl
        url = (
            stream_info.get("hlsUrl") or
            stream_info.get("playUrl") or
            stream_info.get("videoUrl") or
            stream_info.get("webRtcUrl") or
            stream_info.get("flvUrl")
        )
        
        if url:
            print(f"[BuzzCast] Stream URL found: {url[:80]}...")
        else:
            print(f"[BuzzCast] Stream found but no valid URL in response")
        
        return url, stream_info
