"""Decide which files in a folder or git repository to scan, and which text in them.

Discovery: in a git repository only tracked files are listed (optionally also untracked files
that .gitignore does not exclude); elsewhere the folder is walked. Either way, dependency,
virtual-environment, cache and build folders, binary and media files, lock files, minified
bundles and oversized files are skipped, as are paths matched by --exclude patterns or a
.laya-pii-ignore file in the scanned folder.

Text: prose-like files (Markdown, text, CSV, JSON, YAML, HTML, notebooks, e-mails, ...) are
scanned whole. In source code only string literals, comments and long digit runs are kept,
at their original positions, so identifiers such as `PIIScanner` are never read as names
while line numbers stay correct.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IGNORE_FILE = ".laya-pii-ignore"
DEFAULT_MAX_BYTES = 1_000_000

# Folders that hold dependencies, virtual environments, caches or build output.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "virtualenv", "__pycache__", "node_modules",
    "bower_components", "jspm_packages", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".ipynb_checkpoints", "site-packages", ".eggs", ".next", ".nuxt",
    ".svelte-kit", ".terraform", ".gradle", ".idea", ".vscode", ".cache", "dist", "build",
    "target", "coverage", "htmlcov", "vendor", ".bundle", ".yarn", ".pnpm-store",
}
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info")
VENV_MARKER = "pyvenv.cfg"  # any folder containing this is a Python virtual environment

LOCK_FILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "pdm.lock", "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
    "go.sum", "packages.lock.json", "flake.lock", ".DS_Store",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd", ".svg",
    ".mp3", ".wav", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war", ".whl", ".egg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a", ".lib", ".class",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".db", ".sqlite", ".sqlite3", ".parquet", ".feather", ".pkl", ".pickle", ".npy", ".npz",
    ".h5", ".onnx", ".pt", ".pth", ".safetensors", ".ckpt",
}
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".js.map", ".css.map", ".map")


# ---------------------------------------------------------------- source code


@dataclass(frozen=True)
class CodeSyntax:
    line_comments: Tuple[str, ...] = ()
    block_comments: Tuple[Tuple[str, str], ...] = ()
    triple_quotes: bool = False
    backticks: bool = False


_HASH = CodeSyntax(("#",))
_PYTHON = CodeSyntax(("#",), triple_quotes=True)
_C = CodeSyntax(("//",), (("/*", "*/"),))
_JS = CodeSyntax(("//",), (("/*", "*/"),), backticks=True)
_SQL = CodeSyntax(("--",), (("/*", "*/"),))
CODE_SYNTAX: Dict[str, CodeSyntax] = {
    ".py": _PYTHON, ".pyi": _PYTHON, ".pyx": _PYTHON,
    ".sh": _HASH, ".bash": _HASH, ".zsh": _HASH, ".fish": _HASH, ".rb": _HASH, ".pl": _HASH,
    ".pm": _HASH, ".r": _HASH, ".toml": _HASH, ".nix": _HASH,
    ".tf": CodeSyntax(("#", "//"), (("/*", "*/"),)),
    ".ps1": CodeSyntax(("#",), (("<#", "#>"),)), ".psm1": CodeSyntax(("#",), (("<#", "#>"),)),
    ".js": _JS, ".jsx": _JS, ".mjs": _JS, ".cjs": _JS, ".ts": _JS, ".tsx": _JS,
    ".java": _C, ".kt": _C, ".kts": _C, ".scala": _C, ".groovy": _C, ".gradle": _C,
    ".go": _JS, ".rs": _C, ".c": _C, ".h": _C, ".cc": _C, ".cpp": _C, ".hpp": _C, ".cxx": _C,
    ".cs": _C, ".swift": _C, ".dart": _C, ".php": CodeSyntax(("//", "#"), (("/*", "*/"),)),
    ".css": CodeSyntax((), (("/*", "*/"),)), ".scss": _C, ".less": _C,
    ".sql": _SQL, ".lua": CodeSyntax(("--",), (("--[[", "]]"),)),
    ".hs": CodeSyntax(("--",), (("{-", "-}"),)),
}
CODE_FILENAMES: Dict[str, CodeSyntax] = {"Dockerfile": _HASH, "Makefile": _HASH, "Justfile": _HASH}


def syntax_for(path: Path) -> Optional[CodeSyntax]:
    """The comment syntax when `path` is source code, None when it is prose to scan whole."""
    return CODE_FILENAMES.get(path.name) or CODE_SYNTAX.get(path.suffix.lower())


HTML_SUFFIXES = {".html", ".htm", ".xhtml", ".vue", ".svelte"}
_HTML_HIDDEN = re.compile(r"<(script|style|svg)\b[\s\S]*?</\1\s*>|<[^>]*>", re.I)


def html_view(text: str) -> str:
    """`text` with tags, scripts, styles and inline SVG blanked out, keeping the visible text.

    Each removed tag becomes a line break followed by spaces, so text in neighbouring cells
    never runs together into one name. Offsets stay the same as in the original file.
    """
    out = list(text)
    for m in _HTML_HIDDEN.finditer(text):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = "\n" if i == m.start() else " "
    return "".join(out)


def view_for(path: Path, text: str) -> Tuple[str, str]:
    """("code" | "html" | "text", the part of `text` to scan, with offsets preserved)."""
    if path.suffix.lower() in HTML_SUFFIXES:
        return "html", html_view(text)
    syntax = syntax_for(path)
    return ("code", code_view(text, syntax)) if syntax else ("text", text)


def _code_regex(s: CodeSyntax) -> re.Pattern:
    parts = []
    for start, end in sorted(s.block_comments, key=lambda b: -len(b[0])):  # "--[[" before "--"
        parts.append(re.escape(start) + r"[\s\S]*?(?:" + re.escape(end) + r"|\Z)")
    if s.triple_quotes:
        parts += [r'"""[\s\S]*?(?:"""|\Z)', r"'''[\s\S]*?(?:'''|\Z)"]
    parts += [re.escape(m) + r"[^\n]*" for m in s.line_comments]
    parts += [r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'"]
    if s.backticks:
        parts.append(r"`(?:\\.|[^`\\])*`")
    parts.append(r"(?<![\w.])\d[\d_]{5,}(?![\w.])")  # BSNs and account numbers written as numbers
    return re.compile("|".join(parts))


_REGEX_CACHE: Dict[CodeSyntax, re.Pattern] = {}


def code_view(text: str, syntax: CodeSyntax) -> str:
    """`text` with everything except strings, comments and long numbers blanked out.

    Blanked characters become spaces and newlines are kept, so offsets, line and column
    numbers in the result are the same as in the original file.
    """
    rx = _REGEX_CACHE.setdefault(syntax, _code_regex(syntax))
    out = [c if c == "\n" else " " for c in text]
    for m in rx.finditer(text):
        out[m.start():m.end()] = text[m.start():m.end()]
    return "".join(out)


# ---------------------------------------------------------------- discovery


@dataclass
class Discovery:
    root: Path
    mode: str                                   # "git" (tracked files) or "folder"
    files: List[Path] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    skipped_paths: List[Tuple[str, str]] = field(default_factory=list)   # (relative path, reason)


def load_ignore_patterns(root: Path) -> List[str]:
    path = root / IGNORE_FILE
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def is_excluded(rel: str, patterns: Sequence[str]) -> bool:
    """gitignore-like matching: "dir/" matches a folder anywhere, "*.csv" a file name, "a/b.txt" a path."""
    parts = rel.split("/")
    for p in patterns:
        p = p.lstrip("/")
        if p.endswith("/"):
            d = p.rstrip("/")
            if "/" in d:
                if rel.startswith(d + "/"):
                    return True
            elif any(fnmatch.fnmatch(part, d) for part in parts[:-1]):
                return True
        elif fnmatch.fnmatch(rel, p) or ("/" not in p and fnmatch.fnmatch(parts[-1], p)):
            return True
    return False


def _git_files(root: Path, include_untracked: bool) -> Optional[List[str]]:
    try:
        inside = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True)
    except OSError:
        return None  # git is not installed
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    cmd = ["git", "-C", str(root), "ls-files", "-z", "--cached"]
    if include_untracked:
        cmd += ["--others", "--exclude-standard"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8", "replace")
    return sorted({p for p in out.split("\0") if p})


def _walk_files(root: Path) -> Tuple[List[str], List[str]]:
    """(files to consider, folders pruned without descending into them)."""
    rels, pruned = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        keep = []
        for n in dirnames:
            if (d / n).is_symlink():
                continue
            if n in SKIP_DIRS or n.endswith(SKIP_DIR_SUFFIXES) or (d / n / VENV_MARKER).exists():
                pruned.append((d / n).relative_to(root).as_posix() + "/")
            else:
                keep.append(n)
        dirnames[:] = keep
        rels += [(d / f).relative_to(root).as_posix() for f in filenames]
    return sorted(rels), sorted(pruned)


def _skip_reason(root: Path, rel: str, max_bytes: int, venv_cache: Dict[str, bool]) -> Optional[str]:
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if part in SKIP_DIRS or part.endswith(SKIP_DIR_SUFFIXES):
            return "dependency, cache or build folder"
        folder = "/".join(parts[: i + 1])
        if folder not in venv_cache:
            venv_cache[folder] = (root / folder / VENV_MARKER).exists()
        if venv_cache[folder]:
            return "dependency, cache or build folder"
    name = parts[-1]
    lower = name.lower()
    if name in LOCK_FILES:
        return "lock file"
    if lower.endswith(MINIFIED_SUFFIXES):
        return "minified or source map"
    if Path(lower).suffix in BINARY_SUFFIXES:
        return "binary or media file"
    path = root / rel
    if path.is_symlink() or not path.is_file():
        return "not a regular file"
    size = path.stat().st_size
    if size == 0:
        return "empty"
    if size > max_bytes:
        return "larger than --max-size"
    with path.open("rb") as fh:
        head = fh.read(8192)
    if b"\0" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 has NULs too
        return "binary or media file"
    return None


def discover(root: Path, include_untracked: bool = False, excludes: Iterable[str] = (),
             max_bytes: int = DEFAULT_MAX_BYTES) -> Discovery:
    """List the files under `root` worth scanning, with a count of what was skipped and why."""
    root = root.resolve()
    tracked = _git_files(root, include_untracked)
    found = Discovery(root, "git" if tracked is not None else "folder")
    patterns = list(excludes) + load_ignore_patterns(root)
    venv_cache: Dict[str, bool] = {}
    if tracked is None:
        tracked, pruned = _walk_files(root)
        for folder in pruned:  # counted once per folder, not per file inside it
            found.skipped["dependency, cache or build folder"] += 1
            found.skipped_paths.append((folder, "dependency, cache or build folder"))
    for rel in tracked:
        reason = "excluded by pattern" if is_excluded(rel, patterns) else \
            _skip_reason(root, rel, max_bytes, venv_cache)
        if reason:
            found.skipped[reason] += 1
            found.skipped_paths.append((rel, reason))
        else:
            found.files.append(root / rel)
    return found


def read_text(path: Path) -> Optional[str]:
    """The file as text, or None when it is not UTF-8 (or UTF-16 with a byte-order mark)."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return None
