"""laya-pii: scan Dutch and English text for personal data.

    laya-pii                              paste text, finish with an empty line; Ctrl+C quits
    laya-pii --file letter.txt            scan a file
    laya-pii --file letter.txt --redact   also print the text with findings masked
    laya-pii --file letter.txt --json     JSON for other tools
    laya-pii --offline                    use the cached model only (after the first run)

A screening aid, not a guarantee: see the README for measured accuracy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from typing import Dict, List

from .scanner import CATEGORIES, ChunkResult, PIIScanner, redact

_COLOUR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RESET = ("\033[1m", "\033[2m", "\033[0m") if _COLOUR else ("", "", "")
VERDICT_COLOUR = {"clean": "\033[38;5;71m", "personal": "\033[38;5;214m",
                  "special": "\033[38;5;203m", "review": "\033[38;5;221m"} if _COLOUR else {}
VERDICT_LABEL = {"clean": "CLEAN", "personal": "PERSONAL DATA",
                 "special": "SPECIAL CATEGORY DATA", "review": "REVIEW"}


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
    print(f"{DIM}'?' = below the threshold, shown for review. A screening aid, not a guarantee.{RESET}")
    if show_redacted:
        print(f"\n{BOLD}Redacted:{RESET}\n{redact(text, chunks)}")


def to_json(text: str, chunks: List[ChunkResult]) -> dict:
    return {"passages": [{"start": c.start, "end": c.end, "verdict": c.verdict,
                          "findings": [dict(asdict(f), confirmed=f.confirmed, review=f.review)
                                       for f in c.findings]}
                         for c in chunks],
            "redacted": redact(text, chunks)}


def main() -> None:
    p = argparse.ArgumentParser(prog="laya-pii", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="scan this file instead of reading pasted text")
    p.add_argument("--redact", action="store_true", help="print the text with confirmed findings masked")
    p.add_argument("--json", action="store_true", help="print JSON instead of a report")
    p.add_argument("--device", help="cuda or cpu (default: cuda when available)")
    p.add_argument("--offline", action="store_true", help="use cached model files only, no network")
    args = p.parse_args()
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    if os.name == "nt":
        os.system("")  # enables ANSI colours in older Windows consoles
    for stream in (sys.stdout, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.json:
        print(f"{DIM}Loading Laya…{RESET}", flush=True)
    scanner = PIIScanner(device=args.device)

    def run(text: str) -> None:
        start = time.perf_counter()
        chunks = scanner.scan(text)
        elapsed = time.perf_counter() - start
        if args.json:
            print(json.dumps(to_json(text, chunks), ensure_ascii=False, indent=1))
        else:
            report(text, chunks, elapsed, args.redact)

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            run(fh.read())
        return
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
                run("\n".join(lines))
            print()
            return
        if lines:
            run("\n".join(lines))


if __name__ == "__main__":
    main()
