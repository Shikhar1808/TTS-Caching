"""
LRU eviction module for the TTS Caching Server.

Deletes least-recently-used cached audio files when disk usage exceeds
the configured threshold. Can run as:

1. Background task — triggered after cache writes (when eviction_enabled=True)
2. Standalone CLI — run `python -m app.eviction` for manual cleanup

Eviction is OPTIONAL and controlled by the `eviction_enabled` config flag.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import redis.asyncio as aioredis

from .config import get_settings
from .models import CacheMetadata

logger = logging.getLogger(__name__)


async def run_eviction(redis_client: aioredis.Redis) -> dict:
    """Run the LRU eviction process.

    Scans all cache metadata in Redis, sorts entries by last_accessed
    (oldest first), and deletes files + metadata until disk usage drops
    below the target threshold.

    Args:
        redis_client: An async Redis client instance.

    Returns:
        A dict with eviction results:
        {
            "evicted_count": int,
            "freed_bytes": int,
            "before_bytes": int,
            "after_bytes": int,
        }
    """
    settings = get_settings()
    cache_dir = Path(settings.cache_dir)
    max_bytes = int(settings.max_cache_size_gb * (1024 ** 3))
    threshold_bytes = int(max_bytes * settings.eviction_threshold)
    # Target: bring usage down to 70% to create headroom
    target_bytes = int(max_bytes * 0.7)

    # Calculate current disk usage
    current_usage = _get_disk_usage(cache_dir)
    logger.info(
        "Eviction check: usage=%d bytes (%.2f GB), threshold=%d bytes (%.2f GB)",
        current_usage,
        current_usage / (1024 ** 3),
        threshold_bytes,
        threshold_bytes / (1024 ** 3),
    )

    if current_usage <= threshold_bytes:
        logger.info("Disk usage below threshold, no eviction needed")
        return {
            "evicted_count": 0,
            "freed_bytes": 0,
            "before_bytes": current_usage,
            "after_bytes": current_usage,
        }

    # Collect all metadata entries from Redis
    entries: list[tuple[str, CacheMetadata]] = []
    async for key in redis_client.scan_iter(match="tts:meta:*", count=500):
        raw = await redis_client.get(key)
        if raw is None:
            continue
        try:
            metadata = CacheMetadata.model_validate_json(raw)
            # Extract cache_key from the Redis key (tts:meta:{cache_key})
            cache_key = key.decode("utf-8") if isinstance(key, bytes) else key
            cache_key = cache_key.replace("tts:meta:", "")
            entries.append((cache_key, metadata))
        except Exception:
            logger.warning("Corrupt metadata in Redis for key %s, skipping", key)

    # Sort by last_accessed ascending (oldest first = evict first)
    entries.sort(key=lambda e: e[1].last_accessed)

    evicted_count = 0
    freed_bytes = 0
    before_bytes = current_usage

    for cache_key, metadata in entries:
        if current_usage - freed_bytes <= target_bytes:
            break

        file_path = Path(metadata.path)

        # Delete file from disk
        if file_path.exists():
            try:
                file_size = file_path.stat().st_size
                os.remove(file_path)
                freed_bytes += file_size
                logger.info(
                    "Evicted: %s (%d bytes, last_accessed=%s, text='%s...')",
                    cache_key,
                    file_size,
                    metadata.last_accessed,
                    metadata.text[:50],
                )
            except OSError as e:
                logger.error("Failed to delete file %s: %s", file_path, e)
                continue
        else:
            logger.warning("File already missing from disk: %s", file_path)

        # Delete metadata from Redis
        redis_key = f"tts:meta:{cache_key}"
        await redis_client.delete(redis_key)
        evicted_count += 1

    after_bytes = before_bytes - freed_bytes
    logger.info(
        "Eviction complete: evicted %d files, freed %d bytes (%.2f MB), "
        "usage now %d bytes (%.2f GB)",
        evicted_count,
        freed_bytes,
        freed_bytes / (1024 ** 2),
        after_bytes,
        after_bytes / (1024 ** 3),
    )

    return {
        "evicted_count": evicted_count,
        "freed_bytes": freed_bytes,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
    }


def _get_disk_usage(cache_dir: Path) -> int:
    """Calculate total disk usage of the cache directory.

    Args:
        cache_dir: Path to the cache root directory.

    Returns:
        Total bytes used by cached files (excluding .tmp files).
    """
    total = 0
    if not cache_dir.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for filename in filenames:
            if filename.endswith(".tmp"):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, filename))
            except OSError:
                pass
    return total


async def _run_standalone() -> None:
    """Run eviction as a standalone CLI script."""
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    logger.info("Starting standalone eviction run...")
    redis_client = aioredis.from_url(
        settings.redis_url, decode_responses=False
    )

    try:
        result = await run_eviction(redis_client)
        logger.info("Eviction result: %s", json.dumps(result))
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(_run_standalone())
