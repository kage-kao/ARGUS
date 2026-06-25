"""SOCKS5 proxy manager for BuzzCast requests.

Loads a public SOCKS5 list and rotates through it to dodge per-IP rate limits
(HTTP 429 from BuzzCast). Proxies are consumed via aiohttp_socks.ProxyConnector
(plain aiohttp `proxy=` does NOT support socks5).
"""
from __future__ import annotations
import asyncio
import os
import random
from typing import List, Set

import aiohttp

PROXY_LIST_URL = os.environ.get(
    "PROXY_LIST_URL",
    "https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/MostStable/socks5.txt",
)


class ProxyManager:
    """Manages SOCKS5 proxy rotation (thread/async-safe load)."""

    def __init__(self, url: str = PROXY_LIST_URL) -> None:
        self.url = url
        self.proxies: List[str] = []
        self.working: List[str] = []
        self.failed: Set[str] = set()
        self._lock = asyncio.Lock()
        self._loaded = False

    async def ensure_loaded(self) -> None:
        """Load the proxy list exactly once (idempotent, concurrency-safe)."""
        if self._loaded and self.proxies:
            return
        async with self._lock:
            if self._loaded and self.proxies:
                return
            await self._load()

    async def _load(self) -> None:
        try:
            print(f"[Proxy] Loading SOCKS5 list from {self.url} ...")
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(self.url) as r:
                    text = await r.text()
            self.proxies = [
                line if "://" in line else f"socks5://{line}"
                for raw in text.splitlines()
                if (line := raw.strip()) and not line.startswith("#")
            ]
            self.working = self.proxies.copy()
            self.failed.clear()
            self._loaded = True
            print(f"[Proxy] ✅ Loaded {len(self.proxies)} SOCKS5 proxies")
        except Exception as e:  # noqa: BLE001
            print(f"[Proxy] ⚠️ Failed to load proxies: {e}")
            self.proxies = []
            self.working = []

    def get_random_proxy(self) -> str | None:
        """Pick a random working proxy, recycling the pool when exhausted."""
        if self.working:
            return random.choice(self.working)
        if self.proxies:
            # everything tried & failed → recycle and try again
            self.working = self.proxies.copy()
            self.failed.clear()
            return random.choice(self.working)
        return None

    def mark_failed(self, proxy: str) -> None:
        if proxy in self.working:
            self.working.remove(proxy)
            self.failed.add(proxy)

    def mark_success(self, proxy: str) -> None:
        if proxy not in self.working and proxy in self.proxies:
            self.working.append(proxy)
        self.failed.discard(proxy)


# Global singleton shared by all BuzzCast clients.
_proxy_manager = ProxyManager()


def get_proxy_manager() -> ProxyManager:
    return _proxy_manager
