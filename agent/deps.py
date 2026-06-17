"""Dependency checker — detect missing deps and optionally install them."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from agent.detect import detect_package_manager

PackageManager = Literal["npm", "yarn", "pnpm", "pip", "unknown"]


def check_node_deps(path: Path, manager: PackageManager) -> bool:
    """Check if Node.js dependencies are installed.

    Returns True if deps appear complete, False if missing.
    """
    node_modules = path / "node_modules"
    if not node_modules.exists():
        return False

    cmd_map = {
        "npm": ["npm", "list", "--depth=0", "--quiet"],
        "yarn": ["yarn", "list", "--depth=0", "--quiet"],
        "pnpm": ["pnpm", "list", "--depth=0"],
    }
    cmd = cmd_map.get(manager, ["npm", "list", "--depth=0", "--quiet"])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
            shell=True,
        )
        if result.returncode != 0:
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_python_deps(path: Path, dep_file: str | None) -> bool:
    """Check if Python dependencies are satisfied.

    Returns True if deps are complete, False if missing.
    dep_file is either 'requirements.txt' or 'pyproject.toml'.
    """
    if dep_file == "pyproject.toml":
        try:
            result = subprocess.run(
                ["pip", "check"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    req_file = path / "requirements.txt"
    if not req_file.exists():
        return True

    try:
        result = subprocess.run(
            ["pip", "check"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=True,
        )
        if result.returncode == 0:
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def install_node_deps(path: Path, manager: PackageManager) -> bool:
    """Run install command for Node project. Returns True on success."""
    cmd_map = {
        "npm": ["npm", "install"],
        "yarn": ["yarn", "install"],
        "pnpm": ["pnpm", "install"],
    }
    cmd = cmd_map.get(manager, ["npm", "install"])

    try:
        result = subprocess.run(
            cmd,
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=300,
            shell=True,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def install_python_deps(path: Path, dep_file: str | None) -> bool:
    """Run pip install for Python project. Returns True on success."""
    if dep_file == "requirements.txt":
        req_file = path / "requirements.txt"
        cmd = ["pip", "install", "-r", str(req_file)]
    else:
        cmd = ["pip", "install", "-e", str(path)]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=300,
            shell=True,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


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
        """Return the install command as a list for subprocess."""
        if self.manager in ("npm", "yarn", "pnpm"):
            cmd_map = {
                "npm": ["npm", "install"],
                "yarn": ["yarn", "install"],
                "pnpm": ["pnpm", "install"],
            }
            return cmd_map.get(self.manager, ["npm", "install"])
        elif self.manager == "pip":
            if self.dep_file == "requirements.txt":
                return ["pip", "install", "-r", str(self.service_path / "requirements.txt")]
            return ["pip", "install", "-e", str(self.service_path)]
        return []


def check_service_deps(service_name: str, service_path: Path) -> DependencyCheckResult:
    """Check dependencies for a single service.

    Returns DependencyCheckResult with missing=True if deps are not installed.
    """
    manager, dep_file = detect_package_manager(service_path)

    missing = False
    if manager in ("npm", "yarn", "pnpm"):
        if not check_node_deps(service_path, manager):
            missing = True
    elif manager == "pip":
        if not check_python_deps(service_path, dep_file):
            missing = True
    else:
        manager = "unknown"

    return DependencyCheckResult(
        missing=missing,
        manager=manager,
        dep_file=dep_file,
        service_name=service_name,
        service_path=service_path,
    )