"""Test-framework adapters behind one protocol."""

from __future__ import annotations

from harness.adapters.base import TestAdapter
from harness.adapters.pytest_adapter import PytestAdapter
from harness.adapters.stubs import GoAdapter, JestAdapter
from harness.core.errors import BundleInvalidError

_ADAPTERS: dict[str, TestAdapter] = {
    adapter.framework: adapter for adapter in (PytestAdapter(), GoAdapter(), JestAdapter())
}

__all__ = ["GoAdapter", "JestAdapter", "PytestAdapter", "TestAdapter", "get_adapter", "smoke_argv"]


def get_adapter(framework: str) -> TestAdapter:
    """The adapter for a framework, or a typed error naming what is supported."""
    try:
        return _ADAPTERS[framework]
    except KeyError:
        raise BundleInvalidError(
            f"no adapter for test framework {framework!r}.",
            fix=f"Supported: {', '.join(sorted(_ADAPTERS))}.",
        ) from None


def smoke_argv(framework: str) -> list[str]:
    """A cheap command proving the framework's runner is installed."""
    return get_adapter(framework).smoke_argv()
