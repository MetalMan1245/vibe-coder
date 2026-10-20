"""VibeIgnore: context-only ignore rules for VibeCoder projects.

A ``.vibeignore`` file lives in the project root and is read *only* by this
application. It never affects git: it exists to keep junk (build outputs,
caches, virtualenvs...) out of the model's context, while letting files that
are useful to the AI but not worth publishing (internal docs, scratch notes)
stay visible — which is why it is deliberately separate from ``.gitignore``.

Syntax is plain gitignore syntax (see https://git-scm.com/docs/gitignore):
patterns, ``#`` comments, ``!`` negation, directory-only ``/`` suffixes,
anchored ``/leading`` patterns and ``**`` globs all work as expected. The
file is user-editable like a gitignore; invalid lines are skipped with a
warning instead of crashing.

Newly created projects get a starter ``.vibeignore`` containing language
agnostic defaults plus any templates the user picked in the New Project
dialog (GitHub-Desktop style "generate a .gitignore for Python/Rust/..."
feature, but scoped to model context reduction).
"""

from pathlib import Path

VIBEIGNORE_FILENAME = ".vibeignore"

# ---------------------------------------------------------------------------
# Language-agnostic defaults: build artifacts, caches, environments, and
# OS/editor noise that no model ever needs to see.
# ---------------------------------------------------------------------------
_CORE_DEFAULTS = [
    "# >>> vibe-coder core defaults (safe to edit) >>>",
    "# Version control internals",
    ".git/",
    "",
    "# Python caches & tooling",
    "__pycache__/",
    "*.py[cod]",
    ".venv/",
    "venv/",
    "env/",
    "site-packages/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "htmlcov/",
    "dist/",
    "build/",
    "*.egg-info/",
    "",
    "# Node / web builds",
    "node_modules/",
    ".next/",
    ".nuxt/",
    "coverage/",
    "*.tsbuildinfo",
    "",
    "# Rust / Cargo",
    "/target/",
    "**/target/",
    "",
    "# Other common build output",
    "bin/Debug/",
    "bin/Release/",
    "obj/",
    "out/",
    "cmake-build-*/",
    "*.o",
    "*.obj",
    "*.a",
    "*.so",
    "*.so.*",
    "*.dylib",
    "*.exe",
    "*.dll",
    "*.class",
    "*.jar",
    "*.war",
    "*.pyd",
    "*.pdb",
    "*.ilk",
    "",
    "# Logs & temp files",
    "*.log",
    "logs/",
    "tmp/",
    "temp/",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*~",
    "",
    "# vibe-coder internal state",
    ".vibe-coder/",
    "# <<< vibe-coder core defaults <<<",
]

# ---------------------------------------------------------------------------
# Per-language templates (GitHub-Desktop style generator, vibeignore flavor).
# Keys double as display names via pretty_name().
# ---------------------------------------------------------------------------
TEMPLATES: dict[str, list[str]] = {
    "python": [
        "# Python (added by vibe-coder)",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        ".Python",
        "pip-log.txt",
        "pip-delete-this-directory.txt",
        "docs/_build/",
        ".tox/",
        ".nox/",
        "*.db",
        "*.sqlite3",
        ".ipynb_checkpoints/",
        "profile_default/",
        "*.safetensors",
        "*.pth",
        "*.pt",
        "*.onnx",
    ],
    "rust": [
        "# Rust (added by vibe-coder)",
        "/target/",
        "**/target/",
        "*.rs.bk",
        "*.rlib",
        "Cargo.lock.bak",
    ],
    "javascript": [
        "# JavaScript / TypeScript (added by vibe-coder)",
        "bower_components/",
        ".pnpm-store/",
        ".yarn/",
        "*.tgz",
        ".parcel-cache/",
        ".svelte-kit/",
        ".output/",
        "npm-debug.log*",
        "yarn-error.log*",
    ],
    "go": [
        "# Go (added by vibe-coder)",
        "*.exe",
        "*.test",
        ".golangci.yml.local",
        "vendor/",
        "*.out",
        "go.work",
    ],
    "java": [
        "# Java / JVM (added by vibe-coder)",
        "*.class",
        "*.ctxt",
        ".gradle/",
        ".mtj.tmp/",
        "*.hsperfdata_*",
    ],
    "cpp": [
        "# C / C++ (added by vibe-coder)",
        "*.d",
        "*.gch",
        "*.lo",
        "*.la",
        "*.slo",
        "*.sp",
        "*.suo",
        "*.user",
        "*.userosscache",
        "*.sln.docstates",
        "CMakeFiles/",
        "CMakeCache.txt",
        "compile_commands.json",
        "CTestTestfile.cmake",
    ],
    "dotnet": [
        "# .NET (added by vibe-coder)",
        "[Bb]in/",
        "[Oo]bj/",
        "*.user",
        "*.suo",
        "*.userprefs",
        "*.pidb",
        "*.booproj",
        "*.dll.metadta",
        "TestResults/",
        "*.nupkg",
    ],
    "php": [
        "# PHP (added by vibe-coder)",
        "/vendor/",
        "*.phar",
        "/web/config.php",
    ],
    "ruby": [
        "# Ruby (added by vibe-coder)",
        ".bundle/",
        "spec/reports/",
        "test/tmp/",
        "test/version_tmp/",
        "*.gem",
        "!*.gemspec",
        ".rvm-version",
    ],
}

#: Names shown in the template picker dialog.
TEMPLATE_NAMES = sorted(TEMPLATES.keys())


def pretty_name(key: str) -> str:
    """Human-friendly template name: 'dotnet' -> 'Dotnet', 'javascript' -> 'Javascript'."""
    return key.capitalize() if key else key


def _core_block() -> list[str]:
    return list(_CORE_DEFAULTS)


def default_content(template_keys: list[str] | None = None) -> str:
    """Render a fresh .vibeignore: core defaults + chosen templates."""
    lines = ["# vibe-coder context filter — read ONLY by vibe-coder.",
             "# Files matching these patterns are hidden from the AI's",
             "# file listings and file reads to save context. This does",
             "# NOT affect git. Syntax = standard gitignore patterns.",
             ""]
    lines += _core_block()
    seen: set[str] = set()
    for key in template_keys or []:
        block = TEMPLATES.get(key.lower().strip())
        if not block:
            continue
        lines.append("")
        lines += [l for l in block if l.strip() not in seen]
        seen.update(l for l in block if l.strip())
    return "\n".join(lines) + "\n"


def merge_missing(path: Path, template_keys: list[str]) -> list[str]:
    """Append template blocks missing from an existing .vibeignore.

    Never rewrites or reorders user content — additions go at the end, so
    user negation rules (``!important.md``) placed earlier still win.
    Returns the template names that were added.
    """
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    existing = {line.strip() for line in current.splitlines() if line.strip()}

    added: list[str] = []
    appended: list[str] = []
    for key in template_keys:
        block = TEMPLATES.get(key.lower().strip())
        if not block:
            continue
        missing = [l for l in block[1:]  # skip the "# <lang>" header comment
                   if l.strip() and l.strip() not in existing]
        if not missing:
            continue
        appended.append("")
        appended.extend(block)
        existing.update(l.strip() for l in block if l.strip())
        added.append(pretty_name(key.lower().strip()))

    if not appended:
        return []

    text = current
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join(appended) + "\n"
    path.write_text(text, encoding="utf-8")
    return added


class VibeIgnore:
    """Compiled .vibeignore matcher for one project scope.

    Backed by a tiny built-in gitignore engine (no third-party dependency —
    requiring e.g. ``pathspec`` crashed the app whenever it was missing from
    the user's venv). Each rule carries a compiled regex plus flags describing
    whether it is a negation (``!``) and/or directory-only (trailing ``/``)
    pattern; matching follows gitignore precedence (last match wins).
    """

    class _Rule:
        __slots__ = ("regex", "negate", "dir_only")

        def __init__(self, regex, negate: bool, dir_only: bool):
            self.regex = regex
            self.negate = negate
            self.dir_only = dir_only

    def __init__(self, root: Path, rules=None, warnings: list[str] | None = None):
        self.root = Path(root)
        self._rules: list[VibeIgnore._Rule] = list(rules or [])
        self.warnings = list(warnings or [])

    @property
    def active(self) -> bool:
        """True when at least one pattern is in effect."""
        return bool(self._rules)

    @classmethod
    def compile(cls, lines, root: Path = None, warnings: list[str] | None = None
                ) -> "VibeIgnore":
        """Compile iterable of gitignore-style lines into a matcher.

        Invalid patterns are skipped with a warning rather than raising.
        """
        warns = list(warnings or [])
        rules: list[VibeIgnore._Rule] = []
        for lineno, line in enumerate(lines, start=1):
            text = line.rstrip("\n").rstrip("\r")
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                rule = cls._compile_line(stripped)
            except Exception:
                warns.append(
                    f"{VIBEIGNORE_FILENAME}:{lineno}: invalid pattern skipped: {stripped!r}"
                )
                continue
            if rule is not None:
                rules.append(rule)
        return cls(root or Path("."), rules, warns)

    @staticmethod
    def _compile_line(pattern: str) -> "VibeIgnore._Rule | None":
        import re

        negate = False
        if pattern.startswith("!"):
            negate = True
            pattern = pattern[1:].lstrip()
            if not pattern:
                return None
        if pattern.startswith("\\#") or pattern.startswith("\\!"):
            pattern = pattern[1:]
        if not pattern:
            return None

        dir_only = pattern.endswith("/")
        if dir_only:
            pattern = pattern[:-1]
        if not pattern:
            return None

        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        if pattern.startswith("/"):
            pattern = pattern[1:]

        # Translate the glob to a regex (gitignore semantics).
        i, n = 0, len(pattern)
        out: list[str] = []
        while i < n:
            c = pattern[i]
            if c == "*":
                if i + 1 < n and pattern[i + 1] == "*":
                    # '**' — crosses directory boundaries.
                    i += 2
                    if i < n and pattern[i] == "/":
                        out.append("(?:.*/)?")
                        i += 1
                    else:
                        out.append(".*")
                    continue
                out.append("[^/]*")
                i += 1
                continue
            if c == "?":
                out.append("[^/]")
                i += 1
                continue
            if c == "[":
                j = i + 1
                if j < n and pattern[j] == "!":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 1
                if j >= n:
                    out.append(re.escape("["))  # unterminated bracket: literal
                    i += 1
                    continue
                cls_body = pattern[i + 1:j]
                if cls_body.startswith("!"):
                    cls_body = "^" + cls_body[1:]
                elif cls_body.startswith("^"):
                    cls_body = "\\" + cls_body
                out.append("[" + cls_body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
            out.append(re.escape(c))
            i += 1

        body = "".join(out)
        prefix = "" if anchored else "(?:.*/)?"
        if dir_only:
            # Match both the directory itself and everything beneath it.
            regex = re.compile("^" + prefix + body + "(?:/.*)?$")
        else:
            regex = re.compile("^" + prefix + body + "$")
        return VibeIgnore._Rule(regex, negate, dir_only)

    def is_ignored(self, rel_path: str) -> bool:
        """True when ``rel_path`` (a file path or a dir path ending in '/')
        should be kept out of model context. Never raises.

        Directory-only rules also match every descendant, mirroring git:
        ``target/`` ignores ``target/debug/app`` even though the regex only
        matches the directory entry itself during a walk.
        """
        try:
            p = str(rel_path).replace("\\", "/")
            while p.startswith("./"):  # strip leading "./" only (never ".git"!)"
                p = p[2:]
            p = p.lstrip("/")
            if not p or p == ".":
                return False
            ignored = False
            for rule in self._rules:
                if rule.regex.match(p):
                    ignored = not rule.negate
            if not ignored and "/" in p.rstrip("/"):
                # A negated parent must not force-ignore children either;
                # re-evaluate the full rule set against each ancestor dir.
                parts = p.split("/")
                for k in range(1, len(parts)):
                    ancestor = "/".join(parts[:k]) + "/"
                    for rule in self._rules:
                        if rule.dir_only and rule.regex.match(ancestor):
                            ignored = not rule.negate
            return ignored
        except Exception:
            # A broken matcher must never hide real files or crash tools.
            return False


def load_vibeignore(scope: Path) -> tuple[VibeIgnore, list[str]]:
    """Parse ``scope/.vibeignore``. Missing/broken files degrade gracefully.

    Falls back to compiling the built-in core defaults when no file exists,
    so even legacy projects (created before .vibeignore support) don't waste
    context on build junk. Returns (matcher, warning_lines).
    """
    path = Path(scope) / VIBEIGNORE_FILENAME

    if path.exists():
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            vi = VibeIgnore.compile(_core_block(), scope)
            vi.warnings.insert(0, f"Could not read {VIBEIGNORE_FILENAME}: {exc}")
            return vi, vi.warnings
        matcher = VibeIgnore.compile(raw_lines, scope)
        return matcher, matcher.warnings

    # No usable file: apply core defaults so context stays lean anyway.
    matcher = VibeIgnore.compile(_core_block(), scope)
    return matcher, matcher.warnings


def ensure_vibeignore(scope: Path, template_keys: list[str] | None = None) -> tuple[bool, list[str]]:
    """Guarantee a project has a .vibeignore.

    Creates it with core defaults + selected templates when absent; when the
    file already exists, only appends template blocks that are missing.
    Returns (created_new, template_names_added).
    """
    scope = Path(scope)
    path = scope / VIBEIGNORE_FILENAME
    if path.exists():
        added = merge_missing(path, template_keys or [])
        return False, added
    path.write_text(default_content(template_keys), encoding="utf-8")
    return True, [pretty_name(k.lower().strip()) for k in (template_keys or [])
                  if k.lower().strip() in TEMPLATES]
