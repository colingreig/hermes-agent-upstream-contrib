#!/usr/bin/env python3
"""Fail-closed isolation primitives for the PR trust boundary.

This module deliberately has no dependency on the PR reconciler.  It accepts a
small immutable candidate identity and exposes plans/results that the
reconciler can persist in its fenced ledger.  Nothing in here executes by
default: callers must explicitly request a real checkout or sandbox run.

The candidate checkout fetches only the immutable base, head, and GitHub's
synthetic ``refs/pull/<number>/merge`` object into a newly-created temporary
repository.  PR code is never sourced, and test commands run only in a Linux
``bwrap`` sandbox with a network namespace (therefore no network).  A missing
sandbox backend is a blocked result, not a host-execution fallback.
"""
from __future__ import annotations

import json
import math
import os
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
_MAX_EVIDENCE_BYTES = 8 * 1024


class CandidateValidationError(ValueError):
    """Raised before a candidate can reach a filesystem or subprocess call."""


def quote_evidence(value: object, *, max_bytes: int = _MAX_EVIDENCE_BYTES) -> str:
    """Return untrusted material as bounded, JSON-quoted evidence.

    PR titles, branch names, process output, and remote error text are all
    attacker-controlled.  Keeping the JSON quotes prevents an evidence line
    from becoming executable-looking instructions in a later log consumer.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    raw = str(value).encode("utf-8", "replace")
    truncated = len(raw) > max_bytes
    if truncated:
        marker = b"...[truncated]"
        if len(marker) > max_bytes:
            marker = b"..."[:max_bytes]
        raw = raw[:max_bytes - len(marker)] + marker
    text = raw.decode("utf-8", "replace")
    return json.dumps(text, ensure_ascii=True)


@dataclass(frozen=True)
class CandidateIdentity:
    """The immutable identity shared by review and merge stages.

    These are object IDs, rather than branch names, so a later force-push or
    base advance cannot silently change the code a verdict covers.
    """

    repository: str
    pull_number: int
    base_sha: str
    head_sha: str
    tested_merge_sha: str
    base_ref: str = "main"

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.fullmatch(self.repository):
            raise CandidateValidationError("repository must be an owner/name identifier")
        if not isinstance(self.pull_number, int) or isinstance(self.pull_number, bool) or self.pull_number <= 0:
            raise CandidateValidationError("pull_number must be a positive integer")
        for field_name in ("base_sha", "head_sha", "tested_merge_sha"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
                raise CandidateValidationError(f"{field_name} must be a lowercase full object id")
        if not isinstance(self.base_ref, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", self.base_ref):
            raise CandidateValidationError("base_ref contains unsupported characters")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateIdentity":
        """Adapt the core resolver's immutable mapping without importing it."""
        try:
            repository = value.get("repository") or value["canonical_repo"]
            pull_number = value.get("pull_number") or value["pr_number"]
            return cls(
                repository=str(repository),
                pull_number=pull_number,
                base_sha=str(value["base_sha"]),
                head_sha=str(value["head_sha"]),
                tested_merge_sha=str(value["tested_merge_sha"]),
                base_ref=str(value.get("base_ref", "main")),
            )
        except (KeyError, TypeError) as exc:
            raise CandidateValidationError("candidate mapping is missing a required identity field") from exc

    def evidence(self) -> str:
        return json.dumps(
            {
                "repository": self.repository,
                "pull_number": self.pull_number,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "tested_merge_sha": self.tested_merge_sha,
                "base_ref": self.base_ref,
            },
            ensure_ascii=True,
            sort_keys=True,
        )


def coerce_candidate(value: CandidateIdentity | Mapping[str, Any] | object) -> CandidateIdentity:
    """Adapt the core candidate contract lazily, without hard-coupling imports.

    The reconciler may supply its frozen dataclass instead of a mapping.  Only
    the explicit identity fields are read; arbitrary objects and branch/ref
    strings are never used as commands.
    """
    if isinstance(value, CandidateIdentity):
        return value
    if isinstance(value, Mapping):
        return CandidateIdentity.from_mapping(value)
    fields = ("base_sha", "head_sha", "tested_merge_sha")
    repository = getattr(value, "repository", getattr(value, "canonical_repo", None))
    pull_number = getattr(value, "pull_number", getattr(value, "pr_number", None))
    if repository is not None and pull_number is not None and all(hasattr(value, field_name) for field_name in fields):
        return CandidateIdentity(
            repository=str(repository),
            pull_number=pull_number,
            base_sha=str(getattr(value, "base_sha")),
            head_sha=str(getattr(value, "head_sha")),
            tested_merge_sha=str(getattr(value, "tested_merge_sha")),
            base_ref=str(getattr(value, "base_ref", "main")),
        )
    raise CandidateValidationError("candidate does not satisfy the explicit identity contract")


@dataclass(frozen=True)
class SandboxLimits:
    """Explicit resource ceilings; unlimited execution is not representable."""

    cpu_seconds: float = 120.0
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    wall_seconds: float = 180.0
    output_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.cpu_seconds <= 0 or self.wall_seconds <= 0:
            raise ValueError("CPU and wall-time limits must be positive")
        if self.memory_bytes <= 0 or self.output_bytes <= 0:
            raise ValueError("memory and output limits must be positive")


@dataclass(frozen=True)
class SandboxResult:
    """A bounded evidence-only result from a sandbox attempt."""

    status: str
    command: tuple[str, ...]
    returncode: int | None = None
    stdout_evidence: str = '""'
    stderr_evidence: str = '""'
    reason_evidence: str = '""'
    elapsed_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True)
class CandidateCheckoutPlan:
    """Auditable commands for one exact, disposable candidate checkout."""

    candidate: CandidateIdentity
    fetch_ref: str
    commands: tuple[tuple[str, ...], ...]

    def evidence(self) -> str:
        return json.dumps(
            {
                "candidate": json.loads(self.candidate.evidence()),
                "fetch_ref": self.fetch_ref,
                "commands": [list(command) for command in self.commands],
            },
            ensure_ascii=True,
            sort_keys=True,
        )


@dataclass
class CandidateCheckout:
    """A temporary checkout that must be cleaned up by its owner."""

    plan: CandidateCheckoutPlan
    workspace: Path | None = None
    status: str = "shadow"
    reason_evidence: str = '""'
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.workspace is not None

    def cleanup(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None
            self.workspace = None

    def __enter__(self) -> "CandidateCheckout":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.cleanup()


def _minimal_git_environment(home: Path) -> dict[str, str]:
    """Use a fresh HOME and no global/system Git config or credential prompts."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GCM_INTERACTIVE": "Never",
    }


class CandidateCheckoutRunner:
    """Materialize only GitHub's exact synthetic merge in a disposable clone.

    ``execute`` and ``allow_network_fetch`` are independently opt-in.  The
    latter exists because the test sandbox itself has no network; the fetch is
    a tightly-scoped, unauthenticated ingress phase, never a fallback to a
    persistent developer checkout.
    """

    def __init__(self, *, git_binary: str = "git", temp_parent: Path | None = None, timeout_seconds: float = 90.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.git_binary = git_binary
        self.temp_parent = temp_parent
        self.timeout_seconds = timeout_seconds

    def plan(self, candidate: CandidateIdentity | Mapping[str, Any] | object) -> CandidateCheckoutPlan:
        identity = coerce_candidate(candidate)
        merge_ref = f"refs/pull/{identity.pull_number}/merge"
        local_merge_ref = f"refs/remotes/origin/pr/{identity.pull_number}/merge"
        remote = f"https://github.com/{identity.repository}.git"
        return CandidateCheckoutPlan(
            candidate=identity,
            fetch_ref=merge_ref,
            commands=(
                (self.git_binary, "init", "--quiet", "."),
                (self.git_binary, "remote", "add", "origin", remote),
                (
                    self.git_binary,
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    identity.base_sha,
                    identity.head_sha,
                    f"{merge_ref}:{local_merge_ref}",
                ),
                (self.git_binary, "rev-parse", local_merge_ref),
                (self.git_binary, "show", "-s", "--format=%P", identity.tested_merge_sha),
                (self.git_binary, "checkout", "--detach", "--quiet", identity.tested_merge_sha),
            ),
        )

    def materialize(
        self,
        candidate: CandidateIdentity | Mapping[str, Any] | object,
        *,
        execute: bool = False,
        allow_network_fetch: bool = False,
    ) -> CandidateCheckout:
        plan = self.plan(candidate)
        if not execute:
            return CandidateCheckout(plan=plan, status="shadow", reason_evidence=quote_evidence("checkout execution disabled"))
        if not allow_network_fetch:
            return CandidateCheckout(plan=plan, status="blocked", reason_evidence=quote_evidence("network fetch not explicitly enabled"))

        try:
            temporary_directory = tempfile.TemporaryDirectory(prefix="hermes-pr-candidate-", dir=self.temp_parent)
            workspace = Path(temporary_directory.name).resolve()
            home = workspace / ".sandbox-home"
            (home / "tmp").mkdir(parents=True, mode=0o700)
            environment = _minimal_git_environment(home)
            for command in plan.commands[:3]:
                self._checked(command, workspace, environment)
            actual_merge = self._checked(plan.commands[3], workspace, environment).strip()
            if actual_merge != plan.candidate.tested_merge_sha:
                raise RuntimeError("fetched merge object did not equal the requested tested merge object")
            parents = self._checked(plan.commands[4], workspace, environment).split()
            if parents != [plan.candidate.base_sha, plan.candidate.head_sha]:
                raise RuntimeError("tested merge object did not have the expected base/head parents")
            self._checked(plan.commands[5], workspace, environment)
            return CandidateCheckout(plan=plan, workspace=workspace, status="ready", _temporary_directory=temporary_directory)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            try:
                temporary_directory.cleanup()  # type: ignore[name-defined]
            except (NameError, OSError):
                pass
            return CandidateCheckout(plan=plan, status="blocked", reason_evidence=quote_evidence(exc))

    def _checked(self, command: Sequence[str], cwd: Path, environment: Mapping[str, str]) -> str:
        completed = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "git command failed"
            raise RuntimeError(detail[:_MAX_EVIDENCE_BYTES])
        return completed.stdout


class SandboxRunner:
    """Run a trusted test argv inside bwrap with network denied by default."""

    def __init__(self, *, bwrap_binary: str = "bwrap", limits: SandboxLimits | None = None):
        self.bwrap_binary = bwrap_binary
        self.limits = limits or SandboxLimits()

    def command_for(self, command: Sequence[str], workspace: Path) -> tuple[str, ...]:
        """Build the only supported execution envelope (no shell involved)."""
        argv = _validate_argv(command)
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise ValueError("sandbox workspace must be an existing directory")
        bwrap = shutil.which(self.bwrap_binary)
        if bwrap is None:
            raise FileNotFoundError("bubblewrap is required for sandbox execution")

        args: list[str] = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--clearenv"]
        # System executables are mounted read-only.  Deliberately omit /home,
        # /Users, the caller's repository, sockets, and credential locations.
        for host_path in ("/usr", "/bin", "/lib", "/lib64", "/etc/ssl/certs"):
            path = Path(host_path)
            if path.exists():
                args.extend(("--ro-bind", host_path, host_path))
        args.extend(("--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"))
        args.extend(("--bind", str(workspace), "/work", "--dir", "/work/.home"))
        for key, value in (
            ("HOME", "/work/.home"),
            ("TMPDIR", "/tmp"),
            ("PATH", "/usr/bin:/bin"),
            ("LANG", "C.UTF-8"),
            ("LC_ALL", "C.UTF-8"),
            ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_CONFIG_GLOBAL", "/dev/null"),
            ("GIT_TERMINAL_PROMPT", "0"),
        ):
            args.extend(("--setenv", key, value))
        args.extend(("--chdir", "/work", "--"))
        args.extend(argv)
        return tuple(args)

    def run(self, command: Sequence[str], checkout: CandidateCheckout, *, execute: bool = False) -> SandboxResult:
        argv = _validate_argv(command)
        if not execute:
            return SandboxResult("shadow", argv, reason_evidence=quote_evidence("sandbox execution disabled"))
        # A bare path is deliberately not accepted.  It could be a developer's
        # checkout or another persistent workspace.  Only the checkout runner
        # can construct a live CandidateCheckout backed by TemporaryDirectory.
        if not isinstance(checkout, CandidateCheckout) or not checkout.ready or checkout._temporary_directory is None:
            return SandboxResult("blocked", argv, reason_evidence=quote_evidence("sandbox requires a live disposable candidate checkout"))
        assert checkout.workspace is not None
        try:
            envelope = self.command_for(argv, checkout.workspace)
        except (OSError, ValueError) as exc:
            return SandboxResult("blocked", argv, reason_evidence=quote_evidence(exc))
        return self._run_bounded(envelope, argv, checkout.workspace)

    def _run_bounded(self, envelope: tuple[str, ...], original: tuple[str, ...], workspace: Path) -> SandboxResult:
        started = time.monotonic()
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        try:
            process = subprocess.Popen(
                envelope,
                cwd=workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=lambda: _apply_resource_limits(self.limits),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return SandboxResult("blocked", original, reason_evidence=quote_evidence(exc), elapsed_seconds=time.monotonic() - started)

        stdout, stderr = bytearray(), bytearray()
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        status: str | None = None
        reason = ""
        try:
            while selector.get_map():
                remaining = self.limits.wall_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    status, reason = "timed_out", "wall-time limit exceeded"
                    _terminate_process_group(process)
                    break
                for key, _mask in selector.select(timeout=min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), min(64 * 1024, self.limits.output_bytes + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target: bytearray = key.data
                    remaining_output = self.limits.output_bytes - len(stdout) - len(stderr)
                    if remaining_output <= 0 or len(chunk) > remaining_output:
                        target.extend(chunk[:max(0, remaining_output)])
                        status, reason = "output_limited", "combined stdout/stderr limit exceeded"
                        _terminate_process_group(process)
                        break
                    target.extend(chunk)
                if status is not None:
                    break
            if status is not None:
                process.wait(timeout=5)
            else:
                process.wait(timeout=max(1.0, self.limits.wall_seconds - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            status, reason = "timed_out", "wall-time limit exceeded"
            _terminate_process_group(process)
            process.wait(timeout=5)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

        elapsed = time.monotonic() - started
        if status is None:
            status = "passed" if process.returncode == 0 else "failed"
            reason = "" if status == "passed" else "sandboxed command returned non-zero"
        return SandboxResult(
            status=status,
            command=original,
            returncode=process.returncode,
            stdout_evidence=quote_evidence(bytes(stdout).decode("utf-8", "replace")),
            stderr_evidence=quote_evidence(bytes(stderr).decode("utf-8", "replace")),
            reason_evidence=quote_evidence(reason),
            elapsed_seconds=elapsed,
        )


def _validate_argv(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("command must be a non-empty argv sequence")
    argv = tuple(command)
    if any(not isinstance(part, str) or not part or "\x00" in part for part in argv):
        raise ValueError("command arguments must be non-empty strings without NUL bytes")
    return argv


def _apply_resource_limits(limits: SandboxLimits) -> None:
    """Child-only resource limits, in addition to the parent's wall/output caps."""
    resource.setrlimit(resource.RLIMIT_CPU, (max(1, math.ceil(limits.cpu_seconds)), max(1, math.ceil(limits.cpu_seconds))))
    # macOS starts a Python child with a very large virtual address map; lowering
    # RLIMIT_AS below that map fails before exec.  RLIMIT_DATA is the supported
    # heap/data ceiling there.  Linux uses RLIMIT_AS, which covers mappings too.
    memory_limit = resource.RLIMIT_DATA if sys.platform == "darwin" else resource.RLIMIT_AS
    resource.setrlimit(memory_limit, (limits.memory_bytes, limits.memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.output_bytes, limits.output_bytes))
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError):
        process.kill()
