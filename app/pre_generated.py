"""
Pre-generated audio file lookup for the TTS Caching Server.

Parses filenames in the pre-generated directory (e.g. TTSPrompt/) into
(language, voice, text) tuples, builds an in-memory index keyed by cache
key, and provides a fast lookup to serve audio without calling the external
TTS API.

Filename convention expected:
    {language}_{voice}_{prompt_text}___{dynamic_variable}.{ext}
    OR
    {language}_{voice}_{full_text}.{ext}

Examples:
    english_dipti_am_i_speaking_with___a._abdul_rahaman.wav
    english_dipti_am_i_speaking_with___pallavi.wav
    english_dipti_am_i_speaking_with___.wav  (no variable — just the template)
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
    """In-memory index of pre-generated TTS audio files.

    On initialization, scans the configured directory, parses each filename,
    computes the cache key using the same formula as CacheManager, and stores
    a mapping from cache_key → file path.

    This allows the /tts endpoint to serve pre-generated audio with zero
    external API calls.
    """

    def __init__(self, directory: str | None = None) -> None:
        """Initialize and build the lookup index.

        Args:
            directory: Path to the pre-generated audio directory.
                       Defaults to settings.pre_generated_dir.
        """
        if directory is None:
            directory = get_settings().pre_generated_dir

        self._directory = Path(directory)
        # Maps cache_key → absolute file path
        self._index: dict[str, Path] = {}
        # Maps cache_key → (language, voice, text, format) for metadata
        self._metadata: dict[str, tuple[str, str, str, str]] = {}

        self._build_index()

    @property
    def size(self) -> int:
        """Number of indexed pre-generated files."""
        return len(self._index)

    def _build_index(self) -> None:
        """Scan the directory and index all parseable audio files."""
        if not self._directory.exists():
            logger.warning(
                "Pre-generated directory does not exist: %s",
                self._directory,
            )
            return

        if not self._directory.is_dir():
            logger.warning(
                "Pre-generated path is not a directory: %s",
                self._directory,
            )
            return

        # Import here to avoid circular imports
        from .cache import CacheManager

        indexed = 0
        skipped = 0

        for entry in os.scandir(self._directory):
            if not entry.is_file():
                continue

            parsed = _parse_filename(entry.name)
            if parsed is None:
                skipped += 1
                continue

            language, voice, text, fmt = parsed

            # Build a TTSRequest to compute the cache key
            # (uses the same hash formula as the main cache)
            try:
                request = TTSRequest(
                    text=text,
                    voice=voice,
                    language=language,
                    format=fmt,
                )
            except Exception:
                skipped += 1
                continue

            cache_key = CacheManager.compute_key(request)
            file_path = Path(entry.path).resolve()

            self._index[cache_key] = file_path
            self._metadata[cache_key] = (language, voice, text, fmt)
            indexed += 1

        logger.info(
            "Pre-generated index built: %d files indexed, %d skipped (dir: %s)",
            indexed,
            skipped,
            self._directory,
        )

    def lookup(self, request: TTSRequest) -> Optional[Path]:
        """Check if a pre-generated file exists for this TTS request.

        Computes the cache key and checks the in-memory index.

        Args:
            request: The TTS request to look up.

        Returns:
            Absolute Path to the pre-generated file if found, None otherwise.
        """
        from .cache import CacheManager

        cache_key = CacheManager.compute_key(request)
        path = self._index.get(cache_key)

        if path is not None:
            # Verify the file still exists on disk
            if path.exists():
                logger.debug(
                    "Pre-generated HIT: %s → %s",
                    cache_key,
                    path.name,
                )
                return path
            else:
                logger.warning(
                    "Pre-generated file missing from disk: %s (was: %s)",
                    cache_key,
                    path,
                )
                # Remove stale entry
                del self._index[cache_key]
                del self._metadata[cache_key]

        return None

    def get_metadata(self, cache_key: str) -> Optional[tuple[str, str, str, str]]:
        """Get the parsed metadata for a pre-generated cache key.

        Args:
            cache_key: The hex hash to look up.

        Returns:
            Tuple of (language, voice, text, format) if found, None otherwise.
        """
        return self._metadata.get(cache_key)

    def get_all_entries(self) -> list[tuple[str, Path, tuple[str, str, str, str]]]:
        """Get all indexed entries for diagnostics.

        Returns:
            List of (cache_key, path, (language, voice, text, format)) tuples.
        """
        return [
            (key, self._index[key], self._metadata[key])
            for key in self._index
        ]
"""Module initialization complete."""
