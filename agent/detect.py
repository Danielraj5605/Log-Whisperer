"""Project detection — auto-detect project structure and dev commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

ProjectType = Literal[
    "node",
    "python",
    "go",
    "rust",
    "java",
    "ruby",
    "php",
    "dotnet",
    "static",   # plain HTML/CSS/JS, no build tool
    "unknown",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_package_manager(path: Path) -> str:
    """Return 'yarn', 'pnpm', or 'npm' depending on lock file present."""
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    return "npm"


def _node_dev_script(path: Path) -> str | None:
    """Return 'dev', 'start', or None based on package.json scripts."""
    pkg = path / "package.json"
    if not pkg.exists():
        return None
    try:
        with open(pkg, encoding="utf-8") as f:
            data = json.load(f)
        scripts = data.get("scripts", {})
        for script in ("dev", "start", "serve"):
            if script in scripts:
                return script
    except (json.JSONDecodeError, OSError):
        pass
    return "dev"   # assume dev even if we can't read the file


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_project_type(cwd: Path | None = None) -> ProjectType:
    """Detect project type from common files in current directory."""
    if cwd is None:
        cwd = Path.cwd()
    return detect_project_type_at(cwd)


def detect_project_type_at(path: Path) -> ProjectType:
    """Detect project type at a specific path."""
    # Node
    if (path / "package.json").exists():
        return "node"

    # Python
    if (
        (path / "pyproject.toml").exists()
        or (path / "setup.py").exists()
        or (path / "requirements.txt").exists()
        or (path / "main.py").exists()
        or (path / "app.py").exists()
    ):
        return "python"

    # Go
    if (path / "go.mod").exists():
        return "go"

    # Rust
    if (path / "Cargo.toml").exists():
        return "rust"

    # Java (Maven or Gradle)
    if (path / "pom.xml").exists() or (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
        return "java"

    # Ruby
    if (path / "Gemfile").exists():
        return "ruby"

    # PHP (Composer)
    if (path / "composer.json").exists():
        return "php"

    # .NET
    if list(path.glob("*.csproj")) or list(path.glob("*.fsproj")) or list(path.glob("*.sln")):
        return "dotnet"

    # Static HTML/CSS/JS (no build tool at all)
    if (path / "index.html").exists():
        return "static"

    return "unknown"


def detect_dev_command(project_type: ProjectType, path: Path | None = None) -> str | None:
    """Return the dev/run command string for the given stack, or None."""
    if path is None:
        path = Path.cwd()

    if project_type == "node":
        pm = _node_package_manager(path)
        script = _node_dev_script(path)
        if script is None:
            return None
        if pm == "npm":
            return f'npm --prefix "{path}" run {script}'
        elif pm == "yarn":
            return f'yarn --cwd "{path}" {script}'
        else:  # pnpm
            return f'pnpm --dir "{path}" run {script}'

    elif project_type == "python":
        for entrypoint in ["main.py", "app.py", "server.py", "manage.py", "run.py"]:
            ep = path / entrypoint
            if ep.exists():
                # Django
                if entrypoint == "manage.py":
                    return f'python -u "{ep}" runserver'
                # FastAPI / uvicorn hint
                if entrypoint in ("main.py", "app.py"):
                    # check if uvicorn is likely used
                    src = ep.read_text(encoding="utf-8", errors="ignore")
                    if "uvicorn" in src or "FastAPI" in src or "fastapi" in src:
                        module = ep.stem  # "main" or "app"
                        return f'python -m uvicorn {module}:app --reload --app-dir "{path}"'
                return f'python -u "{ep}"'
        return None

    elif project_type == "go":
        # Prefer `go run .` if main.go exists, else `go run ./cmd/...`
        if (path / "main.go").exists():
            return f'go run "{path / "main.go"}"'
        cmd_dirs = list((path / "cmd").glob("*/main.go")) if (path / "cmd").is_dir() else []
        if cmd_dirs:
            return f'go run "{cmd_dirs[0]}"'
        return f'go run .'

    elif project_type == "rust":
        return f'cargo run --manifest-path "{path / "Cargo.toml"}"'

    elif project_type == "java":
        if (path / "pom.xml").exists():
            mvnw = path / ("mvnw.cmd" if os.name == "nt" else "mvnw")
            runner = f'"{mvnw}"' if mvnw.exists() else "mvn"
            return f'{runner} -f "{path / "pom.xml"}" spring-boot:run'
        elif (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
            gradlew = path / ("gradlew.bat" if os.name == "nt" else "gradlew")
            runner = f'"{gradlew}"' if gradlew.exists() else "gradle"
            return f'{runner} -p "{path}" bootRun'
        return None

    elif project_type == "ruby":
        if (path / "config.ru").exists():
            return f'bundle exec rails server -b 0.0.0.0'
        return f'bundle exec ruby "{path / "app.rb"}"' if (path / "app.rb").exists() else None

    elif project_type == "php":
        return f'php -S localhost:8000 -t "{path}"'

    elif project_type == "dotnet":
        csproj = next(iter(path.glob("*.csproj")), None)
        sln = next(iter(path.glob("*.sln")), None)
        target = csproj or sln
        if target:
            return f'dotnet run --project "{target}"'
        return f'dotnet run --project "{path}"'

    elif project_type == "static":
        # Use Python's built-in HTTP server — zero extra installs needed.
        # Serves index.html at http://localhost:3000
        return f'python -m http.server 3000 --directory "{path}"'

    return None


def _default_url(project_type: ProjectType, path: Path | None = None) -> str | None:
    """Return a best-guess localhost URL for a given stack."""
    urls: dict[str, str] = {
        "node":   "http://localhost:5173",   # Vite default; npm start → 3000
        "python": "http://localhost:8000",
        "go":     "http://localhost:8080",
        "rust":   "http://localhost:8000",
        "java":   "http://localhost:8080",
        "ruby":   "http://localhost:3000",
        "php":    "http://localhost:8000",
        "dotnet": "http://localhost:5000",
        "static": "http://localhost:3000",
    }
    # Node: check if package.json says "start" (CRA → 3000) vs "dev" (Vite → 5173)
    if project_type == "node" and path is not None:
        script = _node_dev_script(path)
        if script == "start":
            return "http://localhost:3000"
    return urls.get(project_type)


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------

def detect_project_parts(cwd: Path | None = None) -> dict[str, dict]:
    """Detect all project parts (frontend, backend, etc.) at the given directory.

    Returns a dict like:
    {
        "backend":  {"name": "backend",  "path": ".", "log_path": "logs/backend/app.log",
                     "type": "python", "command": "python -u main.py", "url": "http://localhost:8000"},
        "frontend": {"name": "frontend", "path": "client", ...,
                     "type": "node",   "command": "npm --prefix client run dev", "url": "http://localhost:5173"},
    }
    """
    if cwd is None:
        cwd = Path.cwd()
    parts: dict[str, dict] = {}

    def _make_entry(name: str, path: Path, pt: ProjectType, cmd: str, url: str | None = None) -> dict:
        return {
            "name":     name,
            "path":     str(path),
            "log_path": f"logs/{name}/app.log",
            "type":     pt,
            "command":  cmd,
            "url":      url or _default_url(pt, path),
        }

    # ------------------------------------------------------------------
    # 1. Root-level project
    # ------------------------------------------------------------------
    root_type = detect_project_type(cwd)
    if root_type not in ("unknown", "static"):
        cmd = detect_dev_command(root_type, cwd)
        if cmd:
            parts["backend"] = _make_entry("backend", cwd, root_type, cmd)
    elif root_type == "static":
        # Pure HTML/CSS/JS at root → serve it
        cmd = detect_dev_command("static", cwd)
        if cmd:
            parts["project"] = _make_entry("project", cwd, "static", cmd)

    # ------------------------------------------------------------------
    # 2. Frontend sub-directories
    # ------------------------------------------------------------------
    frontend_dirs = ["frontend", "client", "app", "web", "ui", "www"]
    for frontend_dir in frontend_dirs:
        fp = cwd / frontend_dir
        if not fp.is_dir():
            continue
        ft = detect_project_type_at(fp)
        if ft == "unknown":
            continue
        cmd = detect_dev_command(ft, fp)
        if cmd:
            parts["frontend"] = _make_entry("frontend", fp, ft, cmd)
            break

    # ------------------------------------------------------------------
    # 3. Backend/server sub-directories
    # ------------------------------------------------------------------
    backend_dirs = ["backend", "server", "api", "core", "service", "services"]
    for backend_dir in backend_dirs:
        fp = cwd / backend_dir
        if not fp.is_dir():
            continue
        bt = detect_project_type_at(fp)
        if bt == "unknown":
            continue
        cmd = detect_dev_command(bt, fp)
        if not cmd:
            continue

        if "backend" in parts and parts["backend"]["path"] == str(cwd):
            # Prefer the dedicated subdir over the generic root entry
            parts["backend"] = _make_entry("backend", fp, bt, cmd)
        else:
            parts["server"] = _make_entry("server", fp, bt, cmd)
        break

    # ------------------------------------------------------------------
    # 4. docker-compose: treat each named service as its own entry
    #    (only if nothing else was found — avoids double-spawning)
    # ------------------------------------------------------------------
    if not parts:
        for compose_file in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
            cf = cwd / compose_file
            if cf.exists():
                parts["docker"] = _make_entry(
                    "docker", cwd, "unknown",
                    cmd=f'docker compose -f "{cf}" up',
                    url=None,
                )
                break

    # ------------------------------------------------------------------
    # 5. Fallback: root is node but no subdir was found
    # ------------------------------------------------------------------
    if not parts and root_type == "node":
        cmd = detect_dev_command("node", cwd)
        if cmd:
            parts["project"] = _make_entry("project", cwd, "node", cmd)

    return parts


def detect_project_parts_at(path: Path) -> dict[str, dict]:
    """Detect project parts at a specific path (for the project command)."""
    return detect_project_parts(cwd=path)


def get_project_name(cwd: Path | None = None) -> str:
    """Return the current project name (directory name)."""
    if cwd is None:
        cwd = Path.cwd()
    return cwd.name


def ensure_logs_dir() -> Path:
    """Ensure ./logs directory exists, return the path."""
    logs_dir = Path.cwd() / "logs"
    logs_dir.mkdir(exist_ok=True)
    return logs_dir


def detect_package_manager(path: Path) -> tuple[str, str | None]:
    """Detect package manager and return (manager, dep_file).

    Checks for lock files in order:
      - package-lock.json  → npm
      - yarn.lock         → yarn
      - pnpm-lock.yaml    → pnpm

    Returns (manager, dep_file) or ("pip", "requirements.txt") for Python,
    or ("unknown", None) if nothing detected.
    """
    if (path / "package-lock.json").exists():
        return ("npm", None)
    if (path / "yarn.lock").exists():
        return ("yarn", None)
    if (path / "pnpm-lock.yaml").exists():
        return ("pnpm", None)

    if (path / "requirements.txt").exists():
        return ("pip", "requirements.txt")
    if (path / "pyproject.toml").exists():
        return ("pip", "pyproject.toml")

    return ("unknown", None)
