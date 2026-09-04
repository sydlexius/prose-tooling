# Genericize A1: container-runtime abstraction + backend selection - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LanguageTool server runtime-agnostic (docker/podman/nerdctl) and add a `container | url` backend selection, both configured by environment variables, with no behavior change when nothing is set.

**Architecture:** `bin/prose-lint-server.sh` resolves a single `$RUNTIME` variable once (validated `PROSE_LINT_RUNTIME`, else first of docker/podman/nerdctl on `PATH`, else die) and substitutes it for every hardcoded `docker`. A new `runtime` subcommand prints the resolved binary so selection is testable with stub executables on a temp `PATH`. On the client side, `bin/prose_check.py` grows a `_backend()` reader; `ensure_server` skips `start_fn` entirely when the backend is `url`.

**Tech Stack:** Bash (shellcheck-clean, `set -euo pipefail`), Python 3 stdlib + `markdown-it-py`, pytest (subprocess-driven for the shell script).

**Spec:** `docs/superpowers/specs/2026-07-11-genericize-a1-runtime-design.md`

**Issue:** #15 (umbrella #2). #18 (local binary/JAR backend) builds on the selection layer added here and is out of scope.

## Global Constraints

- **Env vars only.** No new CLI flags on either the script or the client. Names: `PROSE_LINT_RUNTIME`, `PROSE_LINT_BACKEND` (joining the existing `PROSE_LINT_SERVER`, `PROSE_LINT_PORT`).
- **Additive and backward-compatible.** With no env set: auto-detect docker, container backend, identical behavior to today. All existing tests must keep passing unmodified.
- **Valid values.** `PROSE_LINT_RUNTIME` in `docker|podman|nerdctl`. `PROSE_LINT_BACKEND` in `container|url` (default `container`).
- **No silent failures.** An unknown value, or a runtime named but absent from `PATH`, dies loudly naming the offending value and the valid set. Never fall back to another runtime.
- **Python is stdlib-only** (plus the existing `markdown-it-py`). Bash stays `shellcheck` clean. Linux + macOS; Windows out of scope.
- **Privacy unchanged:** still a local/self-hosted server; the `url` backend points at the adopter's own server, never a public API.
- Gate for every commit: `./.venv/bin/python -m pytest`, `ruff check bin/ tests/`, `shellcheck bin/*.sh`.

---

### Task 1: Runtime abstraction in `prose-lint-server.sh`

**Files:**
- Modify: `bin/prose-lint-server.sh` (header comment lines 1-9; `command -v docker` guard line 22; `docker` call sites lines 24, 25, 33, 37, 52, 56; usage/dispatch lines 69-78)
- Test: `tests/test_runtime_selection.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `prose-lint-server.sh runtime` - prints the resolved runtime binary name (one line, e.g. `podman`) on stdout and exits 0; exits non-zero with a message on stderr when resolution fails. Task 3 documents this contract.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_selection.py`:

```python
"""Runtime resolution in prose-lint-server.sh.

The script picks one container runtime (docker/podman/nerdctl) and substitutes
it for every hardcoded `docker`. Selection is exercised through the `runtime`
subcommand against stub executables on a temp PATH, so no real container or
real runtime is needed.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "bin" / "prose-lint-server.sh"


def _stub_path(tmp_path, *names):
    """Create executable stubs named `names` in tmp_path; return it as a PATH."""
    for name in names:
        stub = tmp_path / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    return str(tmp_path)


def _run_runtime(path, **env):
    environ = dict(os.environ)
    environ["PATH"] = path + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
    environ.pop("PROSE_LINT_RUNTIME", None)
    environ.update(env)
    return subprocess.run(
        [str(SERVER), "runtime"], capture_output=True, text=True, env=environ
    )


def test_autodetects_docker_first(tmp_path):
    path = _stub_path(tmp_path, "docker", "podman", "nerdctl")
    r = _run_runtime(path)
    assert r.returncode == 0
    assert r.stdout.strip() == "docker"


def test_autodetects_podman_when_docker_absent(tmp_path):
    path = _stub_path(tmp_path, "podman", "nerdctl")
    r = _run_runtime(path)
    assert r.returncode == 0
    assert r.stdout.strip() == "podman"


def test_env_override_wins_over_autodetect(tmp_path):
    path = _stub_path(tmp_path, "docker", "nerdctl")
    r = _run_runtime(path, PROSE_LINT_RUNTIME="nerdctl")
    assert r.returncode == 0
    assert r.stdout.strip() == "nerdctl"


def test_dies_on_unknown_runtime_value(tmp_path):
    path = _stub_path(tmp_path, "docker")
    r = _run_runtime(path, PROSE_LINT_RUNTIME="rkt")
    assert r.returncode != 0
    assert "rkt" in r.stderr
    assert "docker" in r.stderr  # names the valid set


def test_dies_when_named_runtime_not_on_path(tmp_path):
    path = _stub_path(tmp_path, "docker")
    r = _run_runtime(path, PROSE_LINT_RUNTIME="podman")
    assert r.returncode != 0
    assert "podman" in r.stderr


def test_dies_when_no_runtime_available(tmp_path):
    path = _stub_path(tmp_path)  # empty dir, no stubs
    empty = dict(os.environ)
    empty["PATH"] = path
    empty.pop("PROSE_LINT_RUNTIME", None)
    r = subprocess.run(
        [str(SERVER), "runtime"], capture_output=True, text=True, env=empty
    )
    assert r.returncode != 0
    assert "PROSE_LINT_RUNTIME" in r.stderr or "url" in r.stderr
```

Note on `test_dies_when_no_runtime_available`: `PATH` holds only the empty stub dir so no real docker leaks in. The script uses `command -v` (a bash builtin) and `echo`, so it still runs with an empty PATH; `curl`/`seq`/`sleep` are not reached on the `runtime` code path.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_runtime_selection.py -v`
Expected: all six FAIL - the `runtime` subcommand does not exist, so the script hits its `*)` usage branch and dies with "usage: ..." (and `test_autodetects_podman_when_docker_absent` additionally fails at the `command -v docker` guard).

- [ ] **Step 3: Implement runtime resolution**

In `bin/prose-lint-server.sh`, replace the header comment (lines 1-9) and the `command -v docker` guard (line 22) as follows, then substitute `"${RUNTIME}"` for every `docker` call.

New header:

```bash
#!/usr/bin/env bash
# prose-lint-server.sh -- manage the local LanguageTool container.
#
# The grammar linter checks against a LOCAL LanguageTool server only; repo
# content is never sent to the public API. This starts a stock
# erikvl87/languagetool container bound to localhost with a restart policy so
# it survives reboots.
#
# The container runtime is docker, podman, or nerdctl: set PROSE_LINT_RUNTIME
# to pick one, otherwise the first found on PATH wins (in that order).
#
# Usage: prose-lint-server.sh {start|stop|status|restart|runtime}
set -euo pipefail
```

Replace line 22 (`command -v docker >/dev/null 2>&1 || die "docker (OrbStack) not found on PATH"`) with:

```bash
RUNTIMES="docker podman nerdctl"

resolve_runtime() {
	local candidate
	if [ -n "${PROSE_LINT_RUNTIME:-}" ]; then
		case " ${RUNTIMES} " in
		*" ${PROSE_LINT_RUNTIME} "*) ;;
		*) die "unknown PROSE_LINT_RUNTIME '${PROSE_LINT_RUNTIME}' (valid: ${RUNTIMES})" ;;
		esac
		command -v "${PROSE_LINT_RUNTIME}" >/dev/null 2>&1 ||
			die "PROSE_LINT_RUNTIME '${PROSE_LINT_RUNTIME}' not found on PATH"
		echo "${PROSE_LINT_RUNTIME}"
		return 0
	fi
	for candidate in ${RUNTIMES}; do
		if command -v "${candidate}" >/dev/null 2>&1; then
			echo "${candidate}"
			return 0
		fi
	done
	die "no container runtime found (${RUNTIMES}); set PROSE_LINT_RUNTIME or use PROSE_LINT_BACKEND=url"
}

RUNTIME="$(resolve_runtime)"
```

Then substitute in the rest of the file - `running`, `exists`, `start`, `stop`, and the not-ready message:

```bash
running() { [ -n "$("${RUNTIME}" ps -q -f "name=^${NAME}$")" ]; }
exists() { [ -n "$("${RUNTIME}" ps -aq -f "name=^${NAME}$")" ]; }
```

Inside `start()`: `docker start "${NAME}"` becomes `"${RUNTIME}" start "${NAME}"`; `docker run -d \` becomes `"${RUNTIME}" run -d \`; the comment `# --restart unless-stopped: OrbStack brings it back on boot.` becomes `# --restart unless-stopped: the runtime brings it back on boot.`; the final `die` becomes:

```bash
	die "server did not become ready within 30s (see: ${RUNTIME} logs ${NAME})"
```

Inside `stop()`: `docker stop "${NAME}"` becomes `"${RUNTIME}" stop "${NAME}"`.

Add the subcommand and update usage in the dispatch block:

```bash
case "${1:-}" in
start) start ;;
stop) stop ;;
restart)
	stop
	start
	;;
status) status ;;
runtime) echo "${RUNTIME}" ;;
*) die "usage: $0 {start|stop|status|restart|runtime}" ;;
esac
```

Because `die` inside `resolve_runtime` runs in a command substitution subshell, its `exit 1` ends only the subshell; `set -e` on the failing assignment `RUNTIME="$(resolve_runtime)"` then ends the script. Verify with `test_dies_on_unknown_runtime_value` that the exit code is non-zero - if it is not, make the assignment explicit: `RUNTIME="$(resolve_runtime)" || exit 1`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_runtime_selection.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the full gate**

Run: `shellcheck bin/*.sh && ruff check bin/ tests/ && ./.venv/bin/python -m pytest`
Expected: shellcheck silent, ruff "All checks passed!", pytest all green (integration tests may skip if no server is up - that is expected).

- [ ] **Step 6: Manual smoke check (only if a real runtime is installed)**

Run: `bin/prose-lint-server.sh runtime && bin/prose-lint-server.sh status`
Expected: prints the runtime name, then the existing status output. If no runtime is installed, skip this step and say so.

- [ ] **Step 7: Commit**

```bash
git add bin/prose-lint-server.sh tests/test_runtime_selection.py
git commit -m "feat(server): resolve container runtime (docker/podman/nerdctl) (#15)"
```

---

### Task 2: Backend selection in the client

**Files:**
- Modify: `bin/prose_check.py` (`ensure_server` at line 372; add `_backend()` near `DEFAULT_SERVER` line 318; the unreachable-server branch in `main` at lines 550-557)
- Test: `tests/test_autostart.py` (extend; existing three tests stay unchanged)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (the client only shells out to the server script under the `container` backend, as today).
- Produces:
  - `_backend() -> str` - returns `"container"` (default) or `"url"`, read from `os.environ` at call time; raises `ValueError` naming the bad value and the valid set on anything else.
  - `ensure_server(server, start_fn=_start_server, is_up=server_is_up, backend=None) -> bool` - `backend=None` means "read `_backend()`". Under `url` it returns `is_up(server)` without ever calling `start_fn`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_autostart.py` (keep the existing three tests and the existing import line; add `pytest` and `_backend` to the imports):

```python
import pytest

from prose_check import _backend, ensure_server


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
```

The file's top import line becomes the two lines shown above; do not leave a duplicate `from prose_check import ensure_server`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_autostart.py -v`
Expected: collection error / ImportError - `cannot import name '_backend' from 'prose_check'`.

- [ ] **Step 3: Implement `_backend()` and the `ensure_server` seam**

In `bin/prose_check.py`, after the `DEFAULT_SERVER` / `_SERVER_SCRIPT` block (around line 320), add:

```python
_BACKENDS = ("container", "url")


def _backend():
    """The selected server backend: 'container' (default) or 'url'.

    Read at call time (not import) so the environment can change under tests
    and under a hook that sets it per-repo. An unknown value is an error, not
    a silent fallback.
    """
    value = os.environ.get("PROSE_LINT_BACKEND", "container")
    if value not in _BACKENDS:
        raise ValueError(
            f"unknown PROSE_LINT_BACKEND {value!r} (valid: {', '.join(_BACKENDS)})"
        )
    return value
```

Replace `ensure_server` (lines 372-377) with:

```python
def ensure_server(server, start_fn=_start_server, is_up=server_is_up, backend=None):
    """Ensure the server is reachable, starting it once if it is not.

    Under the 'url' backend the server is preexisting/remote, so it is probed
    but never started.
    """
    if is_up(server):
        return True
    if (backend or _backend()) == "url":
        return False
    start_fn()
    return is_up(server)
```

Then make `main`'s unreachable branch (lines 550-557) say the right thing per backend:

```python
    if not args.no_autostart and args.files:
        try:
            backend = _backend()
        except ValueError as exc:
            print(f"prose-check: {exc}", file=sys.stderr)
            return 2
        if not ensure_server(args.server, backend=backend):
            print(
                f"prose-check: LanguageTool server unreachable at {args.server}.",
                file=sys.stderr,
            )
            if backend == "url":
                print(
                    "PROSE_LINT_BACKEND=url: start your own server, or unset it "
                    "to let prose-check run a container.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Start it manually with: bin/prose-lint-server.sh start",
                    file=sys.stderr,
                )
            return 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_autostart.py -v`
Expected: 9 passed (the original 3 plus the 6 new ones).

- [ ] **Step 5: Run the full gate**

Run: `ruff check bin/ tests/ && ./.venv/bin/python -m pytest && shellcheck bin/*.sh`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add bin/prose_check.py tests/test_autostart.py
git commit -m "feat(client): PROSE_LINT_BACKEND=url skips container autostart (#15)"
```

---

### Task 3: Document runtimes and backends

**Files:**
- Modify: `docs/CONFIG.md` (append a "Backends and runtimes" section)
- Modify: `README.md:12` (de-OrbStack the server one-liner)
- Modify: `CLAUDE.md` (the `bin/prose-lint-server.sh` line under "Architecture", and the "Key Rules" exit-code bullet if it names Docker)
- Modify: `bin/install.sh:79` (mention the env vars in the manual-management line)
- Test: `tests/test_install_scaffold.py` (add one assertion)

**Interfaces:**
- Consumes: the `runtime` subcommand contract from Task 1 and the `PROSE_LINT_BACKEND` values from Task 2.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_scaffold.py`:

```python
def test_prints_backend_env_guidance(tmp_path):
    repo = _git_repo(tmp_path)
    r = _run(repo)
    assert r.returncode == 0
    assert "PROSE_LINT_BACKEND" in r.stdout
    assert "PROSE_LINT_RUNTIME" in r.stdout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_install_scaffold.py::test_prints_backend_env_guidance -v`
Expected: FAIL on the first assert - the string is not in `install.sh`'s output.

- [ ] **Step 3: Update `install.sh`**

After line 79 (`echo "To manage it manually: ${TOOLING_DIR}/bin/prose-lint-server.sh start"`), add:

```bash
echo "The container runtime is auto-detected (docker, podman, nerdctl);"
echo "set PROSE_LINT_RUNTIME to pin one."
echo "Already run a shared LanguageTool server? Set PROSE_LINT_BACKEND=url and"
echo "PROSE_LINT_SERVER=<its url>; prose-check will then never start a container."
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_install_scaffold.py -v`
Expected: all passed, including the new test.

- [ ] **Step 5: Add the CONFIG.md section**

Append to `docs/CONFIG.md`:

```markdown
## Backends and runtimes

Where the LanguageTool server lives is configured entirely by environment
variables. With none set, prose-check auto-starts a local container - the
behavior it has always had.

| Variable | Values | Default | Meaning |
| --- | --- | --- | --- |
| `PROSE_LINT_SERVER` | a URL | `http://localhost:8081` | The server the client checks against. |
| `PROSE_LINT_PORT` | a port | `8081` | Host port the container binds on `127.0.0.1`. |
| `PROSE_LINT_BACKEND` | `container`, `url` | `container` | Whether prose-check may start a server. |
| `PROSE_LINT_RUNTIME` | `docker`, `podman`, `nerdctl` | auto-detect | Which container runtime the server script drives. |

**`container` (default).** When the server is unreachable, the client runs
`bin/prose-lint-server.sh start`, which starts the stock
`erikvl87/languagetool` image bound to `127.0.0.1:${PROSE_LINT_PORT}` with
`--restart unless-stopped`.

**`url`.** The server is yours and already running (a shared box, a service in
your dev compose stack, a systemd unit). The client probes
`PROSE_LINT_SERVER` and, if it is unreachable, exits 2 with guidance rather
than starting a container. Nothing is ever sent off your network - point this
at your own server, never at the public LanguageTool API.

**Runtime resolution.** The server script picks its runtime once, in this
order:

1. `PROSE_LINT_RUNTIME` if set. It must name one of `docker`, `podman`,
   `nerdctl` and be on `PATH`; otherwise the script exits non-zero. There is no
   fallback to another runtime, so a typo fails loudly instead of silently
   using the wrong thing.
2. Otherwise the first of `docker`, `podman`, `nerdctl` found on `PATH`.
3. Otherwise it exits non-zero, suggesting `PROSE_LINT_RUNTIME` or
   `PROSE_LINT_BACKEND=url`.

Check what it resolved to without starting anything:

```sh
bin/prose-lint-server.sh runtime   # prints e.g. podman
```

Rootless podman and nerdctl work: the container binds to `127.0.0.1` and the
`--restart unless-stopped` policy is supported by all three runtimes.
```

- [ ] **Step 6: Update README.md and CLAUDE.md**

In `README.md`, line 12 currently reads:

```markdown
- `bin/prose-lint-server.sh` -- start/stop/status the LanguageTool container (OrbStack).
```

Replace with:

```markdown
- `bin/prose-lint-server.sh` -- start/stop/status the LanguageTool container
  (docker, podman, or nerdctl; auto-detected). See "Backends and runtimes" in
  `docs/CONFIG.md` to pin a runtime or point at a server you already run.
```

In `CLAUDE.md`, the Architecture bullet currently reads:

```markdown
- `bin/prose-lint-server.sh` - LanguageTool container lifecycle (OrbStack/Docker).
```

Replace with:

```markdown
- `bin/prose-lint-server.sh` - LanguageTool container lifecycle. The runtime is
  resolved once (`PROSE_LINT_RUNTIME`, else first of docker/podman/nerdctl on
  PATH, else die); `prose-lint-server.sh runtime` prints the choice.
  `PROSE_LINT_BACKEND=url` makes the client use a preexisting server and never
  autostart. Documented in `docs/CONFIG.md`.
```

Grep for any other stale mentions and fix them in the same pass:

```sh
grep -rn "OrbStack" README.md CLAUDE.md docs/ bin/ examples/ || echo "none left"
```

Leave matches inside `docs/superpowers/specs/` and `docs/superpowers/plans/` alone - those are dated design records, not live docs.

- [ ] **Step 7: Prose-lint the docs you just wrote**

Run: `./.venv/bin/python bin/prose_check.py README.md CLAUDE.md docs/CONFIG.md`
Expected: exit 0 (advisory findings are fine; fix any blocking finding it reports). If the server is unreachable and no runtime is installed, note that and move on.

- [ ] **Step 8: Run the full gate**

Run: `shellcheck bin/*.sh && ruff check bin/ tests/ && ./.venv/bin/python -m pytest`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add docs/CONFIG.md README.md CLAUDE.md bin/install.sh tests/test_install_scaffold.py
git commit -m "docs: document PROSE_LINT_RUNTIME and PROSE_LINT_BACKEND (#15)"
```

---

## Done criteria

- `bin/prose-lint-server.sh` contains no bare `docker` invocation (`grep -n '\bdocker \b' bin/prose-lint-server.sh` returns only comment/doc text).
- `prose-lint-server.sh runtime` resolves per the documented order and dies loudly on a bad or absent runtime.
- `PROSE_LINT_BACKEND=url` never starts a container, and an invalid value exits 2 with a named error.
- No new CLI flags; unset environment reproduces today's behavior exactly (the three original `test_autostart.py` tests pass unmodified).
- `pytest`, `ruff check bin/ tests/`, and `shellcheck bin/*.sh` all green.
- PR closes #15. #2 stays open pending #18.
