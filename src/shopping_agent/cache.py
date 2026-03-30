"""Cache abstraction for the image proxy.

To swap in Redis, implement the ImageCache protocol:

    class RedisImageCache:
        def __init__(self, client: redis.asyncio.Redis) -> None:
            self._r = client

        async def get(self, key: str) -> tuple[bytes, str] | None:
            data = await self._r.get(f"imgcache:{key}")
            if data is None:
                return None
            content, _, media_type = data.partition(b"|")
            return content, media_type.decode()

        async def set(self, key: str, content: bytes, media_type: str, ttl: int = 3600) -> None:
            payload = content + b"|" + media_type.encode()
            await self._r.set(f"imgcache:{key}", payload, ex=ttl)

Then replace the module-level `image_cache` instance with yours.
"""

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class ImageCache(Protocol):
    """Cache for proxied images. Keys are arbitrary strings; values are (bytes, media_type)."""

    async def get(self, key: str) -> tuple[bytes, str] | None:
        """Return cached (content, media_type), or None if missing/expired."""
        ...

    async def set(self, key: str, content: bytes, media_type: str, ttl: int = 3600) -> None:
        """Store content with the given TTL in seconds."""
        ...


class InMemoryImageCache:
    """LRU-ish in-memory image cache with TTL support.

    Args:
        max_entries: Maximum number of cached images before eviction kicks in.
        default_ttl: Default time-to-live in seconds.
    """

    def __init__(self, max_entries: int = 500, default_ttl: int = 3600) -> None:
        # Stores (content, media_type, expires_at). Insertion order = eviction order.
        self._store: dict[str, tuple[bytes, str, float]] = {}
        self._max_entries = max_entries
        self._default_ttl = default_ttl

    async def get(self, key: str) -> tuple[bytes, str] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        content, media_type, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return content, media_type

    async def set(self, key: str, content: bytes, media_type: str, ttl: int | None = None) -> None:
        if len(self._store) >= self._max_entries:
            evict = max(1, self._max_entries // 10)
            for k in list(self._store.keys())[:evict]:
                del self._store[k]
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        self._store[key] = (content, media_type, expires_at)


# Module-level instance. Replace with a Redis implementation if needed.
image_cache: ImageCache = InMemoryImageCache()
