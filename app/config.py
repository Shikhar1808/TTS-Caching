"""
Configuration module for the TTS Caching Server.

Loads settings from environment variables and .env file using Pydantic Settings.
All server behavior — cache paths, Redis URL, API credentials, feature toggles —
is controlled here.
"""

from pydantic_settings import BaseSettings
from pydantic import Field

# Mapping of full language names to ISO codes.
# Used for normalizing language inputs so that both "english" and "en"
# produce the same cache key.
LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en",
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
    "bengali": "bn",
    "gujarati": "gu",
    "marathi": "mr",
    "punjabi": "pa",
    "urdu": "ur",
    "odia": "or",
    "assamese": "as",
}


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    # --- Cache storage ---
    cache_dir: str = Field(
        default="./tts_cache",
        description="Root directory for cached audio files",
    )

    # --- Redis ---
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )

    # --- Cache limits ---
    max_cache_size_gb: float = Field(
        default=10.0,
        description="Maximum cache size in gigabytes before eviction triggers",
    )

    # --- Eviction (optional) ---
    eviction_enabled: bool = Field(
        default=False,
        description="Toggle automatic LRU eviction on/off",
    )
    eviction_threshold: float = Field(
        default=0.8,
        description="Trigger eviction when disk usage exceeds this fraction of max_cache_size_gb",
    )

    # --- Pre-generated audio fallback ---
    pre_generated_dir: str = Field(
        default="./TTSPrompt",
        description="Directory containing pre-generated TTS audio files for local fallback",
    )

    # --- Demo mode (optional) ---
    demo_mode_enabled: bool = Field(
        default=True,
        description="Toggle the /demo/synthesize endpoint and benchmark features on/off",
    )

    # --- External TTS API ---
    tts_api_base_url: str = Field(
        default="http://localhost:8000/demo",
        description="Base URL of the external TTS API",
    )
    tts_api_key: str = Field(
        default="demo-key",
        description="API key for the external TTS service",
    )
    tts_api_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for external TTS API calls",
    )
    tts_api_max_retries: int = Field(
        default=3,
        description="Maximum retry attempts for transient TTS API failures",
    )

    # --- Distributed lock ---
    lock_timeout_seconds: int = Field(
        default=30,
        description="TTL in seconds for the Redis distributed lock",
    )
    lock_retry_interval: float = Field(
        default=0.5,
        description="Seconds between lock acquisition retry attempts",
    )
    lock_max_retries: int = Field(
        default=20,
        description="Maximum number of lock acquisition attempts before giving up",
    )

    # --- Logging ---
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Creates the Settings instance on first call, then returns the same
    object on subsequent calls.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
