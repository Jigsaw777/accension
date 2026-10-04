"""Public, secret-safe failures. Policy never depends on provider error text."""
from __future__ import annotations

import asyncio
import httpx


class ProviderError(RuntimeError):
    code = "PROVIDER_FAILURE"
    retryable = False


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"
    retryable = True


class AuthenticationRequired(ProviderError):
    code = "AUTH_REQUIRED"


class RateLimited(ProviderError):
    code = "RATE_LIMITED"
    retryable = True

    def __init__(self, message="Provider rate limit reached", retry_after=60.0):
        super().__init__(message)
        self.retry_after = max(0.0, min(float(retry_after), 86400.0))


class CapabilityUnsupported(ProviderError):
    code = "CAPABILITY_UNSUPPORTED"


class ModelUnavailable(ProviderError):
    code = "MODEL_UNAVAILABLE"
    retryable = True


class ContextOverflow(ProviderError):
    code = "CONTEXT_OVERFLOW"


class PrivacyViolation(ProviderError):
    code = "PRIVACY_VIOLATION"


class InvalidStructuredOutput(ProviderError, ValueError):
    code = "INVALID_STRUCTURED_OUTPUT"


class ValidationFailed(ProviderError):
    code = "VALIDATION_FAILED"


def normalize_error(exc: Exception) -> ProviderError:
    """Never preserve response bodies, headers, URLs, credentials or SDK messages."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return AuthenticationRequired("Provider authentication or access required")
        if status == 429:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            raw = exc.response.headers.get("retry-after", "60")
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 60
            return RateLimited(retry_after=delay)
        if status == 404:
            return ModelUnavailable("Provider model or endpoint unavailable")
        if status in (400, 422):
            return CapabilityUnsupported("Provider rejected request parameters")
        return ProviderUnavailable("Provider HTTP request failed")
    if isinstance(exc, (httpx.TransportError, asyncio.TimeoutError, ConnectionError)):
        return ProviderUnavailable("Provider network request timed out or failed")
    # SDK error codes are structured data; never parse human-readable messages.
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    if code in {"ExpiredTokenException", "UnrecognizedClientException", "AccessDeniedException", "InvalidSignatureException"} or type(exc).__name__ in {"NoCredentialsError", "PartialCredentialsError", "RefreshError", "DefaultCredentialsError"}:
        return AuthenticationRequired("Provider credentials expired, missing or access denied")
    if code in {"ThrottlingException", "TooManyRequestsException"}:
        return RateLimited()
    if code in {"ResourceNotFoundException", "ModelNotReadyException"}:
        return ModelUnavailable("Provider model unavailable")
    if code == "ValidationException":
        return CapabilityUnsupported("Provider rejected request parameters")
    return ProviderUnavailable("Provider call failed (" + type(exc).__name__ + ")")
