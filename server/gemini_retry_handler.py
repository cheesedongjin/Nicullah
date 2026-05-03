import random
import time
import logging
import os
import queue
import threading

try:
    from google.genai import types
except Exception:  # Allows the local demo server to boot before google-genai is installed.
    class _GenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Types:
        GenerateContentConfig = _GenerateContentConfig

    types = _Types()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GeminiRetryHandler:
    MODELS = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite"
    ]

    def __init__(self, client, sleep_func=time.sleep, request_timeout=None):
        self.client = client
        self.sleep_func = sleep_func
        self.request_timeout = float(
            request_timeout or os.environ.get("WARROOM_GEMINI_REQUEST_TIMEOUT", "30")
        )
        self.primary_retries = int(os.environ.get("WARROOM_GEMINI_PRIMARY_RETRIES", "2"))
        self.fallback_retries = int(os.environ.get("WARROOM_GEMINI_FALLBACK_RETRIES", "2"))
        self.rate_wait_min = float(os.environ.get("WARROOM_GEMINI_RATE_WAIT_MIN", "5"))
        self.rate_wait_max = float(os.environ.get("WARROOM_GEMINI_RATE_WAIT_MAX", "10"))

    def generate_response(
        self,
        history,
        message,
        system_instruction=None,
        temperature=None,
        response_mime_type=None,
    ):
        """
        Attempts to generate a response using the fallback strategy.

        Args:
            history: List of conversation history compatible with google-genai SDK.
            message: The current user message to send.
            system_instruction: Optional system instruction for the model.
            temperature: Optional temperature setting for generation.

        Returns:
            The text response from the model.

        Raises:
            Exception: If all retries fail or a non-retriable error occurs.
        """
        last_exception = None

        config_args = {}
        if system_instruction:
            config_args['system_instruction'] = system_instruction
        if temperature is not None:
            config_args['temperature'] = temperature
        if response_mime_type:
            config_args['response_mime_type'] = response_mime_type

        config = None
        if config_args:
            config = types.GenerateContentConfig(**config_args)

        for model_index, model_name in enumerate(self.MODELS):
            is_last_model = (model_index == len(self.MODELS) - 1)
            max_retries = self.fallback_retries if is_last_model else self.primary_retries

            logger.info(f"Attempting with model: {model_name} (Max retries: {max_retries})")

            for attempt in range(max_retries):
                try:
                    return self._send_message_with_timeout(model_name, history, config, message)

                except Exception as e:
                    last_exception = e
                    if self._is_rate_limit_error(e):
                        logger.warning(f"Rate limit hit on {model_name} (Attempt {attempt + 1}/{max_retries})")

                        if attempt < max_retries - 1:
                            wait_time = random.uniform(self.rate_wait_min, self.rate_wait_max)
                            logger.info(f"Retrying in {wait_time:.2f}s...")
                            self.sleep_func(wait_time)
                        else:
                            logger.warning(f"Exhausted retries for {model_name}")
                    else:
                        logger.error(f"Non-retriable error on {model_name}: {e}")
                        raise e

            if model_index < len(self.MODELS) - 1:
                switch_wait = random.uniform(2, 4)
                logger.info(f"Switching to next model in {switch_wait:.2f}s...")
                self.sleep_func(switch_wait)

        logger.error("All models and retries exhausted.")
        if last_exception:
            raise last_exception
        raise Exception("Failed to generate response after all attempts.")

    def _is_rate_limit_error(self, exception):
        """Checks if the exception is a 429 Rate Limit error."""
        if hasattr(exception, 'status') and exception.status == 429:
            return True
        if hasattr(exception, 'code') and exception.code == 429:
            return True
        msg = str(exception).lower()
        if "429" in msg or "quota" in msg or "rate limit" in msg:
            return True
        return False

    def _send_message_with_timeout(self, model_name, history, config, message):
        result_queue = queue.Queue(maxsize=1)

        def worker():
            try:
                chat = self.client.chats.create(
                    model=model_name,
                    history=history,
                    config=config
                )
                response = chat.send_message(message)
                result_queue.put(("ok", response.text))
            except BaseException as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(self.request_timeout)
        if thread.is_alive():
            raise TimeoutError(
                f"Gemini request timed out after {self.request_timeout:.0f}s on {model_name}"
            )

        status, payload = result_queue.get()
        if status == "error":
            raise payload
        return payload
