"""
Pre-generated audio file lookup for the TTS Caching Server.

Directly checks the pre-generated directory (e.g. TTSPrompt/) for audio files
named after their cache key hashes (e.g. {cache_key}.{ext}). This provides
a fast O(1) lookup without requiring startup directory scanning or in-memory
indexing.

Legacy filename parsing functions (e.g. _parse_filename) are retained in this
module to support migration/renaming scripts (like scripts/hash_prompts.py).
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from .config import LANGUAGE_ALIASES, get_settings
from .models import TTSRequest

logger = logging.getLogger(__name__)

# Separator between the prompt template and the dynamic variable in filenames
_VARIABLE_SEPARATOR = "___"


def _normalize_language(lang: str) -> str:
    """Normalize a language string to its ISO code.

    Args:
        lang: Language string, either a full name or ISO code.

    Returns:
        The ISO code (e.g. "en"), or the original lowered string if unknown.
    """
    lowered = lang.strip().lower()
    return LANGUAGE_ALIASES.get(lowered, lowered)


def _parse_filename(filename: str) -> Optional[tuple[str, str, str, str]]:
    """Parse a pre-generated audio filename into its components.

    Expected format:
        {language}_{voice}_{text_with_underscores}.{ext}

    Where {text_with_underscores} may contain "___" as a separator between
    template and variable parts. These are joined with a space in the
    reconstructed text.

    Args:
        filename: The filename (without directory path) to parse.

    Returns:
        A tuple (language, voice, text, format) if parseable, None otherwise.

    Examples:
        >>> _parse_filename("english_dipti_am_i_speaking_with___pallavi.wav")
        ("en", "dipti", "am i speaking with pallavi", "wav")
    """
    # Split off the extension
    name, _, ext = filename.rpartition(".")
    if not name or not ext:
        return None

    # Supported audio extensions
    if ext.lower() not in ("wav", "mp3", "ogg", "flac"):
        return None

    # Split into parts: language_voice_rest...
    parts = name.split("_", 2)
    if len(parts) < 3:
        logger.debug("Filename has fewer than 3 underscore-separated parts: %s", filename)
        return None

    raw_language, voice, raw_text = parts

    # Normalize language
    language = _normalize_language(raw_language)

    # Reconstruct the text:
    # 1. Replace the variable separator "___" with a space
    # 2. Replace remaining underscores with spaces
    # 3. Collapse multiple spaces and strip
    text = raw_text.replace(_VARIABLE_SEPARATOR, " ")
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None

    return language, voice, text, ext.lower()


class PreGeneratedLookup:
    """Pre-generated TTS audio file lookup using hashed filenames.

    Checks the configured directory directly for a file matching the request's
    calculated cache key (e.g. `{directory}/{cache_key}.{format}`).

    This avoids indexing overhead on startup and runs in O(1) time.
    """

    def __init__(self, directory: str | None = None) -> None:
        """Initialize PreGeneratedLookup.

        Args:
            directory: Path to the pre-generated audio directory.
                       Defaults to settings.pre_generated_dir.
        """
        if directory is None:
            directory = get_settings().pre_generated_dir

        self._directory = Path(directory)

    def lookup(self, request: TTSRequest) -> Optional[Path]:
        """Check if a pre-generated file exists for this TTS request.

        Computes the cache key and checks if the hashed file exists.

        Args:
            request: The TTS request to look up.

        Returns:
            Absolute Path to the pre-generated file if found, None otherwise.
        """
        from .cache import CacheManager

        cache_key = CacheManager.compute_key(request)
        file_path = self._directory / f"{cache_key}.{request.format}"

        if file_path.exists() and file_path.is_file():
            logger.debug(
                "Pre-generated HIT (hashed): %s → %s",
                cache_key,
                file_path.name,
            )
            return file_path

        return None
"""Module initialization complete."""
