# Changelog

## 0.2.0

### Added

- **Scan folders and git repositories.** Run `laya-pii path/to/repo`.
  - In a git repository only tracked files are read, so `.gitignore` is respected. `--include-untracked` adds new files that are not ignored.
  - Always skipped, even when tracked:
    - virtual environments (`.venv`, `venv`, any folder with `pyvenv.cfg`)
    - dependency, cache and build folders (`node_modules`, `__pycache__`, `.pytest_cache`, `dist`, `build`, `*.egg-info`, ...)
    - binary and media files, office documents and PDFs
    - lock files, and minified bundles and source maps
    - files over 1 MB
- **Source code is scanned smartly.** Only strings, comments and long numbers are read, so identifiers are not taken for names. Line and column numbers still point at the original file.
- **HTML is scanned by its visible text only.**
- **More ways to skip paths:** `--exclude PATTERN` (repeatable), a `.laya-pii-ignore` file, `--max-size`, and `--show-skipped` to list what was skipped.
- **Exit codes for CI.**
  - `--fail-on special|personal|review|never` sets what counts as a failure; the default is `personal`.
  - Exit code 1 means something was found, 2 a usage error.
- **`--threshold NAME=VALUE`** to override any threshold from the command line, for example `name=0.9`.
- **`--fast` rules-only mode.** It needs no model, GPU or PyTorch, but finds only formats, checksums and cued names.
- **`--json` for folders**, with the file, line and column of every finding.
- **Progress while scanning a folder.** Results for each file appear as soon as it is scanned.
  - A progress line on stderr shows files, percentage (by bytes), elapsed time, estimated time left and the number of files with findings. It updates in place in a terminal, and is written as a plain line at every 10% in CI logs.
  - Loading the model and listing the files are announced.
  - `--quiet` turns all of this off.
- **Folder reports show confirmed findings by default**, with a count of review items per file. `--show-review` lists the review items too; `--json` always includes them.
- **Python API:** `scan_tree`, `scan_file` and `discover`.

### Changed

- **Fewer false alarms in technical text:**
  - Special category data now counts only in a passage that refers to a person.
  - Five or more keywords of one category in a sentence are treated as a word list, not a statement.
  - A capitalised word that also appears in lower case in the same text ("Email", "Install") is no longer a name candidate.
  - Repeated names are judged once per text.
  - In source code, the safety net for sensitive data without a keyword is off.
- **Technical IP addresses are not reported:** `0.0.0.0`, `127.x` and `255.x`.
- **Quoted names are cleaned up:** `'Rachel'` is reported as `Rachel`.
- **Email addresses must end in a real top-level domain**, so `repo.git@v0.1.0` is no longer one.
- **One text file given as a path** gets the detailed passage report, as `--file` did, and now also sets the exit code. A single source-code or HTML file goes through the file scan, which reads only its strings and comments, or its visible text.
- **Code references are not email addresses** (`method@file.rb`), and organisation mailboxes such as `security@` and `opensource@` are not personal.
- **ALL-CAPS words of five or more letters are not keywords** (`PANIC`), while acronyms such as HIV and ADHD still count.
- **Accuracy on the independent test set:** precision for "contains personal data" rose from 0.76 to 0.82 and micro F1 from 0.76 to 0.78. Recall is unchanged at 0.95.

## 0.1.0

First public release: a scanner for pasted text and single files, the `laya-pii` CLI, a Python API, rule tests and three synthetic evaluation sets.
