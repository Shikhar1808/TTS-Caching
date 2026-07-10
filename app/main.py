"""
FastAPI application for the TTS Caching Server.

Provides three core endpoints:
- POST /tts        — Generate or serve cached TTS audio
- GET  /health     — Health check (Redis + disk)
- GET  /cache/stats — Cache hit/miss statistics

And one optional demo endpoint (when demo_mode_enabled=True):
- POST /demo/synthesize — Generates random WAV audio for testing
"""

import asyncio
import io
import logging
import os
import random
import struct
import time
import wave
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .cache import CacheManager
from .config import get_settings
from .eviction import run_eviction
from .models import CacheStatsResponse, HealthResponse, TTSRequest
from .tts_client import TTSClient, TTSGenerationError

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Application state (set during lifespan)
# ------------------------------------------------------------------ #

_redis_client: aioredis.Redis | None = None
_cache_manager: CacheManager | None = None
_tts_client: TTSClient | None = None
_http_client: httpx.AsyncClient | None = None


def _get_cache_manager() -> CacheManager:
    """Get the CacheManager instance, raising if not initialized."""
    if _cache_manager is None:
        raise RuntimeError("CacheManager not initialized")
    return _cache_manager


def _get_tts_client() -> TTSClient:
    """Get the TTSClient instance, raising if not initialized."""
    if _tts_client is None:
        raise RuntimeError("TTSClient not initialized")
    return _tts_client


# ------------------------------------------------------------------ #
# Lifespan
# ------------------------------------------------------------------ #


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown.

    On startup:
    - Configure structured logging
    - Connect to Redis
    - Create CacheManager and TTSClient instances
    - Ensure cache directory exists

    On shutdown:
    - Close httpx client
    - Close Redis connection
    """
    global _redis_client, _cache_manager, _tts_client, _http_client

    settings = get_settings()

    # Configure structured logging
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        force=True,
    )

    logger.info("Starting TTS Caching Server...")
    logger.info("Cache directory: %s", os.path.abspath(settings.cache_dir))
    logger.info("Redis URL: %s", settings.redis_url)
    logger.info("Demo mode: %s", settings.demo_mode_enabled)
    logger.info("Eviction enabled: %s", settings.eviction_enabled)

    # Connect to Redis
    _redis_client = aioredis.from_url(
        settings.redis_url, decode_responses=False
    )
    try:
        await _redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.error("Failed to connect to Redis: %s", e)
        raise

    # Create CacheManager
    _cache_manager = CacheManager(_redis_client)

    # Create HTTP client and TTS client
    _http_client = httpx.AsyncClient()
    _tts_client = TTSClient(_http_client)

    # Ensure cache directory exists
    os.makedirs(settings.cache_dir, exist_ok=True)
    logger.info("Cache directory ready: %s", os.path.abspath(settings.cache_dir))

    logger.info("TTS Caching Server started successfully")
    yield

    # Shutdown
    logger.info("Shutting down TTS Caching Server...")
    if _http_client:
        await _http_client.aclose()
    if _redis_client:
        await _redis_client.aclose()
    logger.info("Shutdown complete")


# ------------------------------------------------------------------ #
# FastAPI app
# ------------------------------------------------------------------ #

app = FastAPI(
    title="TTS Caching Server",
    description="Production-grade TTS caching with content-addressable storage",
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------ #
# POST /tts — Core endpoint
# ------------------------------------------------------------------ #


@app.post("/tts")
async def synthesize_speech(request: TTSRequest) -> FileResponse:
    """Generate or serve cached text-to-speech audio.

    Flow:
    1. Compute cache key from input parameters
    2. Check Redis metadata + verify file on disk
    3. HIT → stream file, update last_accessed (fire-and-forget)
    4. MISS → acquire lock, double-check, generate, cache, release, stream

    Returns:
        FileResponse streaming the audio file.

    Raises:
        HTTPException: 502 on TTS API failure, 503 on lock timeout.
    """
    cache_manager = _get_cache_manager()
    tts_client = _get_tts_client()
    settings = get_settings()

    # Step 1: Compute cache key
    cache_key = cache_manager.compute_key(request)
    logger.info(
        "TTS request: cache_key=%s text='%s...' voice=%s lang=%s",
        cache_key,
        request.text[:50],
        request.voice,
        request.language,
    )

    # Step 2-3: Check cache
    metadata = await cache_manager.get(cache_key)
    if metadata is not None:
        # CACHE HIT — serve the file
        # Fire-and-forget: update last_accessed without blocking response
        asyncio.create_task(cache_manager.update_last_accessed(cache_key))

        return FileResponse(
            path=metadata.path,
            media_type=_get_media_type(request.format),
            filename=f"{cache_key}.{request.format}",
            headers={"X-Cache-Status": "HIT"},
        )

    # Step 4: CACHE MISS — acquire distributed lock
    start_lock = time.perf_counter()
    lock_value = await cache_manager.acquire_lock(cache_key)
    lock_wait_ms = (time.perf_counter() - start_lock) * 1000

    if lock_value is None:
        logger.error("Lock timeout for cache_key=%s (waited %.1fms)", cache_key, lock_wait_ms)
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable: lock acquisition timeout",
        )

    logger.debug("Lock wait time: %.1fms for %s", lock_wait_ms, cache_key)

    try:
        # Double-check cache after acquiring lock (another request may have
        # just finished generating this exact audio)
        metadata = await cache_manager.get(cache_key)
        if metadata is not None:
            logger.info("Cache HIT after lock (race avoided): %s", cache_key)
            asyncio.create_task(cache_manager.update_last_accessed(cache_key))
            return FileResponse(
                path=metadata.path,
                media_type=_get_media_type(request.format),
                filename=f"{cache_key}.{request.format}",
                headers={"X-Cache-Status": "HIT"},
            )

        # Step 5: Still a miss — call external TTS API
        start_gen = time.perf_counter()
        try:
            audio_data = await tts_client.generate(request)
        except TTSGenerationError as e:
            status = e.status_code or 502
            logger.error(
                "TTS generation failed for %s: %s (status=%s)",
                cache_key,
                str(e),
                status,
            )
            raise HTTPException(
                status_code=502,
                detail=f"External TTS API failed: {str(e)}",
            )

        gen_ms = (time.perf_counter() - start_gen) * 1000
        logger.info("TTS generation latency: %.1fms for %s", gen_ms, cache_key)

        # Steps 6-8: Write to cache (atomic) + store metadata
        metadata = await cache_manager.put(cache_key, audio_data, request)

        # Trigger eviction in background if enabled
        if settings.eviction_enabled and _redis_client is not None:
            asyncio.create_task(_maybe_evict())

    finally:
        # Step 9: Always release the lock
        await cache_manager.release_lock(cache_key, lock_value)

    # Step 10: Stream the file back
    return FileResponse(
        path=metadata.path,
        media_type=_get_media_type(request.format),
        filename=f"{cache_key}.{request.format}",
        headers={"X-Cache-Status": "MISS"},
    )


# ------------------------------------------------------------------ #
# GET /health
# ------------------------------------------------------------------ #


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Check server health: Redis connectivity and cache directory status.

    Returns:
        HealthResponse with status and component checks.
    """
    settings = get_settings()

    redis_ok = False
    if _redis_client is not None:
        try:
            await _redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    cache_exists = os.path.isdir(settings.cache_dir)
    cache_writable = os.access(settings.cache_dir, os.W_OK) if cache_exists else False

    status = "healthy" if (redis_ok and cache_exists and cache_writable) else "unhealthy"

    return HealthResponse(
        status=status,
        redis_connected=redis_ok,
        cache_dir_exists=cache_exists,
        cache_dir_writable=cache_writable,
    )


# ------------------------------------------------------------------ #
# GET /cache/stats
# ------------------------------------------------------------------ #


@app.get("/cache/stats", response_model=CacheStatsResponse)
async def cache_stats() -> CacheStatsResponse:
    """Get cache statistics including hit/miss counts and disk usage.

    Returns:
        CacheStatsResponse with aggregate cache metrics.
    """
    cache_manager = _get_cache_manager()
    return await cache_manager.get_stats()


# ------------------------------------------------------------------ #
# POST /demo/synthesize — Demo TTS endpoint (optional)
# ------------------------------------------------------------------ #


@app.post("/demo/synthesize", response_model=None)
async def demo_synthesize(request: Request):
    """Generate random WAV audio for testing (demo mode only).

    Produces a valid WAV file with ~1 second of random noise at 22050 Hz,
    16-bit PCM. Adds artificial latency to simulate a real TTS API.

    This endpoint is only available when demo_mode_enabled=True.

    Returns:
        Raw WAV audio bytes.
    """
    settings = get_settings()
    if not settings.demo_mode_enabled:
        return JSONResponse(
            status_code=404,
            content={"detail": "Demo mode is disabled"},
        )

    body = await request.json()
    text = body.get("text", "")

    # Artificial latency to simulate real API (300ms – 1000ms)
    delay = random.uniform(0.3, 1.0)
    await asyncio.sleep(delay)

    # Generate random WAV audio (~1 second, 22050 Hz, 16-bit mono PCM)
    sample_rate = 22050
    duration_seconds = 1.0
    num_samples = int(sample_rate * duration_seconds)

    # Generate random samples (white noise)
    samples = [random.randint(-32768, 32767) for _ in range(num_samples)]
    audio_data = struct.pack(f"<{num_samples}h", *samples)

    # Write WAV to in-memory buffer
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data)

    wav_bytes = buffer.getvalue()

    logger.debug(
        "Demo: generated %d bytes WAV (delay=%.2fs, text='%s...')",
        len(wav_bytes),
        delay,
        text[:50],
    )

    from fastapi.responses import Response

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
    )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _get_media_type(fmt: str) -> str:
    """Map audio format to MIME type.

    Args:
        fmt: Audio format string (e.g. "wav", "mp3").

    Returns:
        The corresponding MIME type string.
    """
    mime_map = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
    }
    return mime_map.get(fmt, "application/octet-stream")


async def _maybe_evict() -> None:
    """Run eviction if disk usage exceeds threshold.

    Called as a fire-and-forget background task after cache writes.
    """
    if _redis_client is None:
        return
    try:
        await run_eviction(_redis_client)
    except Exception as e:
        logger.error("Background eviction failed: %s", e)
