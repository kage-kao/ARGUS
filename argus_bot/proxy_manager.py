"""Proxy manager for BuzzCast requests."""
import random
import aiohttp
from typing import List


PROXY_LIST_URL = "https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/MostStable/socks5.txt"


class ProxyManager:
    """Manages SOCKS5 proxy rotation."""
    
    def __init__(self):
        self.proxies: List[str] = []
        self.working_proxies: List[str] = []
        self.failed_proxies: set[str] = set()
    
    async def load_proxies(self, session: aiohttp.ClientSession) -> None:
        """Load proxies from GitHub."""
        try:
            print(f"[Proxy] Loading proxies from {PROXY_LIST_URL}...")
            async with session.get(PROXY_LIST_URL, timeout=10) as resp:
                text = await resp.text()
                self.proxies = [
                    f"socks5://{line.strip()}"
                    for line in text.strip().split('\n')
                    if line.strip() and not line.startswith('#')
                ]
                self.working_proxies = self.proxies.copy()
                print(f"[Proxy] ✅ Loaded {len(self.proxies)} SOCKS5 proxies")
        except Exception as e:
            print(f"[Proxy] ⚠️ Failed to load proxies: {e}")
            self.proxies = []
            self.working_proxies = []
    
    def get_random_proxy(self) -> str | None:
        """Get random working proxy."""
        # Try working proxies first
        if self.working_proxies:
            proxy = random.choice(self.working_proxies)
            return proxy
        
        # If all failed, reset and try again
        if self.proxies:
            print("[Proxy] All proxies failed, resetting...")
            self.working_proxies = self.proxies.copy()
            self.failed_proxies.clear()
            return random.choice(self.working_proxies)
        
        return None
    
    def mark_failed(self, proxy: str) -> None:
        """Mark proxy as failed."""
        if proxy in self.working_proxies:
            self.working_proxies.remove(proxy)
            self.failed_proxies.add(proxy)
            print(f"[Proxy] ❌ Proxy failed: {proxy[:50]}... ({len(self.working_proxies)} working left)")
    
    def mark_success(self, proxy: str) -> None:
        """Mark proxy as working (move to front of list)."""
        if proxy not in self.working_proxies and proxy in self.proxies:
            self.working_proxies.append(proxy)
            if proxy in self.failed_proxies:
                self.failed_proxies.remove(proxy)


# Global proxy manager instance
_proxy_manager = ProxyManager()


def get_proxy_manager() -> ProxyManager:
    """Get global proxy manager instance."""
    return _proxy_manager
