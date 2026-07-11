"""
Configuration module for the TTS Caching Server.

Loads settings from environment variables and .env file using Pydantic Settings.
All server behavior — cache paths, Redis URL, API credentials, feature toggles —
is controlled here.
"""

from pydantic_settings import BaseSettings
from pydantic import Field

# Single source of truth for all language metadata
LANGUAGES: dict[str, dict[str, str]] = {
    "en-in": {"standard": "en-IN", "full_name": "english"},
    "hi-in": {"standard": "hi-IN", "full_name": "hindi"},
    "ta-in": {"standard": "ta-IN", "full_name": "tamil"},
    "te-in": {"standard": "te-IN", "full_name": "telugu"},
    "kn-in": {"standard": "kn-IN", "full_name": "kannada"},
    "ml-in": {"standard": "ml-IN", "full_name": "malayalam"},
    "bn-in": {"standard": "bn-IN", "full_name": "bengali"},
    "gu-in": {"standard": "gu-IN", "full_name": "gujarati"},
    "mr-in": {"standard": "mr-IN", "full_name": "marathi"},
    "pa-in": {"standard": "pa-IN", "full_name": "punjabi"},
    "ur-in": {"standard": "ur-IN", "full_name": "urdu"},
    "or-in": {"standard": "or-IN", "full_name": "odia"},
    "as-in": {"standard": "as-IN", "full_name": "assamese"},
}

# Dynamically build standard locales set for exact runtime validation checking
SUPPORTED_LOCALES: set[str] = {
    v["standard"] for v in LANGUAGES.values()
}

# Dynamically build aliases mapping for offline/legacy file parsing
LANGUAGE_ALIASES: dict[str, str] = {}
for k, v in LANGUAGES.items():
    LANGUAGE_ALIASES[v["full_name"]] = v["standard"]
    # Map 2-letter ISO prefix (e.g. "en") to the locale tag
    LANGUAGE_ALIASES[k.split("-")[0]] = v["standard"]


def get_full_language_name(lang: str) -> str:
    """Resolve a standard BCP-47 tag (e.g. 'en-IN') to its full language name (e.g. 'english')."""
    clean_lang = lang.strip().lower()
    if clean_lang in LANGUAGES:
        return LANGUAGES[clean_lang]["full_name"]
    return clean_lang


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

    # --- Azure TTS Settings ---
    azure_speech_key: str | None = Field(
        default=None,
        description="Azure Speech subscription key",
    )
    azure_speech_region: str = Field(
        default="centralindia",
        description="Azure Speech service region",
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
