"""BuzzCast API client for anonymous tourist access.

Reverse-engineered from BuzzCast 3.2.75 APK and buzzcast.com PWA.
Uses the authoritative 3-step method:
1. Tourist token via POST /api/user/pwa/event
2. Check liveId via GET /api/token/live/info/getLiveByUserId  
3. Get stream URLs via POST /faceshow/tokens/live/broadcast/detail
"""
from __future__ import annotations
import aiohttp
import base64
import json
import random
import time
import uuid
from typing import Any


BASE_URL = "https://api.buzzcast.com"
FACE_URL = "https://dhcxzil.buzzcast.com/faceshow"
UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"


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
            "Accept": "application/json, text/plain, */*",
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
        payload = {"webId": f"pwa_{self.device_id}"}
        
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
    
    async def is_live_and_liveid(self, session: aiohttp.ClientSession, user_id: str | int) -> int:
        """Step 2: Check if user is live and get liveId.
        Returns liveId (int) if potentially live, 0 if definitely offline.
        WARNING: This may return stale liveId from finished broadcast!
        Always validate with broadcast_detail (step 3).
        """
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{BASE_URL}/api/token/live/info/getLiveByUserId"
        params = {"userId": str(user_id)}
        
        async with session.get(url, params=params, headers=self._headers(), timeout=20) as resp:
            data = await resp.json()
            if data.get("code") != 1:
                print(f"[BuzzCast] getLiveByUserId error: {data}")
                return 0
            
            live_data = data.get("data")
            return int(live_data) if live_data else 0
    
    async def broadcast_detail(self, session: aiohttp.ClientSession, live_id: int) -> dict[str, Any] | None:
        """Step 3: Get authoritative broadcast details and stream URLs.
        
        Returns result dict with:
        - result.status == 1 AND result.live != None => LIVE (contains stream URLs)
        - result.status == 0 OR result.live == None => offline
        
        Stream URLs are in result.live: flvUrl, hlsUrl, rtmpUrl, webrtcUrl, streamId
        """
        if self.tourist_vid is None:
            await self.init_tourist(session)
        
        url = f"{FACE_URL}/tokens/live/broadcast/detail"
        
        # Try different liveType values (0=normal, 1=voice, 2=PK, 3=game)
        for live_type in (0, 1, 2, 3):
            payload = {
                "liveId": str(live_id),
                "liveType": live_type,
                "startTime": "",
                "drmFlag": 0
            }
            
            try:
                async with session.post(url, json=payload, headers=self._headers(), timeout=30) as resp:
                    data = await resp.json()
                    
                    # Get result - could be in "result" or "data" field
                    result = data.get("result") if "result" in data else data.get("data")
                    
                    if result:
                        # Check if live
                        status = result.get("status")
                        live_info = result.get("live")
                        
                        if status == 1 and live_info:
                            print(f"[BuzzCast] ✅ Found active stream with liveType={live_type}")
                            return result
                        elif status == 1 or live_info:
                            # Partial match, keep trying
                            return result
            except Exception as e:
                print(f"[BuzzCast] broadcast_detail liveType={live_type} error: {e}")
                continue
        
        # No active stream found
        return None
    
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
        Get HLS stream URL for user using the authoritative 3-step method:
        1. Get liveId via getLiveByUserId
        2. Validate with broadcast/detail 
        3. Extract stream URLs
        
        Returns (stream_url, stream_info) or (None, user_profile) if offline.
        """
        print(f"[BuzzCast] ===== get_stream_url for user {user_id} (3-step method) =====")
        
        # Step 2: Check if user has liveId
        live_id = await self.is_live_and_liveid(session, user_id)
        
        if not live_id:
            print(f"[BuzzCast] ❌ User {user_id} not live (getLiveByUserId returned 0/null)")
            # Check if user exists at all
            user_profile = await self.search_user(session, user_id)
            return None, user_profile
        
        print(f"[BuzzCast] getLiveByUserId -> liveId = {live_id}")
        
        # Step 3: Get authoritative broadcast details
        result = await self.broadcast_detail(session, live_id)
        
        if not result:
            print(f"[BuzzCast] ❌ broadcast/detail returned no result for liveId={live_id}")
            user_profile = await self.search_user(session, user_id)
            return None, user_profile
        
        status = result.get("status")
        live_info = result.get("live")
        
        if status != 1 or not live_info:
            print(f"[BuzzCast] ❌ NOT live (status={status}, live={'present' if live_info else 'null'})")
            print(f"[BuzzCast] Any URLs in result.finish.* are STALE from finished broadcast")
            user_profile = await self.search_user(session, user_id)
            return None, user_profile
        
        # User is LIVE! Extract stream URL
        print(f"[BuzzCast] ✅ User {user_id} IS LIVE (status={status})")
        
        # Priority: flvUrl > hlsUrl > rtmpUrl > webrtcUrl
        stream_url = (
            live_info.get("flvUrl") or
            live_info.get("hlsUrl") or
            live_info.get("rtmpUrl") or
            live_info.get("webrtcUrl")
        )
        
        if stream_url:
            nick = live_info.get("anchor", {}).get("nickName", "Unknown")
            stream_id = live_info.get("streamId", "N/A")
            print(f"[BuzzCast] Stream URL: {stream_url[:80]}...")
            print(f"[BuzzCast] Anchor: {nick}, streamId: {stream_id}")
            
            # Return live_info as stream_info for compatibility
            return stream_url, live_info
        else:
            print(f"[BuzzCast] ⚠️ Stream found but no valid URL in response")
            return None, live_info
