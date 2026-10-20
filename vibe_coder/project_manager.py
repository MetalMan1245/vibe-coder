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
from vibe_coder.vibeignore import (
    VIBEIGNORE_FILENAME,
    ensure_vibeignore,
    load_vibeignore,
)

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

    def create_project(self, name: str, scope_path: Path,
                       vibeignore_templates: list[str] | None = None) -> dict:
        """Create a new project with git initialized.

        ``vibeignore_templates`` is an optional list of language keys
        (e.g. ["python", "rust"]) used to generate the project's starter
        ``.vibeignore`` — the context filter read only by this app.
        """
        proj_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Create project directory structure
        project_dir = PROJECTS_DIR / proj_id
        project_dir.mkdir(parents=True, exist_ok=True)

        # Initialize git repo
        self._init_git(scope_path)

        # Seed the context-only ignore file (separate from .gitignore on
        # purpose: internal docs can stay visible to the model while being
        # excluded from publishing, and build junk never reaches context).
        created, added = ensure_vibeignore(scope_path, vibeignore_templates)

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
        self._log_activity(
            proj_id,
            f"Project created: {name}, scope: {scope_path}"
            + (f", vibeignore templates: {', '.join(added)}" if added else "")
        )

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
        scope = Path(proj_data["scope_path"])
        if not scope.exists():
            return None
        return scope

    def read_file(self, proj_id: str, relative_path: str,
                  force: bool = False) -> str | None:
        """Read a file within the project scope.

        Files matching the project's .vibeignore are hidden from the model:
        reading them would waste context on build junk/caches. The GUI uses
        ``force=True`` when the *user* explicitly clicks a file.
        """
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

        if not force and self.is_vibe_ignored(proj_id, relative_path):
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

    def list_files(self, proj_id: str, recursive: bool = True,
                   force: bool = False) -> list[str]:
        """List files within the project scope.

        Ignores VCS/internal directories (.git, caches, aider state) so
        listings show real project files instead of thousands of repo
        plumbing objects, then additionally applies the project's
        ``.vibeignore`` rules (context-only filter; pass ``force=True``
        from the GUI to include everything for the human user).
        """
        scope = self.get_project_scope(proj_id)
        if not scope:
            return []

        ignored = {".git", "__pycache__", "node_modules", ".venv",
                   ".mypy_cache", ".pytest_cache", ".ruff_cache",
                   ".idea", ".vscode"}

        def _excluded(path: Path) -> bool:
            for part in path.relative_to(scope).parts[:-1]:
                if part in ignored or part.startswith(".aider"):
                    return True
            return False

        matcher = None if force else self.vibeignore_matcher(proj_id)

        files = []
        if recursive:
            for path in scope.rglob("*"):
                if path.is_file() and not _excluded(path):
                    rel = str(path.relative_to(scope))
                    if matcher is not None and matcher.is_ignored(rel):
                        continue
                    files.append(rel)
        else:
            for path in scope.iterdir():
                if path.is_file():
                    rel = path.name
                    if matcher is not None and matcher.is_ignored(rel):
                        continue
                    files.append(rel)

        return sorted(files)

    # ------------------------------------------------------------------
    # .vibeignore — context-only ignore rules (never affects git)
    # ------------------------------------------------------------------

    def vibeignore_matcher(self, proj_id: str):
        """Return a cached VibeIgnore matcher for the project scope."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return None
        cache = getattr(self, "_vibeignore_cache", None)
        if cache is None:
            cache = self._vibeignore_cache = {}
        entry = cache.get(proj_id)
        if entry is not None and entry[0] == scope:
            return entry[1]
        matcher, warnings = load_vibeignore(scope)
        cache[proj_id] = (scope, matcher)
        return matcher

    def reload_vibeignore(self, proj_id: str) -> tuple[int, list[str]]:
        """Re-read .vibeignore from disk. Returns (pattern_count, warnings)."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return 0, ["No project found"]
        matcher, warnings = load_vibeignore(scope)
        cache = getattr(self, "_vibeignore_cache", None)
        if cache is None:
            cache = self._vibeignore_cache = {}
        cache[proj_id] = (scope, matcher)
        count = len(matcher._rules) if matcher else 0
        return count, warnings

    def read_vibeignore(self, proj_id: str) -> str | None:
        """Raw text of the project's .vibeignore (None if missing/broken)."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return None
        path = scope / VIBEIGNORE_FILENAME
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_vibeignore(self, proj_id: str, content: str) -> bool:
        """Save edited .vibeignore rules and refresh the cached matcher."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return False
        try:
            (scope / VIBEIGNORE_FILENAME).write_text(content, encoding="utf-8")
        except OSError:
            return False
        self._log_activity(proj_id, f"Updated: {VIBEIGNORE_FILENAME}")
        self.reload_vibeignore(proj_id)
        return True

    def ensure_vibeignore_for_project(self, proj_id: str,
                                      template_keys: list[str] | None = None
                                      ) -> tuple[bool, list[str]]:
        """Create/extend the project's .vibeignore on demand (GUI action)."""
        scope = self.get_project_scope(proj_id)
        if not scope:
            return False, []
        result = ensure_vibeignore(scope, template_keys)
        self.reload_vibeignore(proj_id)
        return result

    def is_vibe_ignored(self, proj_id: str, relative_path: str) -> bool:
        matcher = self.vibeignore_matcher(proj_id)
        return bool(matcher and matcher.is_ignored(relative_path))

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
            elif "nothing to commit" in result.stderr.lower():
                self._log_activity(proj_id, f"Clean repo: {message}")
                return True, "Nothing to commit"
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
