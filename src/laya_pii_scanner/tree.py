"""Scan a folder or git repository file by file and summarise what was found where."""
from __future__ import annotations

import bisect
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .files import DEFAULT_MAX_BYTES, Discovery, discover, read_text, view_for
from .scanner import PIIScanner

VERDICT_ORDER = ["clean", "review", "personal", "special"]
FAIL_LEVELS = {"special": {"special"}, "personal": {"special", "personal"},
               "review": {"special", "personal", "review"}, "never": set()}


@dataclass
class FileFinding:
    category: str
    text: str
    line: int
    column: Optional[int]      # None for a finding about a whole passage
    confidence: float
    source: str                # "rule" or "laya"
    confirmed: bool            # False: below the threshold, shown for review


@dataclass
class FileReport:
    path: str                  # relative to the scanned root, with forward slashes
    kind: str                  # "text" (whole file), "code" (strings, comments, numbers) or "html" (visible text)
    verdict: str               # "special", "personal", "review" or "clean"
    findings: List[FileFinding] = field(default_factory=list)


@dataclass
class TreeReport:
    root: str
    mode: str                  # "git" (tracked files), "folder" or "file"
    files: List[FileReport]
    skipped: Dict[str, int]
    skipped_paths: List[List[str]]
    seconds: float

    def counts(self) -> Dict[str, int]:
        out = {v: 0 for v in reversed(VERDICT_ORDER)}
        for f in self.files:
            out[f.verdict] += 1
        return out

    def fails(self, fail_on: str) -> bool:
        return any(f.verdict in FAIL_LEVELS[fail_on] for f in self.files)

    def to_dict(self) -> dict:
        return {"root": self.root, "mode": self.mode, "summary": self.counts(),
                "skipped": self.skipped, "seconds": round(self.seconds, 2),
                "files": [asdict(f) for f in self.files if f.findings]}


def scan_file(scanner: PIIScanner, path: Path, rel: str) -> Optional[FileReport]:
    """Scan one file. Returns None when it is not readable as text."""
    text = read_text(path)
    if text is None:
        return None
    kind, view = view_for(path, text)
    # in source code comments and strings are fragments: only keywords point at sensitive data
    chunks = scanner.scan(view, chunk_check=False if kind == "code" else None)
    line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    report = FileReport(rel, kind,
                        max((ch.verdict for ch in chunks), key=VERDICT_ORDER.index, default="clean"))
    for ch in chunks:
        for f in ch.findings:
            if not (f.confirmed or f.review):
                continue
            offset = f.start if f.start is not None else ch.start
            line = bisect.bisect_right(line_starts, offset)
            column = offset - line_starts[line - 1] + 1 if f.start is not None else None
            snippet = text[f.start:f.end] if f.start is not None else f.text
            report.findings.append(FileFinding(f.category, snippet, line, column, round(f.confidence, 4),
                                               f.source, f.confirmed))
    return report


def scan_tree(scanner: PIIScanner, target: Path, include_untracked: bool = False,
              excludes: Iterable[str] = (), max_bytes: int = DEFAULT_MAX_BYTES,
              progress: Optional[Callable[[int, int, str], None]] = None) -> TreeReport:
    """Scan a file, folder or git repository. See `files.discover` for what is skipped."""
    start = time.perf_counter()
    target = target.resolve()
    if target.is_file():
        found = Discovery(target.parent, "file", [target])
    else:
        found = discover(target, include_untracked, excludes, max_bytes)
    reports: List[FileReport] = []
    for i, path in enumerate(found.files):
        rel = path.relative_to(found.root).as_posix()
        if progress:
            progress(i, len(found.files), rel)
        report = scan_file(scanner, path, rel)
        if report is None:
            found.skipped["not UTF-8 text"] += 1
            found.skipped_paths.append((rel, "not UTF-8 text"))
        else:
            reports.append(report)
    return TreeReport(str(found.root), found.mode, reports, dict(found.skipped),
                      [list(p) for p in found.skipped_paths], time.perf_counter() - start)
