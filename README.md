# laya-pii-scanner

[![tests](https://github.com/martijnvanderkamp/laya-pii-scanner/actions/workflows/tests.yml/badge.svg)](https://github.com/martijnvanderkamp/laya-pii-scanner/actions/workflows/tests.yml)

Find personal data in Dutch and English text, files, folders and git repositories, on your
own machine. Paste a piece of text or point it at a repository, and get a verdict per passage
or file (clean, personal data, special category data, or review), each finding with its line,
column and confidence, and optionally a redacted copy.

It combines checksummed patterns (IBAN, BSN, payment cards) and context rules with
[Laya](https://github.com/NandhaKishorM/laya), an open-weights decision model that answers
typed questions about text in milliseconds. Nothing leaves your machine except the one-time
model download.

> **A screening aid, not a guarantee.** On an independent test set it spots 95% of the texts
> that contain personal data, but it misses about half of the sensitive health mentions and
> some names. Use it to triage and to pre-redact, with a person reviewing the result. See
> [Accuracy](#accuracy).

```text
$ laya-pii examples/email_en.txt --redact

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

### Text and single files

Paste text, finish with an empty line, then paste the next piece. Ctrl+C quits.

```bash
laya-pii
```

Scan a file, and print a redacted copy:

```bash
laya-pii letter.txt --redact
```

Machine-readable output for other tools:

```bash
laya-pii letter.txt --json
```

Other options: `--device cpu` or `--device cuda`, and `--offline` once the model is cached.
`python -m laya_pii_scanner` works the same as `laya-pii`.

### Folders and git repositories

```bash
laya-pii path/to/repo
```

```text
$ laya-pii ~/src/acme-crm

/home/you/src/acme-crm · git repository, tracked files · 5 files scanned · skipped 2: 1 binary or media file, 1 lock file

docs/intake-notes.md  PERSONAL DATA
     1:24     Name                      'Sandra Mulder'                              0.95  rule

src/seed.py  PERSONAL DATA
     2:20     Bank account              'NL91ABNA0417164300'                         1.00  rule
     6:27     Name                      'Pieter de Wit'                              0.95  laya
     6:50     Email or phone            'pieter.dewit@example.com'                   1.00  rule
     6:84     Email or phone            '06-12345678'                                1.00  rule
     7:27     Name                      'Test User'                                  0.92  laya
     7:46     Email or phone            'test@example.com'                           1.00  rule
   + 1 below the threshold (--show-review lists them)

5 files in 0.5 s: 0 special category data · 2 personal data · 0 review · 3 clean
```

In this example, `.venv/` and `node_modules/` are in `.gitignore`, so they are never read.
The tracked logo and lock file are skipped. In `src/seed.py` the IBAN inside a string is
found, but `class CustomerService` in `src/app.ts` is not taken for a name.

The output also shows two known weaknesses. "Test User" is a placeholder that is flagged as
a name. The note "Ze zit sinds maandag thuis met een burn-out" is missed as health data; see
[Accuracy](#accuracy).

**What is read.** In a git repository, only the files git tracks. Everything in `.gitignore`
is left alone, such as virtual environments, build output and local data.
`--include-untracked` also reads new files that are not ignored. In any other folder, all
files are read, recursively.

**What is always skipped**, even when tracked:

- virtual environments: `.venv`, `venv`, and any folder with a `pyvenv.cfg`
- dependency, cache and build folders: `node_modules`, `vendor`, `site-packages`,
  `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `dist`, `build`,
  `target`, `*.egg-info`, `.idea`, `.vscode`
- binary and media files, office documents and PDFs
- lock files, minified bundles and source maps
- files over 1 MB (change the limit with `--max-size`)

**How each file is read.**

- **Source code** (Python, JavaScript and TypeScript, Java, C#, Go, Rust, C and C++, PHP,
  Ruby, shell, SQL and more): only string literals, comments and numbers of six or more
  digits. Identifiers such as `CustomerService` are never read as names, and line and column
  numbers still point at the original file.
- **HTML**: the visible text only.
- **Everything else** (Markdown, text, CSV, JSON, YAML, notebooks, e-mails, config files):
  the whole file.

**Skip more** with `--exclude` (repeatable):

```bash
laya-pii . --exclude "tests/fixtures/" --exclude "*.csv"
```

Or commit a `.laya-pii-ignore` file to the scanned folder, with one pattern per line:

```text
# synthetic test data
tests/fixtures/
*.sample.json
docs/examples/customers.md
```

The patterns match like this:

- A pattern ending in `/` matches a folder anywhere, or a path from the scanned folder when it
  contains another `/`.
- `*.csv` matches file names.
- Any other pattern matches a path from the scanned folder.

`--show-skipped` lists every skipped path with the reason.

| Option | Effect |
|---|---|
| `--fail-on special\|personal\|review\|never` | The lowest verdict that makes the exit code 1. Default `personal`. |
| `--fast` | Rules only, no model: formats, checksums and cued names. Much faster and needs no GPU or PyTorch, but misses other names and all special category data. |
| `--json` | A summary, and for every file with findings each finding's category, text, line, column, confidence and source. |
| `--threshold NAME=VALUE` | Override a threshold, for example `name=0.9` to report fewer uncertain names. Repeatable. |
| `--show-review` | Also list findings below the threshold. They are always counted per file, and always included in `--json`. |
| `--include-untracked` | Git: also scan new files that `.gitignore` allows. |
| `--exclude PATTERN` | Skip matching paths. Repeatable. |
| `--max-size KB` | Skip larger files. Default 1000. |
| `--show-skipped` | List every skipped path and why. |

Exit codes: 0 when nothing reaches the `--fail-on` level, 1 when something does, and 2 for
a usage error.

**In CI.** The `--fast` mode needs no model, so a pipeline can install the scanner without
its dependencies and fail the build on personal data:

```yaml
- run: pip install --no-deps git+https://github.com/martijnvanderkamp/laya-pii-scanner.git
- run: laya-pii . --fast
```

Add `@<tag>` to the URL to pin a release.

For the full model in CI, install normally and cache `~/.cache/huggingface`. Without a GPU,
expect minutes rather than seconds.

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

For a folder or repository:

```python
from pathlib import Path
from laya_pii_scanner import PIIScanner, scan_tree

report = scan_tree(PIIScanner(), Path("path/to/repo"), excludes=["tests/fixtures/"])
for file in report.files:
    for f in file.findings:
        if f.confirmed:
            print(file.path, f.line, f.column, f.category, f.text)
print(report.counts(), report.skipped)
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
("she has diabetes") or a general statement ("diabetes affects one in ten adults").

Three rules keep technical text quiet:

- **A person must be in the passage.** Sensitive data only counts in a passage that refers
  to a person: a pronoun, a role such as "patiënt" or "collega", or a confirmed name.
- **Word lists are not statements.** Five or more keywords of one category in a sentence
  form a word list.
- **Ordinary words are not names.** A capitalised word that also appears in lower case in
  the same file ("Install", "Email") is not a name candidate.

All thresholds live in `THRESHOLDS` at the top of
[`src/laya_pii_scanner/scanner.py`](src/laya_pii_scanner/scanner.py).

## Accuracy

Measured with `laya` 0.3.21 on an RTX 4070 Laptop GPU. The final test set was written by a
separate agent that never saw the code. It was first used for v0.1.0; the v0.2.0 changes
target noise in repositories and were checked against it afterwards. Recall stayed the same
and precision went up.

| | Development set (90 texts) | Final test set (60 texts, independent) |
|---|---|---|
| Text contains personal data? Recall | 0.96 | **0.95** |
| Text contains personal data? Precision | 0.93 | **0.82** |
| All categories, micro F1 | 0.93 | **0.78** |
| Verdict clean / personal / special correct | 0.91 | **0.68** |

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

Limits when scanning repositories:

- **Only the current files are read, not the git history.** Data that was committed and
  later deleted is still in the history. To scan an older state, check it out, for example
  with `git worktree add`.
- **PDFs, office documents and images are skipped**, not read.
- **Code is split into strings and comments with patterns, not a parser.** Unusual string
  syntax, such as heredocs or raw strings with custom delimiters, can be missed.
- **Intentional names are reported too.** Author names in `LICENSE`, package metadata or
  `CODEOWNERS` are personal data as well. Exclude those files when the names are there on
  purpose.
- **Technical documentation produces the most false names.** Product, tool and role names
  such as "Claude", "Admin" or "Echidna" are sometimes taken for people. `--threshold
  name=0.9` trades some recall for far fewer of these.

Speed on the GPU above:

- about 40-80 ms for a short text, and 0.3-0.8 s for an email of a few paragraphs; on CPU
  about 0.7 s and 10 s
- a 365-file repository of mostly Markdown, JSON and Python: about 8 minutes with the model,
  3.4 s with `--fast`

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
