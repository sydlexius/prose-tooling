"""Binary backend: JAR/launcher discovery, the Java probe, and stop() safety.

Everything is stub-driven -- no JVM is required. The development machine has no
Java at all (macOS ships a /usr/bin/java stub that errors), which is precisely
the condition one of these tests reproduces.
"""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "bin" / "prose-lint-server.sh"


def _bin_dir(tmp_path, *names):
    """A directory that is the ENTIRE PATH: bash, plus any named stubs.

    Host state must not leak in -- a real java or languagetool-server on the
    developer's machine would otherwise satisfy a lookup a test means to fail.
    """
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name in names:
        stub = d / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # The lifecycle paths shell out to real coreutils (unlike discovery, which
    # is bash builtins only), so symlink the ones the script actually uses.
    # Everything else is deliberately absent: a real java or languagetool-server
    # on the developer's machine must not satisfy a lookup a test means to fail.
    for tool in ("bash", "rm", "sleep", "cat", "ps", "grep", "curl", "kill"):
        real = shutil.which(tool)
        if real and not (d / tool).exists():
            (d / tool).symlink_to(real)
    assert (d / "bash").exists(), "bash is required to run the server script"
    return d


# A port nothing is listening on, so `start`'s "already running" probe cannot
# find the developer's real LanguageTool container and short-circuit. 8081 is
# the default and IS live on this machine.
TEST_PORT = "18099"


def _run(path_dir, subcmd, **env):
    environ = dict(os.environ)
    environ["PATH"] = str(path_dir)
    for var in ("PROSE_LINT_RUNTIME", "PROSE_LINT_JAR", "PROSE_LINT_START_TIMEOUT"):
        environ.pop(var, None)
    environ["PROSE_LINT_BACKEND"] = "binary"
    environ["PROSE_LINT_PORT"] = TEST_PORT
    environ.update(env)
    return subprocess.run(
        [str(SERVER), subcmd], capture_output=True, text=True, env=environ
    )


def test_explicit_jar_wins_over_launcher(tmp_path):
    d = _bin_dir(tmp_path, "java", "languagetool-server")
    jar = tmp_path / "lt.jar"
    jar.write_text("not really a jar")
    r = _run(d, "launcher", PROSE_LINT_JAR=str(jar))
    assert r.returncode == 0
    assert r.stdout.strip() == f"jar:{jar}"


def test_launcher_found_when_no_jar_is_set(tmp_path):
    d = _bin_dir(tmp_path, "java", "languagetool-server")
    r = _run(d, "launcher")
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-server"


def test_second_launcher_name_is_tried(tmp_path):
    d = _bin_dir(tmp_path, "java", "languagetool-http-server")
    r = _run(d, "launcher")
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-http-server"


def test_missing_jar_path_dies_naming_it(tmp_path):
    d = _bin_dir(tmp_path, "java")
    r = _run(d, "launcher", PROSE_LINT_JAR=str(tmp_path / "absent.jar"))
    assert r.returncode != 0
    assert "absent.jar" in r.stderr


def test_no_jar_and_no_launcher_dies_naming_both_remedies(tmp_path):
    d = _bin_dir(tmp_path, "java")
    r = _run(d, "launcher")
    assert r.returncode != 0
    assert "PROSE_LINT_JAR" in r.stderr
    assert "languagetool-server" in r.stderr


def test_broken_java_stub_is_rejected_on_the_jar_path(tmp_path):
    # Reproduces macOS: /usr/bin/java EXISTS and is executable but errors.
    # `command -v java` would pass here, which is why the script probes.
    d = _bin_dir(tmp_path)
    java = d / "java"
    java.write_text("#!/bin/sh\necho 'Unable to locate a Java Runtime.' >&2\nexit 1\n")
    java.chmod(0o755)
    jar = tmp_path / "lt.jar"
    jar.write_text("not really a jar")
    r = _run(d, "start", PROSE_LINT_JAR=str(jar))
    assert r.returncode != 0
    assert "java" in r.stderr.lower()


def test_launcher_path_does_not_probe_java(tmp_path):
    # A packaged launcher finds its own JVM (possibly bundled), so a broken or
    # absent `java` must not disqualify it.
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "launcher")
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-server"


def _reap(pid):
    """Best-effort cleanup of a detached child that may already have exited."""
    try:
        os.kill(pid, 15)
    except (ProcessLookupError, PermissionError):
        pass


def _server_stub(d, name, sleep_secs=30):
    """A stub whose process name matches the stop() safety check."""
    stub = d / name
    stub.write_text(f"#!/bin/sh\nexec sleep {sleep_secs}\n")
    stub.chmod(0o755)
    return stub


def test_stop_reports_not_running_without_a_pid_file(tmp_path):
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "stop", TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode == 0
    assert "not running" in r.stdout


def test_stop_removes_a_stale_pid_file_without_signaling(tmp_path):
    d = _bin_dir(tmp_path, "languagetool-server")
    pid_file = tmp_path / f"prose-lint-lt-{TEST_PORT}.pid"
    # PID 999999 is above the default pid_max on Linux and macOS: not alive.
    pid_file.write_text("999999\n")
    r = _run(d, "stop", TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode == 0
    assert "not running" in r.stdout
    assert not pid_file.exists()


def test_stop_refuses_to_signal_a_recycled_pid(tmp_path):
    # The dangerous case: the PID file names a LIVE process that is not ours.
    # It must survive untouched. `sleep` does not match the LanguageTool
    # command-line pattern, so it stands in for an unrelated process.
    d = _bin_dir(tmp_path)
    victim = subprocess.Popen(["sleep", "30"])
    try:
        pid_file = tmp_path / f"prose-lint-lt-{TEST_PORT}.pid"
        pid_file.write_text(f"{victim.pid}\n")
        r = _run(d, "stop", TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        assert victim.poll() is None, "stop() killed an unrelated process"
        assert "recycled" in (r.stdout + r.stderr).lower()
        assert not pid_file.exists()
    finally:
        victim.kill()
        victim.wait()


def test_start_writes_a_pid_file_and_returns_promptly(tmp_path):
    # The launcher never becomes healthy, so start times out -- but it must
    # still have written the PID file and detached, and must not hang.
    d = _bin_dir(tmp_path)
    _server_stub(d, "languagetool-server")
    r = _run(
        d, "start",
        TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
        PROSE_LINT_START_TIMEOUT="2",
    )
    assert r.returncode != 0  # never became ready
    assert "did not become ready" in r.stderr
    pid_file = tmp_path / f"prose-lint-lt-{TEST_PORT}.pid"
    assert pid_file.exists()
    # Clean up the detached child. It may already be gone (the stub`s `exec`
    # replaces the recorded wrapper), so a missing process is not a failure --
    # what is asserted above is that start RECORDED a pid, not that it lives.
    _reap(int(pid_file.read_text().strip()))


def test_start_respects_the_timeout_knob(tmp_path):
    import time

    d = _bin_dir(tmp_path)
    _server_stub(d, "languagetool-server")
    began = time.monotonic()
    r = _run(
        d, "start",
        TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
        PROSE_LINT_START_TIMEOUT="2",
    )
    elapsed = time.monotonic() - began
    assert r.returncode != 0
    assert elapsed < 20, f"ignored PROSE_LINT_START_TIMEOUT=2 (took {elapsed:.1f}s)"
    pid_file = tmp_path / f"prose-lint-lt-{TEST_PORT}.pid"
    if pid_file.exists():
        _reap(int(pid_file.read_text().strip()))


def test_status_reports_not_running_without_a_pid_file(tmp_path):
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "status", TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode != 0
    assert "not running" in r.stdout
