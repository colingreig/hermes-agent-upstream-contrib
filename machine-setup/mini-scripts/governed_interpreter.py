#!/usr/bin/env python3
"""Select a physically governed release interpreter without mutable pointers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from typing import Callable

SELECTOR_CONTRACT = "release-python-pyyaml-onepassword-v1"
_RELEASE_NAME = re.compile(r"^v(?P<version>[0-9][0-9A-Za-z.!+_-]*)-(?P<commit>[0-9a-f]{12,64})$")
_UV_RUNTIME = re.compile(r"^cpython-[0-9]+(?:\.[0-9]+){1,2}-[A-Za-z0-9_.-]+$")
_FRAMEWORK_HOME = re.compile(r"^/Library/Frameworks/Python\.framework/Versions/[^/]+/bin$")


class InterpreterSelectionError(RuntimeError):
    pass


def _version_key(name: str) -> tuple[tuple[int, object], ...]:
    match = _RELEASE_NAME.fullmatch(name)
    if match is None:
        raise ValueError(name)
    return tuple(
        (1, int(token)) if token.isdigit() else (0, token.lower())
        for token in re.findall(r"[0-9]+|[^0-9]+", match.group("version"))
    )


def _has_required_modules(candidate: Path) -> bool:
    """Prove this executable is Python in the expected venv with local packages."""
    nonce = secrets.token_hex(24)
    venv = candidate.parents[1].absolute()
    program = r'''
import importlib.metadata, json, pathlib, sys
import onepassword, yaml
print(json.dumps({
    "nonce": sys.argv[1],
    "prefix": sys.prefix,
    "executable": sys.executable,
    "yaml_file": yaml.__file__,
    "yaml_version": importlib.metadata.version("PyYAML"),
    "onepassword_file": onepassword.__file__,
    "onepassword_version": importlib.metadata.version("onepassword-sdk"),
}, sort_keys=True))
'''
    try:
        result = subprocess.run(
            [str(candidate), "-I", "-c", program, nonce],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False
        facts = json.loads(result.stdout)
        if not isinstance(facts, dict) or facts.get("nonce") != nonce:
            return False
        if Path(facts.get("prefix", "")).absolute() != venv:
            return False
        executable = Path(facts.get("executable", "")).absolute()
        if executable != candidate.absolute() and executable != (venv / "bin" / candidate.name):
            return False
        for key in ("yaml_version", "onepassword_version"):
            if not isinstance(facts.get(key), str) or not facts[key].strip():
                return False
        for key in ("yaml_file", "onepassword_file"):
            origin = Path(facts.get(key, "")).resolve(strict=True)
            origin.relative_to(venv.resolve(strict=True))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return False
    return True


def _plain_ancestor(path: Path) -> bool:
    """Require every lexical component to exist and not be a symlink."""
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return False
        return True
    except OSError:
        return False


def _read_pyvenv_cfg(venv: Path) -> dict[str, str] | None:
    """Read a physical pyvenv.cfg, rejecting malformed and duplicate metadata."""
    cfg_path = venv / "pyvenv.cfg"
    try:
        if not stat.S_ISREG(cfg_path.lstat().st_mode) or not _plain_ancestor(cfg_path):
            return None
        values: dict[str, str] = {}
        for raw in cfg_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            if "=" not in raw:
                return None
            key, value = (part.strip() for part in raw.split("=", 1))
            key = key.lower()
            if not key or not value or key in values or "\x00" in value:
                return None
            values[key] = value
        home = Path(values.get("home", ""))
        if not home.is_absolute():
            return None
        executable = values.get("executable")
        if executable is not None and not Path(executable).is_absolute():
            return None
        return values
    except (OSError, UnicodeError):
        return None


def _approved_interpreter_home(home: Path) -> bool:
    """Allow immutable OS/package-manager roots and Mini's physical uv runtime."""
    try:
        physical = home.resolve(strict=True)
        if not physical.is_dir() or not _plain_ancestor(physical):
            return False
    except OSError:
        return False
    text = str(physical)
    if text in {"/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"}:
        return True
    if _FRAMEWORK_HOME.fullmatch(text):
        return True
    # The selector itself is launched by the governed Hermes application. Its
    # base interpreter directory is therefore an approved local provenance.
    base = getattr(sys, "_base_executable", sys.executable)
    try:
        if physical == Path(base).resolve(strict=True).parent:
            return True
    except OSError:
        pass
    uv_root = Path.home() / ".local" / "share" / "uv" / "python"
    try:
        relative = physical.relative_to(uv_root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return len(relative.parts) == 2 and _UV_RUNTIME.fullmatch(relative.parts[0]) is not None and relative.parts[1] == "bin"


def _native_python_binary(path: Path) -> bool:
    """Reject script/shim spoofs: the declared base must be a native Python binary."""
    if re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", path.name) is None:
        return False
    try:
        magic = path.open("rb").read(4)
    except OSError:
        return False
    return magic == b"\x7fELF" or magic in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    }


def _venv_provenance_matches(candidate: Path) -> bool:
    cfg = _read_pyvenv_cfg(candidate.parents[1])
    if cfg is None:
        return False
    try:
        home = Path(cfg["home"])
        home_resolved = home.resolve(strict=True)
        target = candidate.resolve(strict=True)
        if not _approved_interpreter_home(home) or target.parent != home_resolved:
            return False
        executable_value = cfg.get("executable")
        if executable_value is not None:
            executable = Path(executable_value)
            mode = executable.resolve(strict=True).lstat().st_mode
            if not stat.S_ISREG(mode) or executable.resolve(strict=True) != target:
                return False
        return (
            stat.S_ISREG(target.lstat().st_mode)
            and os.access(target, os.X_OK)
            and _native_python_binary(target)
        )
    except OSError:
        return False


def _valid_candidate(candidate: Path) -> bool:
    """Accept only a standard venv executable link with validated provenance."""
    try:
        mode = candidate.lstat().st_mode
        if stat.S_ISLNK(mode):
            link = candidate.readlink()
            direct_target = link if link.is_absolute() else candidate.parent / link
            target_mode = direct_target.lstat().st_mode
            if stat.S_ISLNK(target_mode):
                # CPython venv on macOS commonly uses python -> python3.N -> base.
                if direct_target.parent != candidate.parent or not re.fullmatch(
                    r"python3(?:\.[0-9]+)?", direct_target.name
                ):
                    return False
                second_link = direct_target.readlink()
                second_target = second_link if second_link.is_absolute() else direct_target.parent / second_link
                if stat.S_ISLNK(second_target.lstat().st_mode):
                    return False
            elif not stat.S_ISREG(target_mode):
                return False
        elif not stat.S_ISREG(mode):
            return False
        return os.access(candidate, os.X_OK) and _venv_provenance_matches(candidate)
    except OSError:
        return False


def select_governed_interpreter(
    releases: Path, *, probe: Callable[[Path], bool] = _has_required_modules
) -> Path:
    releases = releases.absolute()
    if not _plain_ancestor(releases) or not releases.is_dir():
        raise InterpreterSelectionError("governed releases directory is unavailable or symlinked")
    try:
        children = sorted(
            (child for child in releases.iterdir() if _RELEASE_NAME.fullmatch(child.name)),
            key=lambda child: (_version_key(child.name), child.stat().st_mtime_ns, child.name),
            reverse=True,
        )
    except OSError as exc:
        raise InterpreterSelectionError(f"cannot enumerate governed releases: {exc}") from exc
    releases_resolved = releases.resolve(strict=True)
    for release in children:
        candidate = release / "venv" / "bin" / "python"
        bindir = candidate.parent
        if not _plain_ancestor(bindir):
            continue
        try:
            release_resolved = release.resolve(strict=True)
            release_resolved.relative_to(releases_resolved)
        except (OSError, ValueError):
            continue
        if release_resolved.parent != releases_resolved:
            continue
        if not _valid_candidate(candidate):
            continue
        if probe(candidate):
            return candidate
    raise InterpreterSelectionError(
        "no governed release interpreter with PyYAML and onepassword is available"
    )
