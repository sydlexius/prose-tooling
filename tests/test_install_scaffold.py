"""Behavioral tests for the install.sh scaffold (writes config, never clobbers)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "bin" / "install.sh"


def _run(target, *args):
    return subprocess.run(
        [str(INSTALL), str(target), *args],
        capture_output=True, text=True,
    )


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_writes_prose_lint_toml_when_absent(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo)
    assert r.returncode == 0
    written = (repo / ".prose-lint.toml").read_text()
    assert "[i18n]" in written


def test_backs_up_existing_prose_lint_toml(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / ".prose-lint.toml").write_text("# my custom config\n")
    r = _run(repo)
    assert r.returncode == 0
    assert (repo / ".prose-lint.toml.bak").read_text() == "# my custom config\n"


def test_with_config_copies_config_dir(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo, "--with-config")
    assert r.returncode == 0
    assert (repo / ".prose-lint-config" / "en-US" / "severity.toml").exists()


def test_prints_backend_env_guidance(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo)
    assert r.returncode == 0
    assert "PROSE_LINT_BACKEND" in r.stdout
    assert "PROSE_LINT_RUNTIME" in r.stdout


def test_prints_hook_snippet(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo)
    assert "prose-lint" in r.stdout
    assert "prose_check.py" in r.stdout


def test_hook_placeholder_is_substituted(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo)
    assert "__CLIENT__" not in r.stdout  # sed substitution fired


def test_with_config_hook_has_config_dir(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo, "--with-config")
    assert "--config-dir" in r.stdout


def test_rerun_preserves_original_custom_config(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / ".prose-lint.toml").write_text("# my custom config\n")
    _run(repo)          # run 1: backs up custom -> .bak, writes starter
    _run(repo)          # run 2: starter already present, must not touch .bak
    assert (repo / ".prose-lint.toml.bak").read_text() == "# my custom config\n"


def test_rerun_with_config_does_not_abort(tmp_path):
    # cp -Rn skips pre-existing files on the 2nd run; on GNU coreutils >=9.2 the
    # skip exits non-zero, which must not abort the run (hook must still print).
    repo = _git_repo(tmp_path)
    _run(repo, "--with-config")
    r = _run(repo, "--with-config")
    assert r.returncode == 0
    assert "--config-dir" in r.stdout  # reached the hook-printing step


def test_git_file_pointer_accepted(tmp_path):
    # A worktree/submodule checkout has .git as a FILE, not a directory.
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n")
    r = _run(tmp_path)
    assert r.returncode == 0
    assert (tmp_path / ".prose-lint.toml").exists()
