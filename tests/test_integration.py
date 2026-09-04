"""End-to-end integration against a live LanguageTool server.

Skipped automatically when no server is reachable (so unit runs stay hermetic).
Start one with: bin/prose-lint-server.sh start
"""

import urllib.error
import urllib.request
from pathlib import Path

import pytest

import prose_check

# The same fixed default the conftest fixture pins prose_check.DEFAULT_SERVER
# to. Reading prose_check.DEFAULT_SERVER here would capture any ambient
# PROSE_LINT_SERVER at COLLECTION time, before the fixture runs, so the skip
# gate could probe one server while the test body checks against another.
SERVER = "http://localhost:8081"
FIXTURES = Path(__file__).parent / "fixtures"


def _server_up():
    try:
        urllib.request.urlopen(SERVER.rstrip("/") + "/v2/languages", timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason="LanguageTool server not running")


def test_blocking_file_exits_1_with_errors(capsys):
    code = prose_check.main([str(FIXTURES / "blocking.md")])
    out = capsys.readouterr().out
    assert code == 1
    # Grammar rules block; spelling is advisory (typos owns blocking spelling).
    assert "[ERROR] LOCAL_EM_DASH" in out
    assert "[ERROR] SERIAL_COMMA_ON" in out
    assert "[warn] MORFOLOGIK_RULE_EN_US" in out


def test_advisory_only_file_exits_0(capsys):
    code = prose_check.main([str(FIXTURES / "advisory.md")])
    out = capsys.readouterr().out
    assert code == 0
    assert "[warn] PASSIVE_VOICE_SIMPLE" in out
    assert "[ERROR]" not in out


def test_code_fence_prose_is_not_flagged(capsys):
    # Misspellings and bad grammar inside a fenced code block must not surface.
    code = prose_check.main([str(FIXTURES / "codefence.md")])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == ""


def test_server_down_fails_loud(capsys):
    code = prose_check.main(
        ["--no-autostart", "--server", "http://localhost:9", str(FIXTURES / "advisory.md")]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "unreachable" in err


def test_i18n_file_checked_with_microcopy_profile(capsys):
    code = prose_check.main(
        ["--format", "i18n", "--profile", "microcopy", str(FIXTURES / "sample.en.json")]
    )
    out = capsys.readouterr().out
    assert code == 0  # microcopy has no blocking findings here
    assert "MORFOLOGIK_RULE_EN_US" in out  # the "youre" typo was checked (advisory)
    assert "[ERROR]" not in out  # fragments/labels do not block
    # A fragment button label must not trip a sentence-fragment error.
    assert "ui.button_merge" not in out
