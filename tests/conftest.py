import sys
from pathlib import Path

import pytest

# Make the client module (bin/prose_check.py) importable in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

_PROSE_LINT_ENV = (
    "PROSE_LINT_BACKEND",
    "PROSE_LINT_RUNTIME",
    "PROSE_LINT_SERVER",
    "PROSE_LINT_PORT",
)


@pytest.fixture(autouse=True)
def _scrub_prose_lint_env(monkeypatch):
    """Run every test against an unconfigured environment.

    The client reads PROSE_LINT_BACKEND at call time, so a developer who has
    adopted the url backend in their shell profile -- exactly this feature's
    target user -- would otherwise see unrelated tests fail under `make check`.
    Tests that need a value set one explicitly with monkeypatch.setenv.
    """
    for var in _PROSE_LINT_ENV:
        monkeypatch.delenv(var, raising=False)
