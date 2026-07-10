"""
Async TTS API client for the TTS Caching Server using Azure Speech SDK.

Calls the Azure Speech SDK directly on a cache miss. To prevent blocking
the FastAPI event loop, blocking operations are executed in a separate
thread pool via asyncio.to_thread.
"""

import asyncio
import logging
import time

import azure.cognitiveservices.speech as speechsdk
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


class TransientAzureTTSError(TTSGenerationError):
    """Raised when a transient error occurs during Azure TTS synthesis (e.g. connection error)."""
    pass


class TTSClient:
    """Async client for calling Azure TTS SDK.

    Executes blocking Azure Speech SDK calls inside asyncio.to_thread and
    uses tenacity for automatic retry with exponential backoff on transient failures.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        """Initialize the TTS client.

        Args:
            http_client: A shared httpx.AsyncClient instance (retained for signature compatibility).
        """
        self._client = http_client
        self._settings = get_settings()

    def _synthesize_azure(self, request: TTSRequest) -> bytes:
        """Synchronous helper to run the Azure Speech SDK synthesis.

        Executed in a background thread via asyncio.to_thread.
        """
        if not self._settings.azure_speech_key:
            raise TTSGenerationError(
                "Azure Speech API key is not configured. Please set AZURE_SPEECH_KEY in .env."
            )

        speech_config = speechsdk.SpeechConfig(
            subscription=self._settings.azure_speech_key,
            region=self._settings.azure_speech_region,
        )

        # Map synthesis output format. Currently configured to 8kHz mono PCM WAV.
        if request.format == "wav":
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff8Khz16BitMonoPcm
            )

        speech_config.speech_synthesis_language = request.language
        speech_config.speech_synthesis_voice_name = request.voice

        # Synthesize in-memory (audio_config=None)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
        result = synthesizer.speak_text_async(request.text).get()

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return result.audio_data

        elif result.reason == speechsdk.ResultReason.Canceled:
            cancellation_details = speechsdk.CancellationDetails(result)
            error_details = cancellation_details.error_details or "Unknown cancellation error"

            # Check if it is a transient error to trigger tenacity retries
            if cancellation_details.reason == speechsdk.CancellationReason.Error:
                transient_codes = (
                    speechsdk.CancellationErrorCode.ConnectionFailure,
                    speechsdk.CancellationErrorCode.ServiceTimeout,
                    speechsdk.CancellationErrorCode.ServiceUnavailable,
                )
                if cancellation_details.error_code in transient_codes:
                    raise TransientAzureTTSError(
                        f"Transient Azure TTS error (code={cancellation_details.error_code}): {error_details}"
                    )

            raise TTSGenerationError(
                f"Azure TTS failed: {cancellation_details.reason} (code={cancellation_details.error_code}): {error_details}"
            )

        else:
            raise TTSGenerationError(f"Azure TTS failed with unexpected reason: {result.reason}")

    async def generate(self, request: TTSRequest) -> bytes:
        """Generate speech audio from text via Azure TTS SDK.

        Args:
            request: The TTS request with text, voice, language, format.

        Returns:
            Raw audio bytes from the Azure Speech SDK.

        Raises:
            TTSGenerationError: If the synthesis fails permanently after all retries.
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
                f"Azure TTS direct integration failed: {str(e)}"
            ) from e

    @retry(
        retry=retry_if_exception_type(TransientAzureTTSError),
        stop=stop_after_attempt(3),  # overridden dynamically in _call_api_with_retry
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_api_azure(self, request: TTSRequest) -> bytes:
        """Call the synchronous Azure synthesis helper in a separate thread.

        Tenacity retries transient exceptions.
        """
        return await asyncio.to_thread(self._synthesize_azure, request)

    async def _call_api_with_retry(self, request: TTSRequest) -> bytes:
        """Wrapper to apply dynamic retry configuration.

        Reads max_retries from settings and calls the retry-decorated method.

        Args:
            request: The TTS request.

        Returns:
            Raw audio bytes.
        """
        # Update retry config dynamically from settings
        self._call_api_azure.retry.stop = stop_after_attempt(
            self._settings.tts_api_max_retries
        )

        try:
            return await self._call_api_azure(request)
        except TransientAzureTTSError as e:
            raise TTSGenerationError(
                f"Azure TTS failed after retries: {str(e)}"
            ) from e
