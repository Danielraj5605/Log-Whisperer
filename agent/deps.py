"""Dependency checker — detect missing deps and optionally install them."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from agent.procs import child_environment
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Literal

from agent.detect import detect_package_manager

PackageManager = Literal["npm", "yarn", "pnpm", "pip", "unknown"]

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _resolve(cmd: list[str]) -> list[str]:
    """Resolve the executable to a full path so Windows finds npm.cmd / yarn.cmd without a shell."""
    exe = shutil.which(cmd[0])
    return [exe, *cmd[1:]] if exe else cmd


def _node_dependency_names(path: Path) -> list[str] | None:
    try:
        data = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    names: list[str] = []
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            names.extend(deps)
    return names


def check_node_deps(path: Path, manager: PackageManager) -> bool:
    """Check if Node.js dependencies are installed.

    Looks for each package listed in package.json inside node_modules (instant, and does
    not depend on `npm list` finishing or on peer-dependency warnings).
    Returns True if deps appear complete, False if missing.
    """
    node_modules = path / "node_modules"
    if not node_modules.is_dir():
        return False
    names = _node_dependency_names(path)
    if names is None:
        return True  # can't tell — don't nag
    return all((node_modules / name).exists() for name in names)


def _requirement_names(path: Path, dep_file: str | None) -> list[str]:
    names: list[str] = []
    if dep_file == "pyproject.toml":
        try:
            data = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        for spec in data.get("project", {}).get("dependencies", []) or []:
            m = _NAME_RE.match(spec)
            if m:
                names.append(m.group(1))
    else:
        try:
            lines = (path / "requirements.txt").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.split("#", 1)[0].strip()
            # skip blanks, options (-r, -e, --index-url ...) and URLs / local paths
            if not line or line.startswith("-") or "://" in line or line.startswith((".", "/")):
                continue
            m = _NAME_RE.match(line)
            if m:
                names.append(m.group(1))
    return names


def check_python_deps(path: Path, dep_file: str | None) -> bool:
    """Check if the project's declared Python dependencies are installed.

    Returns True if deps are complete, False if any is missing.
    dep_file is either 'requirements.txt' or 'pyproject.toml'.
    """
    for name in _requirement_names(path, dep_file):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            return False
    return True


def _install_command(manager: PackageManager, dep_file: str | None, path: Path) -> list[str]:
    if manager in ("npm", "yarn", "pnpm"):
        return _resolve([manager, "install"])
    if manager == "pip":
        if dep_file == "requirements.txt":
            return [sys.executable, "-m", "pip", "install", "-r", str(path / "requirements.txt")]
        return [sys.executable, "-m", "pip", "install", "-e", str(path)]
    return []


def _run_install(cmd: list[str], path: Path) -> bool:
    if not cmd:
        return False
    try:
        result = subprocess.run(
            cmd,
            cwd=str(path),
            env=child_environment(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def install_node_deps(path: Path, manager: PackageManager) -> bool:
    """Run install command for Node project. Returns True on success."""
    manager = manager if manager in ("npm", "yarn", "pnpm") else "npm"
    return _run_install(_install_command(manager, None, path), path)


def install_python_deps(path: Path, dep_file: str | None) -> bool:
    """Run pip install for Python project. Returns True on success."""
    return _run_install(_install_command("pip", dep_file, path), path)


class DependencyCheckResult:
    def __init__(
        self,
        missing: bool,
        manager: PackageManager,
        dep_file: str | None,
        service_name: str,
        service_path: Path,
    ) -> None:
        self.missing = missing
        self.manager = manager
        self.dep_file = dep_file
        self.service_name = service_name
        self.service_path = service_path

    def install_command(self) -> list[str]:
        """Return the install command as an argv list (no shell needed)."""
        return _install_command(self.manager, self.dep_file, self.service_path)


def check_service_deps(service_name: str, service_path: Path) -> DependencyCheckResult:
    """Check dependencies for a single service.

    Returns DependencyCheckResult with missing=True if deps are not installed.
    """
    manager, dep_file = detect_package_manager(service_path)

    missing = False
    if manager in ("npm", "yarn", "pnpm"):
        missing = not check_node_deps(service_path, manager)
    elif manager == "pip":
        missing = not check_python_deps(service_path, dep_file)
    else:
        manager = "unknown"

    return DependencyCheckResult(
        missing=missing,
        manager=manager,
        dep_file=dep_file,
        service_name=service_name,
        service_path=service_path,
    )
