"""Which question design best separates "about a specific person" from general mentions?

Reproduces the comparison behind SPECIAL_STATEMENT in the scanner: for every (case, category)
pair in the tuning set where a sensitive keyword occurs, each design scores whether the text
says it about one specific person. Reports AUC and the best accuracy per design and checkpoint.

    python eval/experiment_special.py
"""
import json
import os
import sys
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from laya import Router
from laya_pii_scanner import scanner as ps

DATA = Path(__file__).parent / "data" / "tune.jsonl"
DESC = {  # the per-keyword phrasing of design F1
    "health": "the health, illness, disability, pregnancy or medical treatment of a specific person",
    "sexuality": "the sexual orientation or gender identity of a specific person",
    "religion": "the religion or belief of a specific person",
    "politics": "the political opinion or party membership of a specific person",
    "union": "the trade union membership of a specific person",
    "ethnicity": "the ethnic origin, race or migration background of a specific person",
    "criminal": "an arrest, suspicion, conviction or sentence of a specific person",
}
VERB = ps.SPECIAL_STATEMENT

DESIGNS = {
    "F1 keyword noul": lambda c, cat, h: (
        {"text": c["text"], "candidate": h},
        {cat: {"type": "noul", "instructions": f"In `text`, does `candidate` say something about {DESC[cat]}?"}},
        lambda a: a[cat]["noul"]),
    "F2 keyword about-whom choice": lambda c, cat, h: (
        {"text": c["text"], "candidate": h},
        {"who": {"type": "choice", "instructions": "In `text`, who or what is `candidate` about?",
                 "criteria": {"person": "one specific person, named or not, such as 'she', 'my son', 'the patient' or 'Mark'",
                              "general": "people in general, a group, statistics, an organisation, a place or a building"}}},
        lambda a: a["who"]["probabilities"]["person"]),
    "F3 statement noul (used)": lambda c, cat, h: (
        {"text": c["text"]},
        {"q": {"type": "noul", "instructions": f"Does `text` say that one specific person {VERB[cat]}?"}},
        lambda a: a["q"]["noul"]),
    "F4 statement choice": lambda c, cat, h: (
        {"text": c["text"]},
        {"q": {"type": "choice", "instructions": f"What does `text` say about whether someone {VERB[cat]}?",
               "criteria": {"specific": "it says that one specific person does, named or not",
                            "general": "it only talks about people in general, a group, statistics or an institution",
                            "none": "it does not say this about anyone"}}},
        lambda a: a["q"]["probabilities"]["specific"]),
}


def main():
    cases = [json.loads(line) for line in DATA.open(encoding="utf-8") if line.strip()]
    pairs = []
    for c in cases:
        for cat, rx in ps.LEXICON_RE.items():
            hits = [m.group() for m in rx.finditer(c["text"])]
            if hits:
                pairs.append((c, cat, hits, cat in c["expected"]))
    print(f"{len(pairs)} keyword pairs, {sum(p[3] for p in pairs)} positive")

    router = Router()
    router.preload(["english", "multilingual"])
    for mode in ["auto", "multilingual"]:
        kw = {} if mode == "auto" else {"model": mode}
        for name, make in DESIGNS.items():
            scores = []
            for c, cat, hits, gold in pairs:
                best = 0.0
                for h in (hits if "keyword" in name else [None]):
                    state, questions, get = make(c, cat, h)
                    best = max(best, get(router.predict(state, questions, **kw)["answers"]))
                scores.append((best, gold))
            acc, t = max((sum((s >= t) == g for s, g in scores) / len(scores), t) for t in [i / 20 for i in range(1, 20)])
            pos = [s for s, g in scores if g]
            neg = [s for s, g in scores if not g]
            auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
            print(f"[{mode:<12}] {name:<30} AUC {auc:.2f}  best accuracy {acc:.2f} at {t:.2f}")


if __name__ == "__main__":
    main()
