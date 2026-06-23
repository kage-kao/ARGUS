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
        import asyncio
        
        url = f"{BASE_URL}/api/user/pwa/event"
        payload = {"webId": self.device_id}
        
        # Retry with backoff on rate limit
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
                    data = await resp.json()
                    
                    # Handle rate limit
                    if data.get("code") == 429:
                        if attempt < max_retries - 1:
                            wait_time = 10 * (attempt + 1)
                            print(f"[BuzzCast] Rate limited, waiting {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise RuntimeError(f"Tourist init failed after retries: {data}")
                    
                    if data.get("code") != 1:
                        raise RuntimeError(f"Tourist init failed: {data}")
                    
                    self.tourist_vid = data["data"]["id"]
                    return self.tourist_vid
            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    print(f"[BuzzCast] Timeout, retrying...")
                    await asyncio.sleep(5)
                    continue
                raise
        
        raise RuntimeError("Failed to initialize tourist session")
    
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
            
            target_id = str(user_id).strip()
            print(f"[BuzzCast] search_user: searching for ID '{target_id}', got {len(users)} results")
            
            # Find exact match - check multiple ID fields with flexible matching
            for user in users:
                # Get all possible ID fields
                user_id_str = str(user.get("userId", "")).strip()
                user_id_long_str = str(user.get("userIdLong", "")).strip()
                id_str = str(user.get("id", "")).strip()
                twinkling_num = str(user.get("twinklingNumber", "")).strip()
                
                print(f"[BuzzCast] Checking user: nick={user.get('userNickName')}, userId={user_id_str}, userIdLong={user_id_long_str}, id={id_str}, twinklingNumber={twinkling_num}, liveState={user.get('liveState', 0)}")
                
                # Check all possible ID field matches
                if (target_id in [user_id_str, user_id_long_str, id_str, twinkling_num] and target_id):
                    print(f"[BuzzCast] ✅ Found exact match: {user.get('userNickName')} (userId: {user_id_str}), liveState={user.get('liveState', 0)}")
                    return user
            
            # Fallback: if we searched by exact ID and got results, first one is likely correct
            print(f"[BuzzCast] No exact match found, returning first result: {users[0].get('userNickName')} (userId: {users[0].get('userId')})")
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
            # Support both "roomList" and "list" field names
            return room_data.get("roomList") or room_data.get("list", [])
    
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
            # Support both "roomList" and "list" field names
            return room_data.get("roomList") or room_data.get("list", [])
    
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
            # Support both "roomList" and "list" field names
            return room_data.get("roomList") or room_data.get("list", [])
    
    async def find_user_stream(self, session: aiohttp.ClientSession, user_id: str | int, max_pages: int = 15) -> dict[str, Any] | None:
        """Find stream for specific user. Returns stream info or None."""
        import asyncio
        
        target_id = str(user_id).strip()
        
        print(f"[BuzzCast] 🔍 Searching for user {target_id} stream across all listings...")
        
        # Search recommend channels first (most common)
        for page in range(1, 4):
            try:
                channels = await self.get_live_channels(session, page=page)
                if channels:
                    print(f"[BuzzCast] RECOMMEND page {page}: found {len(channels)} live channels")
                    
                    for channel in channels:
                        ch_user_id = str(channel.get("userId", "")).strip()
                        ch_user_id_long = str(channel.get("userIdLong", "")).strip()
                        ch_id = str(channel.get("id", "")).strip()
                        
                        # Check match
                        if target_id in [ch_user_id, ch_user_id_long, ch_id] and target_id:
                            live_id = channel.get("liveId", "N/A")
                            online_num = channel.get("onlineNum", 0)
                            nick = channel.get("infoName", channel.get("userNickName", "Unknown"))
                            print(f"[BuzzCast] ✅ FOUND in RECOMMEND! User: {nick}, liveId={live_id}, viewers={online_num}")
                            return channel
            except Exception as e:
                print(f"[BuzzCast] Error checking RECOMMEND page {page}: {e}")
                continue
        
        # Try game channels
        print(f"[BuzzCast] Not in recommend, checking Game channels...")
        for page in range(1, 3):
            try:
                channels = await self.get_game_live_channels(session, page=page)
                if channels:
                    print(f"[BuzzCast] GAME page {page}: found {len(channels)} live channels")
                    
                    for channel in channels:
                        ch_user_id = str(channel.get("userId", "")).strip()
                        ch_user_id_long = str(channel.get("userIdLong", "")).strip()
                        ch_id = str(channel.get("id", "")).strip()
                        
                        if target_id in [ch_user_id, ch_user_id_long, ch_id] and target_id:
                            live_id = channel.get("liveId", "N/A")
                            online_num = channel.get("onlineNum", 0)
                            nick = channel.get("infoName", channel.get("userNickName", "Unknown"))
                            print(f"[BuzzCast] ✅ FOUND in GAME! User: {nick}, liveId={live_id}, viewers={online_num}")
                            return channel
            except Exception as e:
                print(f"[BuzzCast] Error checking GAME page {page}: {e}")
                continue
        
        # Try PK channels
        print(f"[BuzzCast] Not in game, checking PK channels...")
        for page in range(1, 3):
            try:
                channels = await self.get_pk_live_channels(session, page=page)
                if channels:
                    print(f"[BuzzCast] PK page {page}: found {len(channels)} live channels")
                    
                    for channel in channels:
                        ch_user_id = str(channel.get("userId", "")).strip()
                        ch_user_id_long = str(channel.get("userIdLong", "")).strip()
                        ch_id = str(channel.get("id", "")).strip()
                        
                        if target_id in [ch_user_id, ch_user_id_long, ch_id] and target_id:
                            live_id = channel.get("liveId", "N/A")
                            online_num = channel.get("onlineNum", 0)
                            nick = channel.get("infoName", channel.get("userNickName", "Unknown"))
                            print(f"[BuzzCast] ✅ FOUND in PK! User: {nick}, liveId={live_id}, viewers={online_num}")
                            return channel
            except Exception as e:
                print(f"[BuzzCast] Error checking PK page {page}: {e}")
                continue
        
        print(f"[BuzzCast] ❌ User {target_id} not found in any live channels (checked all listing types)")
        return None
    
    async def get_stream_url(self, session: aiohttp.ClientSession, user_id: str | int) -> tuple[str | None, dict[str, Any] | None]:
        """
        Get HLS stream URL for user.
        Returns (stream_url, stream_info) or (None, user_profile) if offline.
        """
        print(f"[BuzzCast] ===== get_stream_url for user {user_id} =====")
        
        # ALWAYS search live listings first - this is the most reliable way
        # liveState can be outdated or incorrect
        print(f"[BuzzCast] Searching in live listings first (most reliable)...")
        stream_info = await self.find_user_stream(session, user_id)
        
        if stream_info:
            print(f"[BuzzCast] ✅ User {user_id} FOUND in live listings!")
            nick = stream_info.get("infoName", "Unknown")
            online_num = stream_info.get("onlineNum", 0)
            live_id = stream_info.get("liveId", "N/A")
            
            # Extract URL
            url = (
                stream_info.get("hlsUrl") or
                stream_info.get("playUrl") or
                stream_info.get("videoUrl") or
                stream_info.get("webRtcUrl") or
                stream_info.get("flvUrl")
            )
            
            if url:
                print(f"[BuzzCast] ✅ Stream found: {nick} (liveId: {live_id}, viewers: {online_num})")
                print(f"[BuzzCast] Stream URL: {url[:80]}...")
                return url, stream_info
            else:
                print(f"[BuzzCast] ⚠️ Stream found but no valid URL in response")
                return None, stream_info
        
        # User not in live listings - check profile to confirm they exist
        print(f"[BuzzCast] User {user_id} NOT in live listings, checking if user exists...")
        user_profile = await self.search_user(session, user_id)
        
        if user_profile is None:
            print(f"[BuzzCast] ❌ User {user_id} not found anywhere (search API + listings)")
            return None, None
        
        # User exists but is offline
        nick = user_profile.get("userNickName", "Unknown")
        live_state = user_profile.get("liveState", 0)
        print(f"[BuzzCast] ✅ User exists: {nick} (ID: {user_id})")
        print(f"[BuzzCast] ⚫ User is OFFLINE (liveState={live_state}, not in live listings)")
        
        return None, user_profile
