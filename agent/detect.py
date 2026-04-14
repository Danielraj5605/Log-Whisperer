"""Project detection — auto-detect project structure and dev commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

ProjectType = Literal["node", "python", "unknown"]


def detect_project_parts(cwd: Path | None = None) -> dict[str, dict]:
    """Detect all project parts (frontend, backend, etc.) at the given directory.

    Returns dict with structure like:
    {
        "backend": {
            "name": "backend",
            "path": ".",
            "log_path": "logs/backend/app.log",
            "type": "node",
            "command": "npm run dev",
        },
        "frontend": {
            "name": "frontend",
            "path": "client",
            "log_path": "logs/frontend/app.log",
            "type": "node",
            "command": "npm --prefix client run dev",
        },
    }
    """
    if cwd is None:
        cwd = Path.cwd()
    parts = {}

    def _make_entry(name: str, path: Path, pt: ProjectType, cmd: str) -> dict:
        return {
            "name": name,
            "path": str(path),
            "log_path": f"logs/{name}/app.log",
            "type": pt,
            "command": cmd,
        }

    # Check root-level project
    root_type = detect_project_type(cwd)
    if root_type != "unknown":
        cmd = detect_dev_command(root_type, cwd)
        if cmd:
            parts["backend"] = _make_entry("backend", cwd, root_type, cmd)

    # Check frontend directories
    frontend_dirs = ["frontend", "client", "app", "web", "ui"]
    for frontend_dir in frontend_dirs:
        fp = cwd / frontend_dir
        if fp.is_dir() and (fp / "package.json").exists():
            pt: ProjectType = "node"
            cmd = detect_dev_command(pt, fp)
            if cmd:
                # detect_dev_command already returns the full npm command with --prefix
                parts["frontend"] = _make_entry("frontend", fp, pt, cmd)
                break

    # Check backend/server directories
    backend_dirs = ["backend", "server", "api", "core"]
    for backend_dir in backend_dirs:
        fp = cwd / backend_dir
        if fp.is_dir():
            pt = detect_project_type_at(fp)
            if pt != "unknown":
                cmd = detect_dev_command(pt, fp)
                if cmd:
                    # If root already detected as backend, skip subdir
                    if "backend" in parts and parts["backend"]["path"] == str(cwd):
                        continue
                    # If root is python and this dir is also python, treat as server
                    if root_type == "python" and pt == "python":
                        cmd = f"python -m uvicorn {backend_dir}.main:app --reload"
                    parts["server"] = _make_entry(backend_dir, fp, pt, cmd)
                    break

    # If nothing found, treat root as a standalone project
    if not parts and root_type == "node":
        cmd = detect_dev_command(root_type, cwd)
        if cmd:
            parts["project"] = _make_entry("project", cwd, root_type, cmd)

    return parts


def detect_project_type(cwd: Path | None = None) -> ProjectType:
    """Detect project type from common files in current directory."""
    if cwd is None:
        cwd = Path.cwd()

    if (cwd / "package.json").exists():
        return "node"
    elif (cwd / "pyproject.toml").exists() or (cwd / "main.py").exists() or (cwd / "app.py").exists():
        return "python"

    return "unknown"


def get_project_name(cwd: Path | None = None) -> str:
    """Return the current project name (directory name)."""
    if cwd is None:
        cwd = Path.cwd()
    return cwd.name


def detect_project_type_at(path: Path) -> ProjectType:
    """Detect project type at a specific path."""
    if (path / "package.json").exists():
        return "node"
    elif (path / "pyproject.toml").exists() or (path / "main.py").exists() or (path / "app.py").exists():
        return "python"

    return "unknown"


def detect_dev_command(project_type: ProjectType, path: Path | None = None) -> str | None:
    """Detect the dev command for a given project type."""
    if path is None:
        path = Path.cwd()

    if project_type == "node":
        package_json = path / "package.json"
        if package_json.exists():
            try:
                with open(package_json, encoding="utf-8") as f:
                    data = json.load(f)
                scripts = data.get("scripts", {})
                if "dev" in scripts:
                    return f'npm --prefix "{path}" run dev'
                elif "start" in scripts:
                    return f'npm --prefix "{path}" start'
            except (json.JSONDecodeError, OSError):
                pass
        return f'npm --prefix "{path}" run dev'

    elif project_type == "python":
        for entrypoint in ["main.py", "app.py", "server.py", "run.py"]:
            if (path / entrypoint).exists():
                return f'python -u "{path / entrypoint}"'
        return None

    return None


def detect_project_parts_at(path: Path) -> dict[str, dict]:
    """Detect project parts at a specific path (for the project command)."""
    return detect_project_parts(cwd=path)


def ensure_logs_dir() -> Path:
    """Ensure ./logs directory exists, return the path."""
    logs_dir = Path.cwd() / "logs"
    logs_dir.mkdir(exist_ok=True)
    return logs_dir
