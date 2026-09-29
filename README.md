# laya-pii-scanner

[![tests](https://github.com/martijnvanderkamp/laya-pii-scanner/actions/workflows/tests.yml/badge.svg)](https://github.com/martijnvanderkamp/laya-pii-scanner/actions/workflows/tests.yml)

Find personal data in Dutch and English text, on your own machine. Paste a piece of text
and get a verdict per passage (clean, personal data, special category data, or review), the
findings with a confidence score, and optionally a redacted copy.

It combines checksummed patterns (IBAN, BSN, payment cards) and context rules with
[Laya](https://github.com/NandhaKishorM/laya), an open-weights decision model that answers
typed questions about text in milliseconds. Nothing leaves your machine except the one-time
model download.

> **A screening aid, not a guarantee.** On an independent test set it spots 95% of the texts
> that contain personal data, but it misses about half of the sensitive health mentions and
> some names. Use it to triage and to pre-redact, with a person reviewing the result. See
> [Accuracy](#accuracy).

```text
$ laya-pii --file examples/email_en.txt --redact

── Passage 1/2 · SPECIAL CATEGORY DATA (address, bank account, date of birth, email or phone, health, name)
   Subject: Onboarding - new starter in the Leeds office  Hi Rachel,  Our new analyst, Tom Ashworth (date of birt…
   ? Name                      'Onboarding'                                 0.67  laya
     Name                      'Rachel'                                     0.95  rule
     Name                      'Tom Ashworth'                               0.86  laya
     Date of birth             '12/08/1994'                                 1.00  rule
     Email or phone            '07700 900314'                               1.00  rule
     Email or phone            'tom.ashworth@example.com'                   1.00  rule
     Address                   '41 Harehills Grove, Leeds LS8 4DX'          1.00  rule
     Health                    'diabetes'                                   0.75  laya
     Bank account              'GB33 BUKB 2020 1555 5555 55'                1.00  rule
   …

Redacted:
Hi [NAME],

Our new analyst, [NAME] (date of birth [DOB]), starts on Monday. His mobile is [CONTACT] and
his personal email is [CONTACT]. He lives at [ADDRESS].

[NAME] mentioned that he has type 1 [HEALTH] and may need short breaks …
```

## Requirements

- Python 3.10 or newer
- About 2 GB of disk for the models, plus PyTorch (~0.3 GB for CPU, ~3 GB for CUDA)
- Optional: an NVIDIA GPU with ~5 GB of free memory. It also runs on CPU, about 10× slower.

## Install

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
```

**With an NVIDIA GPU**, install a CUDA build of PyTorch first. Pick the command for your
system on [pytorch.org](https://pytorch.org/get-started/locally/); for CUDA 12.8 it is:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

Skip this step to run on CPU. Then install the scanner:

```bash
pip install git+https://github.com/martijnvanderkamp/laya-pii-scanner.git
```

To install a fixed release instead of the latest code, add the version tag, or install the
wheel from the [releases page](https://github.com/martijnvanderkamp/laya-pii-scanner/releases):

```bash
pip install git+https://github.com/martijnvanderkamp/laya-pii-scanner.git@v0.1.0
```

The first run downloads Laya's English and multilingual checkpoints (about 1.5 GB) from
Hugging Face into your local cache. After that, add `--offline` to run without a network.

## Use

Paste text, finish with an empty line, then paste the next piece. Ctrl+C quits.

```bash
laya-pii
```

Scan a file, and print a redacted copy:

```bash
laya-pii --file letter.txt --redact
```

Machine-readable output for other tools:

```bash
laya-pii --file letter.txt --json
```

Other options: `--device cpu` or `--device cuda`, and `--offline` once the model is cached.
`python -m laya_pii_scanner` works the same as `laya-pii`.

### From Python

```python
from laya_pii_scanner import PIIScanner, redact

scanner = PIIScanner()            # loads Laya once (5-20 s); reuse it for every text
chunks = scanner.scan(text)       # one result per passage of up to ~500 characters

for chunk in chunks:
    print(chunk.verdict)          # "clean", "personal", "special" or "review"
    for f in chunk.findings:
        if f.confirmed:
            print(f.category, f.text, f.start, f.end, round(f.confidence, 2), f.source)

print(redact(text, chunks))       # confirmed findings replaced by [NAME], [IBAN], ...
```

### Reading the output

| Verdict | Meaning |
|---|---|
| `SPECIAL CATEGORY DATA` | At least one confirmed sensitive finding: health, sexual orientation, religion, political opinion, trade union membership, ethnic origin or criminal record (GDPR articles 9 and 10) |
| `PERSONAL DATA` | Confirmed identifiers: name, email or phone, address, national ID or passport, bank account, payment card, date of birth, IP address |
| `REVIEW` | Only uncertain findings, marked `?` |
| `CLEAN` | Nothing found |

The number after each finding is its confidence. `rule` means a pattern, checksum or context
cue decided; `laya` means the model did.

## How it works

| Layer | What | By |
|---|---|---|
| 1. Fixed formats | email, phone, IBAN (mod-97), BSN (11-proef), payment card (Luhn), IP address, street address, dates | code |
| 2. Context cues | "BSN" or "passport" before a number, "born" or "geboren" before a date, "Mrs", "collega" or a sign-off before a name, "B.V." or "office" near an address | code |
| 3. Meaning | Is this capitalised word a person? Is this 9-digit number a BSN? Is this date a birth date? Does this sentence say something sensitive about one specific person? | Laya |

For special category data, keyword lists only mark *where* something might be ("diabetes",
"moskee", "arrested"). Laya then decides per sentence whether it is about one specific person
("she has diabetes") or a general statement ("diabetes affects one in ten adults"). All
thresholds live in `THRESHOLDS` at the top of
[`src/laya_pii_scanner/scanner.py`](src/laya_pii_scanner/scanner.py).

## Accuracy

Measured with `laya` 0.3.21 on an RTX 4070 Laptop GPU. The final test set was written by a
separate agent that never saw the code; the rule fixes in this release came after its first
run and moved the scores by at most 0.01.

| | Development set (90 texts) | Final test set (60 texts, independent) |
|---|---|---|
| Text contains personal data? Recall | 0.96 | **0.95** |
| Text contains personal data? Precision | 0.85 | **0.76** |
| All categories, micro F1 | 0.91 | **0.76** |
| Verdict clean / personal / special correct | 0.86 | **0.62** |

Recall per category on the final test set:

| Reliable (≥ 0.85) | Moderate | Weak (≤ 0.55) |
|---|---|---|
| payment card, bank account, IP address, email or phone, date of birth | name 0.81, religion 0.75, criminal 0.75, national ID 0.67, political opinion 0.67, address 0.62 | health 0.54, sexual orientation 0.50, ethnic origin 0.50, trade union 0.33 |

Known limitations:

- **Sensitive data phrased indirectly** is often missed: "my repeat prescription for
  levothyroxine", "off work with a slipped disc".
- **Names without a capital letter** ("hi, i'm lisa") are not detected, and capitalised
  words at the start of a sentence are sometimes taken for names ("Salary").
- **Business details** are often flagged as personal: a school's street address, a
  municipality's phone number, a jobs mailbox.
- **OCR text** with misread characters ("l4-O3-1966") is not recognised.
- Dutch and English only. Other languages are routed to Laya's multilingual checkpoint but
  were not evaluated.

Speed: about 40-80 ms for a short text and 0.3-0.8 s for an email of a few paragraphs on
the GPU above; on CPU about 0.7 s and 10 s respectively.

## Evaluate and tune

The labelled data in [`eval/data`](eval/data) is synthetic: invented people, `example.com`
addresses, documentation IP ranges and published test card numbers.

| File | Cases | Role |
|---|---|---|
| `tune.jsonl` | 40 | Written by the author; used to design and tune |
| `test1.jsonl` | 50 | Written independently; used to find weaknesses, so now part of development |
| `test2.jsonl` | 60 | Written independently; used only for the final measurement |

```bash
python eval/evaluate.py eval/data/test2.jsonl --errors                       # report + mistakes
python eval/evaluate.py eval/data/tune.jsonl eval/data/test1.jsonl --sweep  # explore thresholds
```

Each case is a JSON line: `{"id": "...", "lang": "nl", "text": "...", "expected": ["name", "health"]}`.
Add your own cases to measure the scanner on the kind of text you handle. If you change
thresholds, tune on your own development set and keep a separate set for the final number.
[`eval/experiment_special.py`](eval/experiment_special.py) reproduces the comparison behind
the per-sentence question for sensitive data.

The rule tests run without a model or GPU:

```bash
pip install -e ".[dev]"
pytest
```

## Contributing

Issues and pull requests are welcome, especially labelled examples of text the scanner gets
wrong. Please use invented data only.

## License

MIT, see [LICENSE](LICENSE). The Laya model and its weights are published by Convai
Innovations under the Apache 2.0 license and are downloaded separately from Hugging Face.
This project is not affiliated with Convai Innovations.
