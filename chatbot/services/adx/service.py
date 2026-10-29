"""Application-facing ADX service factory."""

from .ports import ADXQueryPort


def default_adx_service() -> ADXQueryPort:
    from chatbot.adapters.defaults import DefaultADXQueryAdapter

    return DefaultADXQueryAdapter()


__all__ = ["default_adx_service"]
