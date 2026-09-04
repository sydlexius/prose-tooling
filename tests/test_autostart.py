"""Tests for hook-triggered server auto-start.

If the LanguageTool container is stopped, the client starts it (via the server
script) and retries, rather than failing the commit. The network/subprocess
I/O is injected so the decision logic is testable without a container.
"""

import pytest

from prose_check import _backend, _unreachable_hint, ensure_server, main


def test_does_not_start_when_already_up():
    started = []
    up = ensure_server("url", start_fn=lambda: started.append(1), is_up=lambda u: True)
    assert up is True
    assert started == []


def test_starts_when_down_then_becomes_up():
    state = {"up": False}

    def start():
        state["up"] = True

    up = ensure_server("url", start_fn=start, is_up=lambda u: state["up"])
    assert up is True


def test_returns_false_when_start_does_not_help():
    started = []
    up = ensure_server("url", start_fn=lambda: started.append(1), is_up=lambda u: False)
    assert up is False
    assert started == [1]  # start was attempted exactly once


def test_backend_defaults_to_container(monkeypatch):
    monkeypatch.delenv("PROSE_LINT_BACKEND", raising=False)
    assert _backend() == "container"


def test_backend_reads_url(monkeypatch):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "url")
    assert _backend() == "url"


def test_backend_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "jar")
    with pytest.raises(ValueError) as excinfo:
        _backend()
    assert "jar" in str(excinfo.value)
    assert "container" in str(excinfo.value)  # names the valid set


def test_url_backend_never_starts_a_container(monkeypatch):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "url")
    started = []
    up = ensure_server("url", start_fn=lambda: started.append(1), is_up=lambda u: False)
    assert up is False
    assert started == []  # a remote server is not ours to start


def test_url_backend_returns_true_when_reachable(monkeypatch):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "url")
    up = ensure_server("url", start_fn=lambda: None, is_up=lambda u: True)
    assert up is True


def test_container_backend_still_starts(monkeypatch):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "container")
    started = []
    ensure_server("url", start_fn=lambda: started.append(1), is_up=lambda u: False)
    assert started == [1]


def test_backend_treats_empty_as_unset(monkeypatch):
    # Matches the shell side's `[ -n "${PROSE_LINT_RUNTIME:-}" ]`: a blank var
    # means "not configured", not "configured to nothing".
    monkeypatch.setenv("PROSE_LINT_BACKEND", "")
    assert _backend() == "container"
    monkeypatch.setenv("PROSE_LINT_BACKEND", "   ")
    assert _backend() == "container"


def test_unreachable_hint_is_backend_specific():
    assert "prose-lint-server.sh start" in _unreachable_hint("container")
    url_hint = _unreachable_hint("url")
    assert "prose-lint-server.sh start" not in url_hint
    assert "your own server" in url_hint


def test_invalid_backend_is_caught_even_with_no_autostart(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PROSE_LINT_BACKEND", "containr")
    doc = tmp_path / "a.md"
    doc.write_text("Hello.\n")
    assert main(["--no-autostart", str(doc)]) == 2
    assert "containr" in capsys.readouterr().err
