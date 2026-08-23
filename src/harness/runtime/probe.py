"""Host and Docker preflight probes.

Every subprocess call here is an argv list with `shell=False` (invariant 6).
Nothing in this module is interpolated into a shell string, and nothing here
takes untrusted input -- but the discipline starts at the first docker call so
there is no exception to point at later.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from harness.core.checks import Check, CheckReport, CheckStatus
from harness.core.env import api_key, key_problem, mask
from harness.core.errors import ExitCode

# Probes are cheap; a hang means the daemon is wedged, which is itself the answer.
PROBE_TIMEOUT_S = 20

# SWE-Bench Pro instance images are multi-gigabyte and each phase snapshot is a
# further commit, so a nearly-full disk fails deep inside a run rather than up front.
DISK_WARN_GIB = 20.0

# Architectures that can run linux/amd64 images natively.
_AMD64_ALIASES = frozenset({"x86_64", "amd64"})


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one probe subprocess."""

    ok: bool
    stdout: str
    stderr: str
    # True when the binary was not found at all, as opposed to running and failing.
    missing: bool = False
    timed_out: bool = False


def run_probe(argv: list[str], timeout_s: int = PROBE_TIMEOUT_S) -> CommandResult:
    """Run an argv list with no shell and capture its output."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False by construction
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(ok=False, stdout="", stderr="", missing=True)
    except subprocess.TimeoutExpired:
        return CommandResult(ok=False, stdout="", stderr="", timed_out=True)
    return CommandResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
    )


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 11):
        return Check(
            name="python",
            status=CheckStatus.FAIL,
            detail=f"{version} (need >= 3.11)",
            fix="Recreate the environment with `uv sync` so the pinned 3.12 interpreter is used.",
            exit_code=ExitCode.UNEXPECTED,
        )
    return Check(name="python", status=CheckStatus.OK, detail=version)


def _check_docker_cli() -> Check:
    result = run_probe(["docker", "--version"])
    if result.missing:
        return Check(
            name="docker cli",
            status=CheckStatus.FAIL,
            detail="`docker` not found on PATH",
            fix="Install Docker Desktop (macOS) or the docker engine, then re-run `task doctor`.",
            exit_code=ExitCode.DOCKER_UNAVAILABLE,
        )
    if not result.ok:
        return Check(
            name="docker cli",
            status=CheckStatus.FAIL,
            detail=result.stderr or "`docker --version` failed",
            fix="Reinstall the docker CLI; the binary on PATH is not working.",
            exit_code=ExitCode.DOCKER_UNAVAILABLE,
        )
    return Check(name="docker cli", status=CheckStatus.OK, detail=result.stdout)


# Tab-delimited so the daemon can be interrogated in one round trip.
_INFO_FORMAT = "\t".join(
    (
        "{{.ServerVersion}}",
        "{{.Architecture}}",
        "{{.OSType}}",
        "{{.NCPU}}",
        "{{.MemTotal}}",
        "{{.DockerRootDir}}",
    )
)


@dataclass(frozen=True)
class DaemonInfo:
    """What the daemon reports about itself."""

    server_version: str
    architecture: str
    os_type: str
    ncpu: str
    mem_total_bytes: int
    root_dir: str


def probe_daemon() -> tuple[Check, DaemonInfo | None]:
    """Ask the daemon about itself. Returns the check and, on success, its info."""
    result = run_probe(["docker", "info", "--format", _INFO_FORMAT])
    if result.timed_out:
        return (
            Check(
                name="docker daemon",
                status=CheckStatus.FAIL,
                detail=f"`docker info` did not respond within {PROBE_TIMEOUT_S}s",
                fix="Restart the Docker daemon; it is running but not answering.",
                exit_code=ExitCode.DOCKER_UNAVAILABLE,
            ),
            None,
        )
    if result.missing or not result.ok:
        detail = result.stderr.splitlines()[0] if result.stderr else "daemon unreachable"
        return (
            Check(
                name="docker daemon",
                status=CheckStatus.FAIL,
                detail=detail,
                fix="Start Docker Desktop (or `systemctl start docker`) and re-run `task doctor`.",
                exit_code=ExitCode.DOCKER_UNAVAILABLE,
            ),
            None,
        )

    fields = result.stdout.split("\t")
    if len(fields) != 6:
        return (
            Check(
                name="docker daemon",
                status=CheckStatus.FAIL,
                detail=f"unparseable `docker info` output: {result.stdout!r}",
                fix="Check the docker CLI version; the --format contract changed.",
                exit_code=ExitCode.DOCKER_UNAVAILABLE,
            ),
            None,
        )

    server_version, architecture, os_type, ncpu, mem_total, root_dir = fields
    info = DaemonInfo(
        server_version=server_version,
        architecture=architecture,
        os_type=os_type,
        ncpu=ncpu,
        mem_total_bytes=int(mem_total) if mem_total.isdigit() else 0,
        root_dir=root_dir,
    )
    mem_gib = info.mem_total_bytes / 1024**3
    return (
        Check(
            name="docker daemon",
            status=CheckStatus.OK,
            detail=(
                f"server {info.server_version} · {info.os_type}/{info.architecture} · "
                f"{info.ncpu} cpu · {mem_gib:.1f} GiB"
            ),
        ),
        info,
    )


def _check_buildx() -> Check:
    result = run_probe(["docker", "buildx", "version"])
    if result.missing or not result.ok:
        return Check(
            name="docker buildx",
            status=CheckStatus.WARN,
            detail="not available",
            fix=(
                "Install the buildx plugin. Only bundles using `dockerfile`/`recipe` "
                "environments need it; pinned `image` bundles do not."
            ),
        )
    return Check(name="docker buildx", status=CheckStatus.OK, detail=result.stdout.splitlines()[0])


def _check_architecture(info: DaemonInfo | None) -> Check:
    host_arch = platform.machine()
    native_amd64 = host_arch.lower() in _AMD64_ALIASES
    if native_amd64:
        return Check(
            name="architecture",
            status=CheckStatus.OK,
            detail=f"host {host_arch} runs linux/amd64 images natively",
        )
    daemon_arch = f" (daemon reports {info.architecture})" if info else ""
    return Check(
        name="architecture",
        status=CheckStatus.WARN,
        detail=f"host {host_arch}{daemon_arch}: linux/amd64 images run under emulation",
        fix=(
            "Expect several-times-slower test runs. SWE-Bench Pro images are amd64 only; "
            "keep imported-instance timeouts at 600s. The tiny fixture builds natively."
        ),
    )


def _check_disk(info: DaemonInfo | None) -> Check:
    # On Linux the daemon's root dir is the filesystem that fills up. On macOS it is a
    # path inside the VM, so fall back to the host cwd -- the VM's disk image grows there.
    target = Path(info.root_dir) if info and Path(info.root_dir).exists() else Path.cwd()
    usage = shutil.disk_usage(target)
    free_gib = usage.free / 1024**3
    detail = f"{free_gib:.1f} GiB free on {target}"
    if free_gib < DISK_WARN_GIB:
        return Check(
            name="disk",
            status=CheckStatus.WARN,
            detail=detail,
            fix=(
                f"Under {DISK_WARN_GIB:.0f} GiB free. Instance images are multi-gigabyte and "
                "each phase adds a snapshot; run `task gc` or `docker image prune`."
            ),
        )
    return Check(name="disk", status=CheckStatus.OK, detail=detail)


def _check_api_key(dotenv_loaded: bool) -> Check:
    key = api_key()
    if not key:
        return Check(
            name="anthropic api key",
            status=CheckStatus.WARN,
            detail="not set",
            fix=(
                "Copy .env.example to .env and paste your key, or export "
                "ANTHROPIC_API_KEY. Only `--solver agent` needs it -- gold, noop, "
                "replay, and cmd make zero API calls."
            ),
        )

    source = ".env" if dotenv_loaded else "environment"
    problem = key_problem(key)
    if problem:
        # Catch a paste error now, not partway through a rate-limited live run.
        return Check(
            name="anthropic api key",
            status=CheckStatus.WARN,
            detail=f"set from {source}, but it {problem}",
            fix="Re-copy the key from console.anthropic.com with no quotes, no "
            "`export ` prefix, and no trailing whitespace.",
        )
    return Check(
        name="anthropic api key",
        status=CheckStatus.OK,
        detail=f"{mask(key)} (from {source})",
    )


def collect_doctor_report(*, dotenv_loaded: bool = False) -> CheckReport:
    """Run every preflight probe.

    Checks are appended in dependency order so the first failure is the root
    cause: a missing docker CLI decides the exit code before the daemon probe
    gets a chance to fail for the same underlying reason.
    """
    checks: list[Check] = [_check_python()]

    cli_check = _check_docker_cli()
    checks.append(cli_check)

    info: DaemonInfo | None = None
    if cli_check.status is CheckStatus.OK:
        daemon_check, info = probe_daemon()
        checks.append(daemon_check)
        checks.append(_check_buildx())

    checks.append(_check_architecture(info))
    checks.append(_check_disk(info))
    checks.append(_check_api_key(dotenv_loaded))
    return CheckReport(checks=checks)
