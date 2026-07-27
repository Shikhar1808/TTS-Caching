"""
Pre-generated audio file lookup for the TTS Caching Server.

Directly checks the pre-generated directory (e.g. TTSPrompt/) for audio files
named after their cache key hashes (e.g. {cache_key}.{ext}).
"""

import logging
from pathlib import Path
from typing import Optional

from .config import get_settings
from .models import TTSRequest

logger = logging.getLogger(__name__)


class PreGeneratedLookup:
    """Pre-generated TTS audio file lookup using hashed filenames.

    Checks the configured directory directly for a file matching the request's
    calculated cache key (e.g. `{directory}/{cache_key}.{format}`).
    """

    def __init__(self, directory: str | None = None) -> None:
        if directory is None:
            directory = get_settings().pre_generated_dir
        self._directory = Path(directory)

    def lookup(self, request: TTSRequest) -> Optional[Path]:
        from .cache import CacheManager

        cache_key = CacheManager.compute_key(request)
        file_path = self._directory / f"{cache_key}.{request.format}"

        if file_path.is_file():
            logger.debug("Pre-generated HIT: %s -> %s", cache_key, file_path.name)
            return file_path

        return None
