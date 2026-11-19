from app.utils.exceptions import InvalidInputError, ProviderConnectionError, ProviderError, is_provider_state_error


def is_transient_provider_error(exc: Exception) -> bool:
    """Retry outages, but not lost connections or requests the provider rejected."""
    if isinstance(exc, (ProviderConnectionError, InvalidInputError)):
        return False
    if isinstance(exc, ProviderError):
        return not exc.request_rejected and not is_provider_state_error(exc)
    return True
