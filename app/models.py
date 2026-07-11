"""
Pydantic models for the TTS Caching Server.

Defines request/response schemas for all API endpoints and the Redis
metadata structure used for cache entries.
"""

from pydantic import BaseModel, Field, field_validator


class TTSRequest(BaseModel):
    """Request body for POST /tts.

    The `text` field should contain the FINAL resolved text — any dynamic
    content (names, numbers, etc.) must already be substituted before
    reaching this endpoint.

    The `language` field accepts both ISO codes ("en") and full names
    ("english") — full names are normalized to ISO codes automatically.
    """

    text: str = Field(..., min_length=1, description="Text to synthesize into speech")
    voice: str = Field(..., min_length=1, description="Voice identifier")
    language: str = Field(..., min_length=1, description="BCP-47 language tag (e.g. 'en-IN', 'hi-IN')")
    format: str = Field(default="wav", description="Audio output format")

    @field_validator("language")
    @classmethod
    def normalize_language(cls, v: str) -> str:
        """Validate that the language is exactly a supported standard BCP-47 tag.

        Accepts only standard locale tags in their exact case (e.g. 'en-IN', 'hi-IN').
        Other casing variations (e.g. 'en-in') are rejected.
        """
        from .config import SUPPORTED_LOCALES

        if v not in SUPPORTED_LOCALES:
            raise ValueError(
                f"Unsupported language format: '{v}'. The request must use standard BCP-47 tags: "
                f"{', '.join(sorted(SUPPORTED_LOCALES))}."
            )
        return v


class CacheMetadata(BaseModel):
    """Metadata stored in Redis for each cached audio file.

    Contains the full human-readable context (language, voice, text) since
    the on-disk filename is a pure hash with no readable prefix.
    """

    path: str = Field(..., description="Absolute path to the cached audio file on disk")
    size_bytes: int = Field(..., description="Size of the audio file in bytes")
    created_at: str = Field(..., description="ISO 8601 timestamp when the file was first generated")
    last_accessed: str = Field(..., description="ISO 8601 timestamp of the most recent access")
    language: str = Field(..., description="Language code used for generation")
    voice: str = Field(..., description="Voice identifier used for generation")
    text: str = Field(..., description="Original text that was synthesized")
    format: str = Field(..., description="Audio format (e.g. 'wav')")


class CacheStatsResponse(BaseModel):
    """Response body for GET /cache/stats."""

    total_entries: int = Field(..., description="Number of cached audio files")
    total_size_bytes: int = Field(..., description="Total cache size in bytes")
    total_size_gb: float = Field(..., description="Total cache size in gigabytes")
    cache_hits: int = Field(..., description="Total cache hit count since last reset")
    cache_misses: int = Field(..., description="Total cache miss count since last reset")
    hit_rate_percent: float = Field(..., description="Cache hit rate as a percentage")
    max_cache_size_gb: float = Field(..., description="Configured maximum cache size in GB")


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str = Field(..., description="Overall health status ('healthy' or 'unhealthy')")
    redis_connected: bool = Field(..., description="Whether Redis is reachable")
    cache_dir_exists: bool = Field(..., description="Whether the cache directory exists")
    cache_dir_writable: bool = Field(..., description="Whether the cache directory is writable")
