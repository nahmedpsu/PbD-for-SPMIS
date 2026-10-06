"""Authoritative-source adapters.

An adapter answers a *question* (a check from the sharing matrix) about an identifier and
returns only the keys the matrix allows. Adapters are the one place in the system that handles a
resolved national identifier, and they hold it only for the duration of the call.
"""

from __future__ import annotations

from .base import SourceAdapter, adapter_for

__all__ = ["SourceAdapter", "adapter_for"]
