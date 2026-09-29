"""laya-pii: find personal data in Dutch and English text, files, folders and git repositories.

    laya-pii                              paste text, finish with an empty line; Ctrl+C quits
    laya-pii letter.txt                   detailed report for one file (--redact, --json)
    laya-pii .                            scan a folder or git repository
    laya-pii ~/src/app --fail-on special  exit code 1 only for special category data
    laya-pii ~/src/app --fast --json      rules only (no model), JSON for other tools

In a git repository only tracked files are scanned (add --include-untracked for new files
that .gitignore allows). Virtual environments, dependency, cache and build folders, binary
files, lock files and minified bundles are always skipped; add your own with --exclude or a
.laya-pii-ignore file. In source code only strings, comments and long numbers are scanned.

Exit codes: 0 nothing at the --fail-on level, 1 something found, 2 usage error.
A screening aid, not a guarantee: see the README for measured accuracy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

from . import __version__
from .files import DEFAULT_MAX_BYTES, read_text, view_for
from .scanner import CATEGORIES, THRESHOLDS, ChunkResult, PIIScanner, redact
from .tree import FAIL_LEVELS, TreeReport, scan_tree

_COLOUR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RESET = ("\033[1m", "\033[2m", "\033[0m") if _COLOUR else ("", "", "")
VERDICT_COLOUR = {"clean": "\033[38;5;71m", "personal": "\033[38;5;214m",
                  "special": "\033[38;5;203m", "review": "\033[38;5;221m"} if _COLOUR else {}
VERDICT_LABEL = {"clean": "CLEAN", "personal": "PERSONAL DATA",
                 "special": "SPECIAL CATEGORY DATA", "review": "REVIEW"}
FOOTNOTE = "'?' = below the threshold, shown for review. A screening aid, not a guarantee."


# ---------------------------------------------------------------- one text or file


def report(text: str, chunks: List[ChunkResult], elapsed: float, show_redacted: bool) -> None:
    for i, ch in enumerate(chunks, 1):
        v = ch.verdict
        cats = sorted({CATEGORIES[f.category][0].lower() for f in ch.findings if f.confirmed})
        extra = f" ({', '.join(cats)})" if cats else ""
        print(f"\n{VERDICT_COLOUR.get(v, '')}{BOLD}── Passage {i}/{len(chunks)} · {VERDICT_LABEL[v]}{extra}{RESET}")
        preview = ch.text.replace("\n", " ")
        print(f"{DIM}   {preview[:110]}{'…' if len(preview) > 110 else ''}{RESET}")
        for f in ch.findings:
            if not (f.confirmed or f.review):
                continue
            mark = "  " if f.confirmed else "? "
            label = CATEGORIES[f.category][0]
            print(f"   {mark}{label:<26}{f.text[:40]!r:<44}{f.confidence:>5.2f}  {DIM}{f.source}{RESET}")
    counts: Dict[str, int] = {}
    for ch in chunks:
        counts[ch.verdict] = counts.get(ch.verdict, 0) + 1
    summary = ", ".join(f"{n}× {v}" for v, n in counts.items())
    print(f"\n{DIM}{len(chunks)} passage(s): {summary} · {elapsed * 1000:.0f} ms{RESET}")
    print(f"{DIM}{FOOTNOTE}{RESET}")
    if show_redacted:
        print(f"\n{BOLD}Redacted:{RESET}\n{redact(text, chunks)}")


def to_json(text: str, chunks: List[ChunkResult]) -> dict:
    return {"passages": [{"start": c.start, "end": c.end, "verdict": c.verdict,
                          "findings": [dict(asdict(f), confirmed=f.confirmed, review=f.review)
                                       for f in c.findings]}
                         for c in chunks],
            "redacted": redact(text, chunks)}


# ---------------------------------------------------------------- folders and repositories


def tree_report(rep: TreeReport, show_skipped: bool, show_review: bool) -> None:
    kind = {"git": "git repository, tracked files", "folder": "folder", "file": "file"}[rep.mode]
    skipped = sum(rep.skipped.values())
    reasons = ", ".join(f"{n} {r}" for r, n in sorted(rep.skipped.items(), key=lambda kv: -kv[1]))
    print(f"\n{BOLD}{rep.root}{RESET} {DIM}· {kind} · {len(rep.files)} files scanned"
          f"{f' · skipped {skipped}: {reasons}' if skipped else ''}{RESET}")
    if show_skipped:
        for rel, reason in rep.skipped_paths:
            print(f"{DIM}   skipped  {rel}  ({reason}){RESET}")
    for f in rep.files:
        if not f.findings:
            continue
        print(f"\n{VERDICT_COLOUR.get(f.verdict, '')}{BOLD}{f.path}{RESET}  "
              f"{VERDICT_COLOUR.get(f.verdict, '')}{VERDICT_LABEL[f.verdict]}{RESET}")
        for x in f.findings:
            if not (x.confirmed or show_review):
                continue
            where = f"{x.line}:{x.column}" if x.column else f"{x.line}"
            mark = "  " if x.confirmed else "? "
            print(f"   {mark}{where:<9}{CATEGORIES[x.category][0]:<26}{x.text[:40]!r:<44}"
                  f"{x.confidence:>5.2f}  {DIM}{x.source}{RESET}")
        hidden = 0 if show_review else sum(not x.confirmed for x in f.findings)
        if hidden:
            print(f"{DIM}   + {hidden} below the threshold (--show-review lists them){RESET}")
    c = rep.counts()
    print(f"\n{BOLD}{len(rep.files)} files in {rep.seconds:.1f} s:{RESET} "
          f"{c['special']} special category data · {c['personal']} personal data · "
          f"{c['review']} review · {c['clean']} clean")


def progress(i: int, n: int, rel: str) -> None:
    line = f"[{i + 1}/{n}] {rel}"
    sys.stderr.write("\r" + line[:100].ljust(100))
    sys.stderr.flush()


# ---------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(prog="laya-pii", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", help="files, folders or git repositories to scan")
    p.add_argument("--file", help="scan this file (same as giving it as a path)")
    p.add_argument("--redact", action="store_true", help="single file: print it with confirmed findings masked")
    p.add_argument("--json", action="store_true", help="print JSON instead of a report")
    p.add_argument("--device", help="cuda or cpu (default: cuda when available)")
    p.add_argument("--offline", action="store_true", help="use cached model files only, no network")
    p.add_argument("--fast", action="store_true",
                   help="rules only, no model: finds formats, checksums and cued names, "
                        "but not other names or special category data")
    p.add_argument("--threshold", action="append", default=[], metavar="NAME=VALUE",
                   help="override a threshold, e.g. name=0.9 to report fewer uncertain names "
                        f"(names: {', '.join(k for k in THRESHOLDS if k != 'review')}; repeatable)")
    p.add_argument("--fail-on", choices=list(FAIL_LEVELS), default="personal",
                   help="lowest verdict that makes the exit code 1 (default: personal)")
    g = p.add_argument_group("folders and repositories")
    g.add_argument("--include-untracked", action="store_true",
                   help="git: also scan untracked files that .gitignore does not exclude")
    g.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                   help="skip paths matching this pattern, e.g. 'tests/' or '*.csv' (repeatable)")
    g.add_argument("--max-size", type=int, default=DEFAULT_MAX_BYTES // 1000, metavar="KB",
                   help=f"skip files larger than this (default: {DEFAULT_MAX_BYTES // 1000} KB)")
    g.add_argument("--show-skipped", action="store_true", help="list every skipped file and why")
    g.add_argument("--show-review", action="store_true",
                   help="also list findings below the threshold (always included in --json)")
    p.add_argument("--version", action="version", version=f"laya-pii {__version__}")
    args = p.parse_args()

    for item in args.threshold:
        key, _, value = item.partition("=")
        try:
            if key not in THRESHOLDS or key == "review":
                raise ValueError
            THRESHOLDS[key] = float(value)
        except ValueError:
            p.error(f"--threshold {item!r}: expected NAME=VALUE with NAME one of "
                    f"{', '.join(k for k in THRESHOLDS if k != 'review')}")
    targets = [Path(t) for t in args.paths + ([args.file] if args.file else [])]
    for t in targets:
        if not t.exists():
            p.error(f"{t} does not exist")
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    if os.name == "nt":
        os.system("")  # enables ANSI colours in older Windows consoles
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.json and not args.fast:
        print(f"{DIM}Loading Laya…{RESET}", flush=True, file=sys.stderr)
    scanner = PIIScanner(device=args.device, rules_only=args.fast)
    fail = FAIL_LEVELS[args.fail_on]

    def run_text(text: str) -> bool:
        start = time.perf_counter()
        chunks = scanner.scan(text)
        elapsed = time.perf_counter() - start
        if args.json:
            print(json.dumps(to_json(text, chunks), ensure_ascii=False, indent=1))
        else:
            report(text, chunks, elapsed, args.redact)
        return any(ch.verdict in fail for ch in chunks)

    if len(targets) == 1 and targets[0].is_file():
        text = read_text(targets[0])
        if text is None:
            p.error(f"{targets[0]} is not a UTF-8 text file")
        if view_for(targets[0], text)[0] == "text":  # one prose file: the detailed passage report
            sys.exit(1 if run_text(text) else 0)
        # source code and HTML go through the file scan below, which reads strings and comments
        # or visible text only

    if targets:  # folders, repositories or several files
        show_progress = sys.stderr.isatty() and not args.json
        reports = []
        for t in targets:
            rep = scan_tree(scanner, t, args.include_untracked, args.exclude, args.max_size * 1000,
                            progress if show_progress else None)
            if show_progress:
                sys.stderr.write("\r" + " " * 100 + "\r")
            reports.append(rep)
            if not args.json:
                tree_report(rep, args.show_skipped, args.show_review)
        if args.json:
            out = [r.to_dict() for r in reports]
            print(json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False, indent=1))
        else:
            print(f"{DIM}{FOOTNOTE}{RESET}")
        sys.exit(1 if any(r.fails(args.fail_on) for r in reports) else 0)

    print(f"{BOLD}Paste text and finish with an empty line. Ctrl+C quits.{RESET}")
    while True:
        lines: List[str] = []
        try:
            prompt = "\n> "
            while True:
                line = input(prompt)
                prompt = "  "
                if not line.strip():
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            if lines:
                run_text("\n".join(lines))
            print()
            return
        if lines:
            run_text("\n".join(lines))


if __name__ == "__main__":
    main()
