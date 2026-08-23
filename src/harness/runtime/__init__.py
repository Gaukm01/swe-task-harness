"""Implementations of the `ContainerRuntime` protocol declared in `core.runtime`.

`DockerRuntime` drives the docker CLI with argv lists; `FakeRuntime` records
calls in memory for unit tests. Nothing in `core` imports this package.
"""

from __future__ import annotations

from harness.runtime.docker import DockerRuntime
from harness.runtime.fake import FakeRuntime, argv_contains

__all__ = ["DockerRuntime", "FakeRuntime", "argv_contains"]
