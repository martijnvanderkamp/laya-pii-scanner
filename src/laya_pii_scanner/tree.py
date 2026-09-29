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
              progress: Optional[Callable[["Progress"], None]] = None,
              on_start: Optional[Callable[[Discovery, int], None]] = None,
              on_file: Optional[Callable[[FileReport], None]] = None) -> TreeReport:
    """Scan a file, folder or git repository. See `files.discover` for what is skipped.

    on_start(discovery, total_bytes) runs once the files are listed, before any is scanned;
    progress(state) runs before each file and once at the end; on_file(report) runs after each
    scanned file, so a caller can show results while the scan continues.
    """
    start = time.perf_counter()
    target = target.resolve()
    if target.is_file():
        found = Discovery(target.parent, "file", [target])
    else:
        found = discover(target, include_untracked, excludes, max_bytes)
    sizes = [p.stat().st_size for p in found.files]
    state = Progress(files_total=len(found.files), bytes_total=sum(sizes))
    if on_start:
        on_start(found, state.bytes_total)
    reports: List[FileReport] = []
    for path, size in zip(found.files, sizes):
        state.current = path.relative_to(found.root).as_posix()
        if progress:
            progress(state)
        report = scan_file(scanner, path, state.current)
        state.files_done += 1
        state.bytes_done += size
        if report is None:
            found.skipped["not UTF-8 text"] += 1
            found.skipped_paths.append((state.current, "not UTF-8 text"))
            continue
        reports.append(report)
        if report.findings and report.verdict != "clean":
            state.files_with_findings += 1
        if on_file:
            on_file(report)
    state.current = ""
    if progress:
        progress(state)
    return TreeReport(str(found.root), found.mode, reports, dict(found.skipped),
                      [list(p) for p in found.skipped_paths], time.perf_counter() - start)


@dataclass
class Progress:
    """Where a running scan is. Byte counts drive the percentage, since file sizes vary widely."""
    files_total: int
    bytes_total: int
    files_done: int = 0
    bytes_done: int = 0
    files_with_findings: int = 0
    current: str = ""
    started: float = field(default_factory=time.perf_counter)

    @property
    def fraction(self) -> float:
        if self.bytes_total:
            return self.bytes_done / self.bytes_total
        return self.files_done / self.files_total if self.files_total else 1.0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def remaining(self) -> Optional[float]:
        """Estimated seconds left, once there is enough to go on."""
        f = self.fraction
        if f < 0.02 or self.elapsed < 3:
            return None
        return self.elapsed * (1 - f) / f
