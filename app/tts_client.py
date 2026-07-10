"""
Async TTS API client for the TTS Caching Server.

Wraps httpx.AsyncClient with tenacity retry/backoff for calling the external
TTS API. Designed to be created once during app lifespan and shared across
all requests.
"""

import logging
import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from .config import get_settings
from .models import TTSRequest

logger = logging.getLogger(__name__)


class TTSGenerationError(Exception):
    """Raised when the external TTS API fails permanently after all retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TTSClient:
    """Async client for calling the external TTS API.

    Uses httpx.AsyncClient for non-blocking HTTP calls and tenacity for
    automatic retry with exponential backoff on transient failures.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        """Initialize the TTS client.

        Args:
            http_client: A shared httpx.AsyncClient instance.
        """
        self._client = http_client
        self._settings = get_settings()

    async def generate(self, request: TTSRequest) -> bytes:
        """Generate speech audio from text via the external TTS API.

        Calls the external API with retry + exponential backoff for transient
        failures (5xx, timeouts, connection errors). Returns raw audio bytes.

        Args:
            request: The TTS request with text, voice, language, format.

        Returns:
            Raw audio bytes from the external API.

        Raises:
            TTSGenerationError: If the API fails permanently after all retries.
        """
        start_time = time.perf_counter()

        try:
            audio_bytes = await self._call_api_with_retry(request)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "TTS generation complete: %.1fms, %d bytes (text='%s...')",
                elapsed_ms,
                len(audio_bytes),
                request.text[:50],
            )
            return audio_bytes

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "TTS generation FAILED after %.1fms: %s (text='%s...')",
                elapsed_ms,
                str(e),
                request.text[:50],
            )
            if isinstance(e, TTSGenerationError):
                raise
            raise TTSGenerationError(
                f"External TTS API failed: {str(e)}"
            ) from e

    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError)
        ),
        stop=stop_after_attempt(3),  # overridden dynamically in _call_api_with_retry
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_api(self, request: TTSRequest) -> bytes:
        """Make a single API call with retry decoration.

        This method is decorated with tenacity @retry. It handles transient
        errors automatically.

        Args:
            request: The TTS request.

        Returns:
            Raw audio bytes.

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx responses (5xx triggers retry).
            httpx.TimeoutException: On timeout (triggers retry).
            httpx.ConnectError: On connection failure (triggers retry).
        """
        payload = {
            "text": request.text,
            "voice": request.voice,
            "language": request.language,
            "format": request.format,
        }

        response = await self._client.post(
            f"{self._settings.tts_api_base_url}/synthesize",
            json=payload,
            timeout=self._settings.tts_api_timeout,
        )

        # Raise for 4xx/5xx — tenacity retries on 5xx HTTPStatusError
        if response.status_code >= 500:
            response.raise_for_status()
        elif response.status_code >= 400:
            raise TTSGenerationError(
                f"TTS API returned client error: {response.status_code} {response.text}",
                status_code=response.status_code,
            )

        return response.content

    async def _call_api_with_retry(self, request: TTSRequest) -> bytes:
        """Wrapper to apply dynamic retry configuration.

        Reads max_retries from settings and calls the retry-decorated method.

        Args:
            request: The TTS request.

        Returns:
            Raw audio bytes.
        """
        # Update retry config dynamically from settings
        self._call_api.retry.stop = stop_after_attempt(
            self._settings.tts_api_max_retries
        )

        try:
            return await self._call_api(request)
        except httpx.HTTPStatusError as e:
            raise TTSGenerationError(
                f"External TTS API returned {e.response.status_code} after retries",
                status_code=e.response.status_code,
            ) from e
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise TTSGenerationError(
                f"External TTS API unreachable after retries: {str(e)}"
            ) from e
