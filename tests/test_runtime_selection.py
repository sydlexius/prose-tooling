"""Runtime resolution in prose-lint-server.sh.

The script picks one container runtime (docker/podman/nerdctl) and substitutes
it for every hardcoded `docker`. Selection is exercised through the `runtime`
subcommand against stub executables on a temp PATH, so no real container or
real runtime is needed.
"""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "bin" / "prose-lint-server.sh"


def _stub_path(tmp_path, *names):
    """Create executable stubs named `names` in tmp_path; return it as a PATH.

    The directory is the ENTIRE PATH under test, so a runtime the developer (or
    the CI runner) happens to have installed cannot leak in and satisfy a
    lookup the test means to fail. ubuntu-latest ships both docker and podman
    in /usr/bin, so appending the system directories would make
    "podman when docker is absent" and "named runtime not on PATH" pass here
    and fail there. Only bash is added, for the script's shebang.
    """
    for name in names:
        stub = tmp_path / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    bash = shutil.which("bash")
    assert bash, "bash is required to run the server script"
    if not (tmp_path / "bash").exists():
        (tmp_path / "bash").symlink_to(bash)
    return str(tmp_path)


def _run_runtime(path, **env):
    environ = dict(os.environ)
    environ["PATH"] = path
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
    # PATH holds only bash (for the shebang) -- no runtime is reachable.
    r = _run_runtime(_stub_path(tmp_path))
    assert r.returncode != 0
    assert "PROSE_LINT_RUNTIME" in r.stderr or "url" in r.stderr


def _stub_runtime(tmp_path, ps_output, stop_rc):
    """A stub docker whose `ps` prints `ps_output` and whose `stop` exits `stop_rc`."""
    path = _stub_path(tmp_path)
    stub = tmp_path / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  ps) printf '%s' '{ps_output}' ;;\n"
        f"  stop) exit {stop_rc} ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return path


def _run_stop(path):
    environ = dict(os.environ)
    environ["PATH"] = path
    environ.pop("PROSE_LINT_RUNTIME", None)
    return subprocess.run(
        [str(SERVER), "stop"], capture_output=True, text=True, env=environ
    )


def test_stop_reports_not_running_when_absent(tmp_path):
    r = _run_stop(_stub_runtime(tmp_path, "", 0))
    assert r.returncode == 0
    assert "not running" in r.stdout


def test_stop_reports_stopped_on_success(tmp_path):
    r = _run_stop(_stub_runtime(tmp_path, "abc123", 0))
    assert r.returncode == 0
    assert "stopped" in r.stdout


def test_stop_fails_loudly_when_the_stop_fails(tmp_path):
    # The old `a && b || c` chain printed "not running" and exited 0 here,
    # telling the user the container was down while it was still up.
    r = _run_stop(_stub_runtime(tmp_path, "abc123", 125))
    assert r.returncode != 0
    assert "not running" not in r.stdout
    assert "failed to stop" in r.stderr


def _run_subcmd(path, subcmd, **env):
    environ = dict(os.environ)
    environ["PATH"] = path
    environ.pop("PROSE_LINT_RUNTIME", None)
    environ.update(env)
    return subprocess.run(
        [str(SERVER), subcmd], capture_output=True, text=True, env=environ
    )


def test_binary_backend_does_not_require_a_container_runtime(tmp_path):
    # The regression test for load-time resolution: with NO container runtime
    # on PATH at all, selecting a non-container backend must not die at load.
    r = _run_subcmd(_stub_path(tmp_path), "status", PROSE_LINT_BACKEND="binary")
    assert "no container runtime found" not in r.stderr


def test_container_backend_still_requires_a_runtime(tmp_path):
    r = _run_subcmd(_stub_path(tmp_path), "status", PROSE_LINT_BACKEND="container")
    assert r.returncode != 0
    assert "no container runtime found" in r.stderr


def test_unknown_backend_dies(tmp_path):
    r = _run_subcmd(
        _stub_path(tmp_path, "docker"), "status", PROSE_LINT_BACKEND="jar"
    )
    assert r.returncode != 0
    assert "jar" in r.stderr
    assert "container" in r.stderr  # names the valid set


def test_url_backend_refuses_lifecycle_commands(tmp_path):
    # A server we did not start is not ours to manage.
    r = _run_subcmd(
        _stub_path(tmp_path, "docker"), "start", PROSE_LINT_BACKEND="url"
    )
    assert r.returncode != 0
    # Assert the REFUSAL, not merely a non-zero exit mentioning "url": with the
    # refusal deleted, dispatch still errors with "backend 'url' does not
    # implement 'start'", which satisfied both of those and left the test inert.
    assert "not ours to manage" in r.stderr


def test_multi_word_backend_value_is_rejected(tmp_path):
    # " container url binary " contains "url binary" as a substring, so a
    # substring allowlist would validate it and then skip the url refusal.
    r = _run_subcmd(
        _stub_path(tmp_path, "docker"), "runtime", PROSE_LINT_BACKEND="url binary"
    )
    assert r.returncode != 0
    assert "url binary" in r.stderr


def test_dispatch_rejects_a_missing_handler(tmp_path):
    # An indirect dispatch to a missing function must resolve to an actionable
    # message, not a raw "command not found" naming an internal symbol.
    #
    # Driven by neutering one handler rather than by naming an unfinished
    # backend: the original form used `binary status` while binary_* did not
    # exist, and silently lost its teeth the moment that backend was
    # implemented. A guard's test should not depend on what happens to be
    # unfinished elsewhere.
    script = SERVER.read_text().replace("container_status() {", "_unused_status() {", 1)
    patched = tmp_path / "patched-server.sh"
    patched.write_text(script)
    patched.chmod(0o755)
    environ = dict(os.environ)
    environ["PATH"] = _stub_path(tmp_path, "docker")
    environ.pop("PROSE_LINT_RUNTIME", None)
    environ["PROSE_LINT_BACKEND"] = "container"
    r = subprocess.run(
        [str(patched), "status"], capture_output=True, text=True, env=environ
    )
    assert r.returncode != 0
    assert "does not implement" in r.stderr


def test_blank_backend_matches_the_client_contract(tmp_path):
    # The client's _backend() strips before testing, so "   " is `container`
    # there. If the script disagreed, the client would accept the value, shell
    # out to autostart, and the script would die -- the failure surfacing at
    # the wrong layer, contradicting the half that just validated it.
    from prose_check import _backend

    for blank in ("", "   ", "\t"):
        os.environ["PROSE_LINT_BACKEND"] = blank
        try:
            assert _backend() == "container", f"client rejected {blank!r}"
        finally:
            os.environ.pop("PROSE_LINT_BACKEND", None)
        r = _run_subcmd(_stub_path(tmp_path, "docker"), "runtime",
                        PROSE_LINT_BACKEND=blank)
        assert r.returncode == 0, f"script rejected {blank!r}: {r.stderr}"
        assert r.stdout.strip() == "docker"
