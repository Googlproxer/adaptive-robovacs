"""Vacuum adapter registry and built-in adapters."""

from .registry import adapter_for_id, async_resolve_adapter

__all__ = ["adapter_for_id", "async_resolve_adapter"]
