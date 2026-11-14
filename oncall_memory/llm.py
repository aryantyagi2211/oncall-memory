import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any
from groq import Groq
from .config import GROQ_API_KEY, PRIMARY_MODEL, FALLBACK_MODEL, GROQ_TOTAL_DEADLINE_SECONDS, GROQ_TRANSIENT_RETRIES, GROQ_RETRY_DELAY_SECONDS, GROQ_EMPTY_RETRIES, GROQ_MAX_COMPLETION_TOKENS
logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4)

class GroqCompletionError(RuntimeError):
    """Raised when configured Groq models cannot complete a request."""
    pass

def groq_error_details(exc: Exception) -> tuple[Any, str]:
    """Groq error details."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status, str(exc)

def is_groq_timeout(exc: Exception) -> bool:
    """Is groq timeout."""
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()

def is_reasoning_effort_rejection(exc: Exception) -> bool:
    """Is reasoning effort rejection."""
    status, message = groq_error_details(exc)
    text = message.lower()
    return status == 400 and "reasoning_effort" in text and any(
        phrase in text
        for phrase in ("unsupported", "not supported", "unknown", "unrecognized", "invalid")
    )

def is_transient_groq_error(exc: Exception) -> bool:
    """Is transient groq error."""
    status, _ = groq_error_details(exc)
    return status == 429 or is_groq_timeout(exc)

def complete_with_model(client: Groq, model: str, messages: list[dict[str, str]]) -> str:
    """Complete with model."""
    transient_retries = 0
    empty_retries = 0
    include_reasoning_effort = True

    while True:
        options: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": GROQ_MAX_COMPLETION_TOKENS,
        }
        if include_reasoning_effort:
            options["reasoning_effort"] = "low"
        try:
            response = client.chat.completions.create(**options)
        except Exception as exc:
            if include_reasoning_effort and is_reasoning_effort_rejection(exc):
                status, message = groq_error_details(exc)
                logger.warning(
                    "Groq model %s rejected reasoning_effort (status=%s): %s; retrying without it",
                    model,
                    status,
                    message,
                )
                include_reasoning_effort = False
                continue

            status, message = groq_error_details(exc)
            logger.error(
                "Groq model %s failed (status=%s): %s",
                model,
                status,
                message,
                exc_info=True,
            )
            if is_transient_groq_error(exc) and transient_retries < GROQ_TRANSIENT_RETRIES:
                transient_retries += 1
                time.sleep(GROQ_RETRY_DELAY_SECONDS)
                continue
            raise

        answer = (
            response.choices[0].message.content
            if response.choices
            else None
        )
        if isinstance(answer, str) and answer.strip():
            return answer.strip()

        status = getattr(response, "status_code", None)
        logger.error(
            "Groq model %s returned empty or whitespace content (status=%s): %r",
            model,
            status,
            answer,
        )
        if empty_retries < GROQ_EMPTY_RETRIES:
            empty_retries += 1
            continue
        raise RuntimeError(f"Groq model {model} returned empty content twice.")

def groq_completion(messages: list[dict[str, str]]) -> str:
    """Groq completion."""
    if not GROQ_API_KEY:
        exc = GroqCompletionError("GROQ_API_KEY is not configured.")
        logger.error("Groq request failed (status=not available): %s", exc)
        raise exc

    try:
        client = Groq(api_key=GROQ_API_KEY)
    except Exception as exc:
        status, message = groq_error_details(exc)
        logger.error(
            "Groq client initialization failed (status=%s): %s",
            status,
            message,
            exc_info=True,
        )
        raise GroqCompletionError(f"Groq client initialization failed: {message}") from exc
    def complete_all() -> str:
        errors = []
        for model in (PRIMARY_MODEL, FALLBACK_MODEL):
            try:
                return complete_with_model(client, model, messages)
            except Exception as exc:
                status, message = groq_error_details(exc)
                errors.append(f"{model} (status={status}): {message}")
                logger.error("Groq model %s exhausted: status=%s message=%s", model, status, message)
        raise GroqCompletionError("; ".join(errors))

    future = _executor.submit(complete_all)
    try:
        return future.result(timeout=GROQ_TOTAL_DEADLINE_SECONDS)
    except FutureTimeout as exc:
        future.cancel()
        raise GroqCompletionError(
            f"Groq request exceeded the {GROQ_TOTAL_DEADLINE_SECONDS}s total deadline."
        ) from exc
