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
