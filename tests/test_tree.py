"""Folder and repository scanning: which files are scanned, which text in them, and the CLI."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import FakeRouter

from laya_pii_scanner import PIIScanner, discover, scan_tree
from laya_pii_scanner.files import code_view, is_excluded, syntax_for

IBAN = "NL91ABNA0417164300"


def write(root: Path, rel: str, content) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "src/app.py", f'IBAN = "{IBAN}"  # Bel Jan op 06-12345678\nclass PIIScanner:\n    pass\n')
    write(tmp_path, "README.md", "Contact: jan.jansen@example.com\n")
    write(tmp_path, "notes/clean.txt", "Nothing personal here.\n")
    write(tmp_path, ".venv/lib/site.py", f'X = "{IBAN}"\n')
    write(tmp_path, "myenv/pyvenv.cfg", "home = /usr\n")            # a venv with a custom name
    write(tmp_path, "myenv/lib/mod.py", f'X = "{IBAN}"\n')
    write(tmp_path, "src/__pycache__/app.cpython-312.pyc", b"\x00\x01binary")
    write(tmp_path, "node_modules/pkg/index.js", f'var a = "{IBAN}";\n')
    write(tmp_path, "package-lock.json", f'{{"x": "{IBAN}"}}\n')
    write(tmp_path, "logo.png", b"\x89PNG\x00\x00")
    write(tmp_path, "data/blob.dat", b"abc\x00def")
    write(tmp_path, "big.txt", "x" * 1_100_000)
    return tmp_path


def rels(found):
    return sorted(p.relative_to(found.root).as_posix() for p in found.files)


# ---------------------------------------------------------------- discovery

def test_folder_skips_environments_caches_binaries_and_lock_files(project):
    found = discover(project)
    assert found.mode == "folder"
    assert rels(found) == ["README.md", "notes/clean.txt", "src/app.py"]
    skipped = dict(found.skipped_paths)
    assert skipped[".venv/"] == skipped["myenv/"] == skipped["node_modules/"] == skipped["src/__pycache__/"] \
        == "dependency, cache or build folder"
    assert skipped["package-lock.json"] == "lock file"
    assert skipped["logo.png"] == skipped["data/blob.dat"] == "binary or media file"
    assert skipped["big.txt"] == "larger than --max-size"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_repository_uses_tracked_files(project):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "src/app.py", "README.md", ".venv/lib/site.py"], cwd=project, check=True)
    found = discover(project)
    assert found.mode == "git"
    assert rels(found) == ["README.md", "src/app.py"]          # a tracked .venv is still skipped
    assert "notes/clean.txt" in rels(discover(project, include_untracked=True))


def test_ignore_file_and_exclude_patterns(project):
    write(project, ".laya-pii-ignore", "# synthetic fixtures\nnotes/\n")
    assert rels(discover(project, excludes=["*.md"])) == [".laya-pii-ignore", "src/app.py"]


@pytest.mark.parametrize("rel, patterns, excluded", [
    ("tests/data/x.csv", ["tests/"], True),
    ("pkg/tests/x.py", ["tests/"], True),
    ("eval/data/t.jsonl", ["eval/data/"], True),
    ("other/eval/data/t.jsonl", ["eval/data/"], False),
    ("x/y.csv", ["*.csv"], True),
    ("docs/a.md", ["docs/*.md"], True),
    ("src/app.py", ["tests/", "*.csv"], False),
])
def test_is_excluded(rel, patterns, excluded):
    assert is_excluded(rel, patterns) is excluded


# ---------------------------------------------------------------- source code

def test_code_view_keeps_strings_comments_and_numbers_only():
    src = 'class PIIScanner:\n    bsn = 123456782\n    owner = "Jan Jansen"  # ask Piet\n'
    view = code_view(src, syntax_for(Path("x.py")))
    assert len(view) == len(src) and view.count("\n") == src.count("\n")
    assert "PIIScanner" not in view and "owner" not in view
    assert '"Jan Jansen"' in view and "# ask Piet" in view and "123456782" in view


def test_code_view_handles_urls_and_block_comments():
    src = 'const url = "https://example.com/a"; // mail jan@example.com\n/* Mevr. Bakker */\n'
    view = code_view(src, syntax_for(Path("x.ts")))
    assert "https://example.com/a" in view and "jan@example.com" in view and "Mevr. Bakker" in view
    assert "const" not in view


def test_prose_files_are_scanned_whole():
    assert syntax_for(Path("README.md")) is None and syntax_for(Path("data.json")) is None
    assert syntax_for(Path("Dockerfile")) is not None


# ---------------------------------------------------------------- scanning

def test_scan_tree_reports_file_line_and_column(project):
    rep = scan_tree(PIIScanner(router=FakeRouter()), project)
    by_path = {f.path: f for f in rep.files}
    assert set(by_path) == {"README.md", "notes/clean.txt", "src/app.py"}
    app = by_path["src/app.py"]
    assert app.kind == "code" and app.verdict == "personal"
    iban = next(x for x in app.findings if x.category == "bank")
    assert (iban.line, iban.column, iban.text) == (1, 9, IBAN)
    assert any(x.category == "contact" and x.line == 1 for x in app.findings)
    assert by_path["notes/clean.txt"].verdict == "clean"
    assert rep.fails("personal") and not rep.fails("special") and not rep.fails("never")


def test_rules_only_needs_no_model(project):
    rep = scan_tree(PIIScanner(rules_only=True), project / "src" / "app.py")
    assert rep.mode == "file"
    assert {x.category for x in rep.files[0].findings} >= {"bank", "contact"}


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "laya_pii_scanner", "--fast", *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_exit_codes_and_json(project):
    found = run_cli("--json", project)
    assert found.returncode == 1, found.stderr
    data = json.loads(found.stdout)
    assert data["mode"] == "folder" and data["summary"]["personal"] == 2
    assert {f["path"] for f in data["files"]} == {"README.md", "src/app.py"}
    assert run_cli(project / "notes").returncode == 0
    assert run_cli(project, "--fail-on", "never").returncode == 0
    assert run_cli(project, "--fail-on", "special").returncode == 0


def test_html_view_keeps_visible_text_only():
    from laya_pii_scanner.files import html_view
    src = f"<p>Contact <b>Jan</b> at jan@example.com</p>\n<script>var x = '{IBAN}';</script>\n"
    view = html_view(src)
    assert len(view) == len(src)  # offsets map 1:1; line numbers come from the original text
    assert "jan@example.com" in view and "Contact" in view and IBAN not in view and "<b>" not in view


def test_html_tags_separate_words():
    from laya_pii_scanner.files import html_view
    view = html_view('<th class="c-jev">Jev</th><th class="c-laya">Laya</th>')
    assert "Jev" in view and "Laya" in view
    assert "\n" in view[view.index("Jev"):view.index("Laya")]  # a tag breaks "Jev ... Laya" apart


def test_cli_single_code_file_uses_the_code_view(project):
    out = run_cli(project / "src" / "app.py")
    assert out.returncode == 1 and "Bank account" in out.stdout and "PIIScanner" not in out.stdout
    assert run_cli(project / "logo.png").returncode == 2  # not text: a usage error, not a crash


def test_cli_threshold_override(project):
    assert run_cli(project, "--threshold", "name=0.95").returncode == 1
    bad = run_cli(project, "--threshold", "nonsense=1")
    assert bad.returncode == 2 and "--threshold" in bad.stderr
