"""Measure the scanner on labelled cases.

    python eval/evaluate.py eval/data/test2.jsonl --errors                       report + mistakes
    python eval/evaluate.py eval/data/tune.jsonl eval/data/test1.jsonl --sweep  explore thresholds

Each case is scanned once; thresholds are then applied to the raw scores, so a sweep costs
no extra model calls. Cases are JSON lines: {"id", "lang", "text", "expected": [categories]}.
Run it with the model cached, or drop HF_HUB_OFFLINE for the first download.
"""
import argparse
import json
import os
import sys
import warnings

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from laya_pii_scanner import scanner as ps

TUNABLE = ["name", "id_number", "dob", "special", "chunk_special"]


def key_for(f):
    """Which threshold decides this finding (None: a rule, which carries its own threshold)."""
    if f.source == "rule":
        return None
    if f.start is None:
        return "chunk_special"
    return f.category if f.category in ("name", "id_number", "dob") else "special"


def predicted(findings, th):
    return {f.category for f in findings
            if (f.confirmed if key_for(f) is None else f.confidence >= th[key_for(f)])}


def metrics(cases, raw, th):
    cats = list(ps.CATEGORIES)
    tp = {c: 0 for c in cats}
    fp, fn = dict(tp), dict(tp)
    any_tp = any_fp = any_fn = exact = verdict_ok = 0

    def verdict(s):
        return "special" if s & set(ps.SPECIAL) else ("personal" if s else "clean")

    for case, findings in zip(cases, raw):
        pred, gold = predicted(findings, th), set(case["expected"])
        for c in cats:
            tp[c] += c in pred and c in gold
            fp[c] += c in pred and c not in gold
            fn[c] += c not in pred and c in gold
        any_tp += bool(pred) and bool(gold)
        any_fp += bool(pred) and not gold
        any_fn += not pred and bool(gold)
        exact += pred == gold
        verdict_ok += verdict(pred) == verdict(gold)

    def prf(t, p, n):
        prec = t / (t + p) if t + p else 1.0
        rec = t / (t + n) if t + n else 1.0
        return prec, rec, (2 * prec * rec / (prec + rec) if prec + rec else 0.0)

    per = {c: (prf(tp[c], fp[c], fn[c]), tp[c] + fn[c]) for c in cats}
    micro = prf(sum(tp.values()), sum(fp.values()), sum(fn.values()))
    return {"per": per, "micro": micro, "any": prf(any_tp, any_fp, any_fn),
            "exact": exact / len(cases), "verdict": verdict_ok / len(cases)}


def group_f1(cases, raw, th, key):
    """Micro-F1 over the categories one threshold controls."""
    cats = list(ps.SPECIAL) if key in ("special", "chunk_special") else [key]
    t = p = n = 0
    for case, findings in zip(cases, raw):
        pred, gold = predicted(findings, th), set(case["expected"])
        for c in cats:
            t += c in pred and c in gold
            p += c in pred and c not in gold
            n += c not in pred and c in gold
    return 2 * t / (2 * t + p + n) if t + p + n else 1.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="+", help="one or more .jsonl files")
    ap.add_argument("--sweep", action="store_true", help="show F1 per value for each tunable threshold")
    ap.add_argument("--errors", action="store_true", help="list every case whose categories differ")
    ap.add_argument("--no-chunk-check", action="store_true", help="skip the safety net for sensitive data")
    args = ap.parse_args()

    cases = [json.loads(line) for path in args.data for line in open(path, encoding="utf-8") if line.strip()]
    ps.THRESHOLDS["review"] = 0.02  # keep low scores so thresholds can be swept offline
    scanner = ps.PIIScanner(chunk_check=not args.no_chunk_check)
    scanner.scan("Warm-up voor de GPU, Jan Jansen, diabetes.")
    raw = [[f for ch in scanner.scan(c["text"]) for f in ch.findings] for c in cases]
    th = {k: ps.THRESHOLDS[k] for k in TUNABLE}

    if args.sweep:
        print("threshold sweep (group F1 per value; current value marked *):")
        for key in TUNABLE:
            row, best = [], (None, -1)
            for v in [0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
                f1 = group_f1(cases, raw, dict(th, **{key: v}), key)
                row.append(f"{v:.2f}{'*' if abs(v - th[key]) < 1e-9 else ' '}{f1:.2f}")
                if f1 > best[1] + 1e-9:
                    best = (v, f1)
            print(f"  {key:<14} " + "  ".join(row) + f"   best {best[0]}")

    m = metrics(cases, raw, th)
    print(f"\n{' + '.join(args.data)}: {len(cases)} cases · thresholds {th}")
    print(f"  contains personal data?  precision {m['any'][0]:.2f}  recall {m['any'][1]:.2f}")
    print(f"  all categories (micro)   precision {m['micro'][0]:.2f}  recall {m['micro'][1]:.2f}  F1 {m['micro'][2]:.2f}")
    print(f"  verdict (clean / personal / special) correct: {m['verdict']:.2f} · exact category match: {m['exact']:.2f}")
    print(f"  {'category':<12}{'n':>4}{'prec':>7}{'recall':>8}")
    for c, ((p, r, _), n) in m["per"].items():
        if n or p < 1.0:
            print(f"  {c:<12}{n:>4}{p:>7.2f}{r:>8.2f}")

    if args.errors:
        print("\nmistakes:")
        for case, findings in zip(cases, raw):
            pred, gold = predicted(findings, th), set(case["expected"])
            if pred != gold:
                miss, extra = sorted(gold - pred), sorted(pred - gold)
                print(f"  {case['id']} {case['text'][:90]!r}")
                print(f"      missed {miss or '-'} · extra {extra or '-'}")
                for f in findings:
                    if f.category in set(miss) | set(extra) and f.confidence >= 0.1:
                        print(f"        {f.category:<10} {f.text[:30]!r:<34} {f.confidence:.2f} ({key_for(f) or 'rule'})")


if __name__ == "__main__":
    main()
