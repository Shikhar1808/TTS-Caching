"""
Cache manager for the TTS Caching Server.

Handles all cache operations: key computation (xxh3_128), Redis metadata
lookup, disk I/O with atomic writes (temp file + rename), distributed
locking, and cache statistics.

Audio bytes are stored on disk only — Redis holds metadata and locks.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
import xxhash

from .config import get_settings, get_full_language_name
from .models import CacheMetadata, CacheStatsResponse, TTSRequest

logger = logging.getLogger(__name__)

# Lua script for safe lock release: only delete if the lock value matches
# the holder's UUID. Prevents accidentally releasing another holder's lock.
RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class CacheManager:
    """Manages the TTS audio cache with Redis metadata and disk storage.

    Provides content-addressable caching via xxh3_128 hashing of input
    parameters, distributed locking for concurrent request deduplication,
    and atomic writes to prevent partial/corrupt files on disk.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        """Initialize CacheManager.

        Args:
            redis_client: An async Redis client instance.
        """
        self._redis = redis_client
        self._settings = get_settings()
        self._cache_dir = Path(self._settings.cache_dir)
        self._release_lock_sha: Optional[str] = None

    async def _ensure_lock_script(self) -> str:
        """Load the Lua lock-release script into Redis and cache the SHA.

        Returns:
            The SHA1 hash of the loaded script.
        """
        if self._release_lock_sha is None:
            self._release_lock_sha = await self._redis.script_load(RELEASE_LOCK_SCRIPT)
        return self._release_lock_sha

    # ------------------------------------------------------------------ #
    # Key computation
    # ------------------------------------------------------------------ #

    @staticmethod
    def compute_key(request: TTSRequest) -> str:
        """Compute the cache key by hashing the full input parameters.

        The cache key is a pure hex hash — no human-readable prefix or slug.

        Formula:
            input_string = f"{full_language_name}_{voice}_{text}"
            cache_key    = xxh3_128(input_string + format).hexdigest()

        Args:
            request: The TTS request containing text, voice, language, format.

        Returns:
            A 32-character hex string (128-bit hash).
        """
        full_lang = get_full_language_name(request.language)
        input_string = f"{full_lang}_{request.voice}_{request.text}"
        hash_input = input_string + request.format
        return xxhash.xxh3_128(hash_input.encode("utf-8")).hexdigest()

    @staticmethod
    def get_shard(cache_key: str) -> str:
        """Extract the shard directory name from a cache key.

        Uses the first 2 hex characters of the hash to distribute files
        across ~256 subdirectories, preventing too many files in one dir.

        Args:
            cache_key: The hex hash string.

        Returns:
            A 2-character hex string (e.g. "a3", "ff").
        """
        return cache_key[:2]

    def get_file_path(self, cache_key: str, fmt: str) -> Path:
        """Build the full file path for a cached audio file.

        Args:
            cache_key: The hex hash string.
            fmt: Audio format extension (e.g. "wav").

        Returns:
            Path like {cache_dir}/{shard}/{cache_key}.{fmt}
        """
        shard = self.get_shard(cache_key)
        return self._cache_dir / shard / f"{cache_key}.{fmt}"

    # ------------------------------------------------------------------ #
    # Cache get / put
    # ------------------------------------------------------------------ #

    async def get(self, cache_key: str) -> Optional[CacheMetadata]:
        """Look up a cache entry by key.

        Checks Redis for metadata, then verifies the file exists on disk.
        Increments the hit counter on success.

        Args:
            cache_key: The hex hash to look up.

        Returns:
            CacheMetadata if the entry exists and the file is on disk, None otherwise.
        """
        redis_key = f"tts:meta:{cache_key}"
        raw = await self._redis.get(redis_key)

        if raw is None:
            logger.debug("Cache MISS (Redis): %s", cache_key)
            return None

        try:
            metadata = CacheMetadata.model_validate_json(raw)
        except Exception:
            logger.warning("Corrupt metadata in Redis for key %s, treating as miss", cache_key)
            await self._redis.delete(redis_key)
            return None

        # Verify the file actually exists on disk
        if not Path(metadata.path).exists():
            logger.warning(
                "File missing from disk for key %s (path: %s), cleaning Redis entry",
                cache_key,
                metadata.path,
            )
            await self._redis.delete(redis_key)
            return None

        # Increment hit counter
        await self._redis.incr("tts:stats:hits")
        logger.info("Cache HIT: %s", cache_key)
        return metadata

    async def put(
        self, cache_key: str, audio_data: bytes, request: TTSRequest
    ) -> CacheMetadata:
        """Write audio data to the cache.

        Performs an atomic write: writes to a .tmp file first, then uses
        os.rename() to move it to the final path. Stores metadata in Redis.
        Increments the miss counter.

        Args:
            cache_key: The hex hash for this entry.
            audio_data: Raw audio bytes to store.
            request: The original TTS request (for metadata).

        Returns:
            The CacheMetadata stored in Redis.
        """
        fmt = request.format
        shard = self.get_shard(cache_key)
        shard_dir = self._cache_dir / shard
        final_path = shard_dir / f"{cache_key}.{fmt}"
        temp_path = shard_dir / f"{cache_key}.{fmt}.tmp"

        # Ensure shard directory exists
        os.makedirs(shard_dir, exist_ok=True)

        # Atomic write: temp file → rename
        try:
            with open(temp_path, "wb") as f:
                f.write(audio_data)

            os.rename(str(temp_path), str(final_path))
            logger.debug("Atomic write complete: %s", final_path)
        except OSError as e:
            # Clean up temp file if rename failed
            if temp_path.exists():
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write cache file: {e}") from e

        now = datetime.now(timezone.utc).isoformat()
        metadata = CacheMetadata(
            path=str(final_path.resolve()),
            size_bytes=len(audio_data),
            created_at=now,
            last_accessed=now,
            language=request.language,
            voice=request.voice,
            text=request.text,
            format=request.format,
        )

        # Store metadata in Redis
        redis_key = f"tts:meta:{cache_key}"
        await self._redis.set(redis_key, metadata.model_dump_json())

        # Increment miss counter
        await self._redis.incr("tts:stats:misses")

        logger.info(
            "Cache PUT: %s (%d bytes, shard=%s)",
            cache_key,
            len(audio_data),
            shard,
        )
        return metadata

    async def update_last_accessed(self, cache_key: str) -> None:
        """Update the last_accessed timestamp for a cache entry.

        Intended to be called fire-and-forget (don't await in the response path).

        Args:
            cache_key: The hex hash of the entry to update.
        """
        redis_key = f"tts:meta:{cache_key}"
        raw = await self._redis.get(redis_key)
        if raw is None:
            return

        try:
            data = json.loads(raw)
            data["last_accessed"] = datetime.now(timezone.utc).isoformat()
            await self._redis.set(redis_key, json.dumps(data))
        except Exception:
            logger.warning("Failed to update last_accessed for %s", cache_key)

    # ------------------------------------------------------------------ #
    # Distributed locking
    # ------------------------------------------------------------------ #

    async def acquire_lock(self, cache_key: str) -> Optional[str]:
        """Acquire a distributed lock for a cache key.

        Uses Redis SET NX PX to implement a lock with a TTL. Retries with
        a fixed interval up to max_retries.

        Args:
            cache_key: The hex hash to lock on.

        Returns:
            The lock value (UUID) if acquired, None if timed out.
        """
        lock_key = f"lock:{cache_key}"
        lock_value = str(uuid.uuid4())
        ttl_ms = self._settings.lock_timeout_seconds * 1000

        for attempt in range(self._settings.lock_max_retries):
            acquired = await self._redis.set(
                lock_key, lock_value, nx=True, px=ttl_ms
            )
            if acquired:
                logger.debug(
                    "Lock acquired: %s (attempt %d)", cache_key, attempt + 1
                )
                return lock_value

            logger.debug(
                "Lock busy: %s (attempt %d/%d), retrying in %.1fs",
                cache_key,
                attempt + 1,
                self._settings.lock_max_retries,
                self._settings.lock_retry_interval,
            )
            await asyncio.sleep(self._settings.lock_retry_interval)

        logger.warning("Lock acquisition timed out for %s", cache_key)
        return None

    async def release_lock(self, cache_key: str, lock_value: str) -> bool:
        """Release a distributed lock, but only if we still own it.

        Uses a Lua script to atomically check the lock value and delete it,
        preventing accidental release of another holder's lock.

        Args:
            cache_key: The hex hash the lock was acquired for.
            lock_value: The UUID returned by acquire_lock().

        Returns:
            True if the lock was released, False if it was already gone or
            owned by someone else.
        """
        lock_key = f"lock:{cache_key}"
        sha = await self._ensure_lock_script()

        try:
            result = await self._redis.evalsha(sha, 1, lock_key, lock_value)
            released = result == 1
            if released:
                logger.debug("Lock released: %s", cache_key)
            else:
                logger.warning("Lock release failed (not owner): %s", cache_key)
            return released
        except Exception:
            # Fallback: try direct delete (less safe but prevents stuck locks)
            logger.warning("Lua script failed for lock release, using fallback DEL: %s", cache_key)
            await self._redis.delete(lock_key)
            return True

    # ------------------------------------------------------------------ #
    # Disk usage & stats
    # ------------------------------------------------------------------ #

    def get_disk_usage(self) -> int:
        """Calculate total disk usage of the cache directory in bytes.

        Walks the entire cache_dir tree and sums file sizes.

        Returns:
            Total bytes used by cached files.
        """
        total = 0
        if not self._cache_dir.exists():
            return 0

        for dirpath, _dirnames, filenames in os.walk(self._cache_dir):
            for filename in filenames:
                # Skip temp files
                if filename.endswith(".tmp"):
                    continue
                filepath = os.path.join(dirpath, filename)
                try:
                    total += os.path.getsize(filepath)
                except OSError:
                    pass
        return total

    async def get_stats(self) -> CacheStatsResponse:
        """Collect cache statistics from Redis counters and disk.

        Returns:
            A CacheStatsResponse with hit/miss counts, total size, etc.
        """
        hits = await self._redis.get("tts:stats:hits")
        misses = await self._redis.get("tts:stats:misses")
        hits = int(hits) if hits else 0
        misses = int(misses) if misses else 0

        total_requests = hits + misses
        hit_rate = (hits / total_requests * 100) if total_requests > 0 else 0.0

        total_size_bytes = self.get_disk_usage()
        total_size_gb = total_size_bytes / (1024 ** 3)

        # Count entries via Redis key scan
        total_entries = 0
        async for _key in self._redis.scan_iter(match="tts:meta:*", count=1000):
            total_entries += 1

        return CacheStatsResponse(
            total_entries=total_entries,
            total_size_bytes=total_size_bytes,
            total_size_gb=round(total_size_gb, 4),
            cache_hits=hits,
            cache_misses=misses,
            hit_rate_percent=round(hit_rate, 2),
            max_cache_size_gb=self._settings.max_cache_size_gb,
        )
