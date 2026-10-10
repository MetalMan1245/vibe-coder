"""Project and file system management with Git integration.

Projects define a scoped directory where the AI can create/modify files.
All changes are tracked via Git with automated commit summaries.
"""

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vibe_coder.config import CONFIG_DIR

PROJECTS_DIR = CONFIG_DIR / "projects"
PROJECT_INDEX = PROJECTS_DIR / "index.json"

class ProjectManager:
    def __init__(self):
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_index()
        self.first_project_warning_shown = False

    def _ensure_index(self) -> None:
        if not PROJECT_INDEX.exists():
            self._write_index([])

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(PROJECT_INDEX.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write_index(self, entries: list[dict]) -> None:
        PROJECT_INDEX.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    def create_project(self, name: str, scope_path: Path) -> dict:
        """Create a new project with git initialized."""
        proj_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Create project directory structure
        project_dir = PROJECTS_DIR / proj_id
        project_dir.mkdir(parents=True, exist_ok=True)

        # Initialize git repo
        self._init_git(scope_path)

        # Create README if one doesn't exist
        readme_path = scope_path / "README.md"
        if not readme_path.exists():
            readme_path.write_text(f"# {name}\n\nCreated by VibeCoder on {now[:10]}\n")

        # Save project metadata
        project_data = {
            "id": proj_id,
            "name": name,
            "scope_path": str(scope_path.absolute()),
            "created": now,
            "updated": now,
        }

        # Persist to project-local config
        config_path = project_dir / "project.json"
        config_path.write_text(json.dumps(project_data, indent=2) + "\n", encoding="utf-8")

        # Update index
        self._add_to_index({
            "id": proj_id,
            "name": name,
            "scope_path": str(scope_path.absolute()),
            "created": now,
        })

        # Log initialization
        self._log_activity(proj_id, f"Project created: {name}, scope: {scope_path}")

        return project_data

    def _init_git(self, scope_path: Path) -> None:
        """Initialize git repository if not already initialized."""
        git_dir = scope_path / ".git"
        if git_dir.exists():
            return

        try:
            subprocess.run(
                ["git", "init"],
                cwd=str(scope_path),
                capture_output=True,
                check=True,
                timeout=10,
            )

            # Configure git identity if not set
            subprocess.run(
                ["git", "config", "user.email", "vibecoder@localhost"],
                cwd=str(scope_path),
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "VibeCoder"],
                cwd=str(scope_path),
                capture_output=True,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            raise RuntimeError(f"Failed to initialize git: {exc}")

    def _add_to_index(self, entry: dict) -> None:
        index = self._read_index()
        index.insert(0, entry)
        self._write_index(index)

    def load_project(self, proj_id: str) -> dict | None:
        project_dir = PROJECTS_DIR / proj_id
        config_path = project_dir / "project.json"
        if not config_path.exists():
            return None
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_projects(self) -> list[dict]:
        return sorted(self._read_index(), key=lambda e: e["updated"] if "updated" in e else e["created"], reverse=True)

    def delete_project(self, proj_id: str) -> bool:
        project_dir = PROJECTS_DIR / proj_id
        if not project_dir.exists():
            return False
        try:
            import shutil
            shutil.rmtree(project_dir)
        except OSError:
            return False

        index = self._read_index()
        index = [e for e in index if e["id"] != proj_id]
        self._write_index(index)
        return True

    def get_project_scope(self, proj_id: str) -> Path | None:
        proj_data = self.load_project(proj_id)
        if not proj_data:
            return None
        return Path(proj_data["scope_path"])

    def read_file(self, proj_id: str, relative_path: str) -> str | None:
        """Read a file within the project scope."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return None

        full_path = scope / relative_path

        # Security: prevent path traversal outside scope
        try:
            full_path.relative_to(scope)
        except ValueError:
            raise PermissionError(f"Path {relative_path} is outside project scope")

        if not full_path.exists():
            return None

        try:
            return full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def write_file(self, proj_id: str, relative_path: str, content: str) -> bool:
        """Write a file within the project scope. Returns True on success."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return False

        full_path = scope / relative_path

        # Security: prevent path traversal
        try:
            full_path.relative_to(scope)
        except ValueError:
            raise PermissionError(f"Path {relative_path} is outside project scope")

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            self._log_activity(proj_id, f"Written: {relative_path}")
            return True
        except OSError:
            return False

    def delete_file(self, proj_id: str, relative_path: str) -> bool:
        """Delete a file within the project scope."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return False

        full_path = scope / relative_path
        try:
            full_path.relative_to(scope)
        except ValueError:
            raise PermissionError(f"Path {relative_path} is outside project scope")

        try:
            full_path.unlink()
            self._log_activity(proj_id, f"Deleted: {relative_path}")
            return True
        except OSError:
            return False

    def list_files(self, proj_id: str, recursive: bool = True) -> list[str]:
        """List files within the project scope.

        Ignores VCS/internal directories (.git, caches, aider state)
        so listings show real project files instead of thousands of
        repo plumbing objects.
        """
        scope = self.get_project_scope(proj_id)
        if not scope or not scope.exists():
            return []

        ignored = {".git", "__pycache__", "node_modules", ".venv",
                   ".mypy_cache", ".pytest_cache", ".ruff_cache",
                   ".idea", ".vscode"}

        def _excluded(path: Path) -> bool:
            for part in path.relative_to(scope).parts[:-1]:
                if part in ignored or part.startswith(".aider"):
                    return True
            return False

        files = []
        if recursive:
            for path in scope.rglob("*"):
                if path.is_file() and not _excluded(path):
                    files.append(str(path.relative_to(scope)))
        else:
            for path in scope.iterdir():
                if path.is_file():
                    files.append(path.name)

        return sorted(files)

    def git_commit(self, proj_id: str, message: str) -> tuple[bool, str]:
        """Commit all changes in the project scope. Returns (success, stdout/stderr)."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return False, "No project found"

        try:
            # Stage all changes
            subprocess.run(
                ["git", "add", "."],
                cwd=str(scope),
                capture_output=True,
                check=True,
                timeout=30,
            )

            # Commit with message
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(scope),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                self._log_activity(proj_id, f"Committed: {message}")
                return True, result.stdout
            else:
                return False, result.stderr or "Git commit failed"
        except subprocess.SubprocessError as exc:
            return False, str(exc)

    def git_status(self, proj_id: str) -> str:
        """Get git status of the project."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return "No project"

        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(scope),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout or "No changes"
        except subprocess.SubprocessError:
            return "Git not initialized or error"

    def _log_activity(self, proj_id: str, message: str) -> None:
        """Append an activity log entry."""
        project_dir = PROJECTS_DIR / proj_id
        log_path = project_dir / ".vibe-coder" / "log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"[{timestamp}] {message}\n"

        try:
            log_path.write_text(log_path.read_text(encoding="utf-8", errors="replace") + entry, encoding="utf-8")
        except OSError:
            pass
