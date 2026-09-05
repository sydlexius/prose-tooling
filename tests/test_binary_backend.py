"""Binary backend: JAR/launcher discovery, the Java probe, and stop() safety.

Everything is stub-driven -- no JVM is required. The development machine has no
Java at all (macOS ships a /usr/bin/java stub that errors), which is precisely
the condition one of these tests reproduces.
"""
import itertools
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "bin" / "prose-lint-server.sh"


def _state_dir(tmp_path):
    """The private per-user dir the script keeps pid/log files in.

    `state_dir()` creates it on start; tests that pre-place a pid file without
    starting must create it themselves.
    """
    d = tmp_path / f"prose-lint-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


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
    # `setsid` is absent on macOS by design -- the script falls back to `set -m`
    # there, and omitting it here keeps that fallback on the tested path.
    # `nohup` is the one whose absence made the launch path silently untested:
    # every start failed with "nohup: command not found", no child was ever
    # spawned, and the assertions (a pid file exists, it returned fast) all
    # still held. `setsid` is absent on macOS by design -- the script falls
    # back to `set -m` there, keeping that fallback on the tested path.
    for tool in ("bash", "rm", "sleep", "cat", "ps", "grep", "curl", "kill",
                 "nohup", "setsid", "env", "date", "id", "mkdir"):
        real = shutil.which(tool)
        # `not (d / tool).exists()` is False for a DANGLING symlink, so guard on
        # the link itself; and skip tools the host lacks (macOS has no setsid)
        # rather than creating a broken link that `command -v` would accept.
        if real and not (d / tool).is_symlink():
            (d / tool).symlink_to(real)
    assert (d / "bash").exists(), "bash is required to run the server script"
    return d


# Every test gets its OWN port. A single shared port made the suite
# order-dependent: `binary_start` health-probes the port first, so one test's
# still-dying server made the next test's start short-circuit with "already
# running" -- which silently skipped the launch it meant to exercise, and made
# mutation results differ between a filtered run and the full suite.
#
# Well away from 8081, which is the default and holds the developer's real
# container.
# Seeded per xdist worker: a module-level counter restarts at the same value in
# every worker process, so parallel runs would hand two tests the same port --
# the exact shared-state problem the fixture exists to remove.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
_WORKER_OFFSET = int("".join(c for c in _WORKER if c.isdigit()) or 0) * 200
_PORT_COUNTER = itertools.count(18100 + _WORKER_OFFSET)


@pytest.fixture
def port():
    return str(next(_PORT_COUNTER))


@pytest.fixture(autouse=True, scope="session")
def _no_stub_servers_survive_the_session():
    """Final backstop: never leave stub servers running after the suite.

    The per-test reaper covers normal paths, but a test that fails BEFORE
    recording its pgid (or a deliberately broken build during mutation
    testing) can still leak. A leaked stub holds a port and poisons later
    runs, which is exactly the shared-state problem the per-test port fixes.
    """
    yield
    out = subprocess.run(
        ["ps", "-eo", "pgid=,command="], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        # `fakejava` ONLY: it is a stub-only token, so this cannot match a
        # developer's real LanguageTool server or an editor whose argv happens
        # to contain the class name. Our stubs always run via fakejava.
        if "fakejava" not in line:
            continue
        head = line.split(None, 1)[0]
        if not head.isdigit():
            continue
        try:
            os.killpg(int(head), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


@pytest.fixture
def reaper():
    """Kill every process group a test started, however the test ends.

    Scoped to groups the test itself recorded -- never `pkill` by name, which
    matches bystanders (it matched a reviewer's own command line once).
    """
    groups = []
    yield groups.append
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _run(path_dir, subcmd, port="18099", **env):
    environ = dict(os.environ)
    environ["PATH"] = str(path_dir)
    for var in ("PROSE_LINT_RUNTIME", "PROSE_LINT_JAR", "PROSE_LINT_START_TIMEOUT"):
        environ.pop(var, None)
    environ["PROSE_LINT_BACKEND"] = "binary"
    environ["PROSE_LINT_PORT"] = port
    environ.update(env)
    return subprocess.run(
        [str(SERVER), subcmd], capture_output=True, text=True, env=environ
    )


def test_explicit_jar_wins_over_launcher(tmp_path, port):
    d = _bin_dir(tmp_path, "java", "languagetool-server")
    jar = tmp_path / "lt.jar"
    jar.write_text("not really a jar")
    r = _run(d, "launcher", port, PROSE_LINT_JAR=str(jar))
    assert r.returncode == 0
    assert r.stdout.strip() == f"jar:{jar}"


def test_launcher_found_when_no_jar_is_set(tmp_path, port):
    d = _bin_dir(tmp_path, "java", "languagetool-server")
    r = _run(d, "launcher", port)
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-server"


def test_second_launcher_name_is_tried(tmp_path, port):
    d = _bin_dir(tmp_path, "java", "languagetool-http-server")
    r = _run(d, "launcher", port)
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-http-server"


def test_missing_jar_path_dies_naming_it(tmp_path, port):
    d = _bin_dir(tmp_path, "java")
    r = _run(d, "launcher", port, PROSE_LINT_JAR=str(tmp_path / "absent.jar"))
    assert r.returncode != 0
    assert "absent.jar" in r.stderr


def test_no_jar_and_no_launcher_dies_naming_both_remedies(tmp_path, port):
    d = _bin_dir(tmp_path, "java")
    r = _run(d, "launcher", port)
    assert r.returncode != 0
    assert "PROSE_LINT_JAR" in r.stderr
    assert "languagetool-server" in r.stderr


def test_broken_java_stub_is_rejected_on_the_jar_path(tmp_path, port):
    # Reproduces macOS: /usr/bin/java EXISTS and is executable but errors.
    # `command -v java` would pass here, which is why the script probes.
    d = _bin_dir(tmp_path)
    java = d / "java"
    java.write_text("#!/bin/sh\necho 'Unable to locate a Java Runtime.' >&2\nexit 1\n")
    java.chmod(0o755)
    jar = tmp_path / "lt.jar"
    jar.write_text("not really a jar")
    r = _run(d, "start", port, PROSE_LINT_JAR=str(jar))
    assert r.returncode != 0
    assert "java" in r.stderr.lower()


def test_launcher_path_does_not_probe_java(tmp_path, port):
    # A packaged launcher finds its own JVM (possibly bundled), so a broken or
    # absent `java` must not disqualify it.
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "launcher", port)
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


def test_stop_reports_not_running_without_a_pid_file(tmp_path, port, reaper):
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode == 0
    assert "not running" in r.stdout


def test_stop_removes_a_stale_pid_file_without_signaling(tmp_path, port, reaper):
    d = _bin_dir(tmp_path, "languagetool-server")
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    # PID 999999 is above the default pid_max on Linux and macOS: not alive.
    pid_file.write_text("999999\n")
    r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode == 0
    assert "not running" in r.stdout
    assert not pid_file.exists()


def test_stop_refuses_to_signal_a_recycled_pid(tmp_path, port, reaper):
    # The dangerous case: the PID file names a LIVE process that is not ours.
    # It must survive untouched. `sleep` does not match the LanguageTool
    # command-line pattern, so it stands in for an unrelated process.
    d = _bin_dir(tmp_path)
    victim = subprocess.Popen(["sleep", "30"])
    try:
        pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
        pid_file.write_text(f"{victim.pid}\n")
        r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        assert victim.poll() is None, "stop() killed an unrelated process"
        assert "recycled" in (r.stdout + r.stderr).lower()
        assert not pid_file.exists()
    finally:
        victim.kill()
        victim.wait()


def test_start_writes_a_pid_file_and_returns_promptly(tmp_path, port, reaper):
    # The launcher never becomes healthy, so start times out -- but it must
    # still have written the PID file and detached, and must not hang.
    d = _bin_dir(tmp_path)
    _server_stub(d, "languagetool-server")
    r = _run(
        d, "start", port,
        TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
        PROSE_LINT_START_TIMEOUT="2",
    )
    assert r.returncode != 0  # never became ready
    assert "did not become ready" in r.stderr
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    assert pid_file.exists()
    reaper(int(pid_file.read_text().strip()))


def test_start_respects_the_timeout_knob(tmp_path, port, reaper):
    import time

    d = _bin_dir(tmp_path)
    _server_stub(d, "languagetool-server")
    began = time.monotonic()
    r = _run(
        d, "start", port,
        TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
        PROSE_LINT_START_TIMEOUT="2",
    )
    elapsed = time.monotonic() - began
    assert r.returncode != 0
    assert elapsed < 20, f"ignored PROSE_LINT_START_TIMEOUT=2 (took {elapsed:.1f}s)"
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    if pid_file.exists():
        reaper(int(pid_file.read_text().strip()))


def test_status_reports_not_running_without_a_pid_file(tmp_path, port, reaper):
    d = _bin_dir(tmp_path, "languagetool-server")
    r = _run(d, "status", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode != 0
    assert "not running" in r.stdout


def _wrapper_launcher(d, name="languagetool-server"):
    """A launcher shaped like LanguageTool's own: runs java WITHOUT exec.

    This is the shape that broke stop(): the recorded pid is the WRAPPER, and
    signaling it alone orphans the JVM, which keeps holding the port.
    """
    # `exec -a` keeps the LanguageTool argv visible in `ps` after the exec.
    # A plain `exec sleep 300` REPLACES the process image and erases the
    # marker, so the stub would look nothing like a real JVM to `ps` -- which
    # is both what pid_is_ours matches on and what these tests assert.
    (d / "fakejava").write_text(
        "#!/bin/bash\n"
        "exec -a \"java $* \" /bin/sleep 300\n"
    )
    (d / "fakejava").chmod(0o755)
    stub = d / name
    stub.write_text(
        "#!/bin/bash\n"
        "# Shaped like LanguageTool's own launcher: runs java WITHOUT exec, so\n"
        "# the recorded pid is this WRAPPER and the JVM is a child.\n"
        "fakejava -cp /opt/lt/languagetool-server.jar "
        "org.languagetool.server.HTTPServer \"$@\" &\n"
        "wait\n"
    )
    stub.chmod(0o755)
    return stub


def _descendants_alive(marker, pgid=None):
    """Processes matching `marker`, scoped to a process group when given.

    Scoping matters: an unscoped grep over every process matches BYSTANDERS --
    a CI job whose command line happens to contain the marker fails spuriously,
    and a real orphan can be masked by an unrelated match. It also matched the
    reviewer's own `pkill` command line during review.
    """
    fmt = "pid=,pgid=,command="
    out = subprocess.run(
        ["ps", "-ww", "-eo", fmt], capture_output=True, text=True
    ).stdout
    hits = []
    for ln in out.splitlines():
        if marker not in ln or "grep" in ln:
            continue
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        if pgid is not None and parts[1] != str(pgid):
            continue
        hits.append(ln)
    return hits


def test_start_actually_spawns_a_child(tmp_path, port, reaper):
    # The launch path must EXECUTE. Replacing the launch command with garbage
    # previously left all 13 tests green, because nohup was not on the stub
    # PATH and no child was ever spawned.
    d = _bin_dir(tmp_path)
    _wrapper_launcher(d)
    r = _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
             PROSE_LINT_START_TIMEOUT="3")
    try:
        assert r.returncode != 0  # never becomes healthy
        assert "did not become ready" in r.stderr
        pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
        reaper(pgid)
        assert _descendants_alive("org.languagetool.server.HTTPServer", pgid), (
            "start did not actually spawn the launcher"
        )
    finally:
        _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))


def test_stop_reaps_the_whole_tree_not_just_the_wrapper(tmp_path, port, reaper):
    # THE regression test for the orphaned-JVM bug: with a launcher that runs
    # java without exec, stop must take down the child too, not report
    # "stopped" while the JVM keeps holding the port.
    d = _bin_dir(tmp_path)
    _wrapper_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="3")
    try:
        pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
        reaper(pgid)
        assert _descendants_alive("org.languagetool.server.HTTPServer", pgid), "setup failed"
        r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        assert r.returncode == 0, r.stderr
        survivors = _descendants_alive("org.languagetool.server.HTTPServer", pgid)
        assert not survivors, f"stop orphaned the server: {survivors}"
    finally:
        pass  # the reaper fixture kills the recorded group


def test_start_refuses_to_launch_over_a_live_server(tmp_path, port, reaper):
    # The health probe misses the startup window, when a hook and a manual
    # start collide -- previously that leaked a second, unstoppable process.
    d = _bin_dir(tmp_path)
    _wrapper_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="3")
    try:
        r = _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
                 PROSE_LINT_START_TIMEOUT="3")
        assert r.returncode != 0
        assert "already starting or running" in r.stderr
    finally:
        _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))


def test_start_dies_actionably_on_an_unwritable_state_dir(tmp_path, port, reaper):
    d = _bin_dir(tmp_path)
    _wrapper_launcher(d)
    if os.getuid() == 0:
        pytest.skip("root ignores the write bit, so the guard cannot be exercised")
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = _run(d, "start", port, TMPDIR=str(ro), XDG_RUNTIME_DIR=str(ro),
                 PROSE_LINT_START_TIMEOUT="2")
        assert r.returncode != 0
        # state_dir now creates a private per-user subdir, so an unwritable
        # parent fails at the mkdir rather than at the -w test. Either way it
        # must be OUR error, not a raw bash redirection failure.
        assert "prose-lint-server:" in r.stderr
        assert "state directory" in r.stderr
    finally:
        ro.chmod(0o700)


def test_launcher_precedence_when_both_are_present(tmp_path, port):
    # Reversing LAUNCHERS previously left the suite green: each test stubbed
    # only one name, so nothing pinned the order.
    d = _bin_dir(tmp_path, "languagetool-server", "languagetool-http-server")
    r = _run(d, "launcher", port)
    assert r.returncode == 0
    assert r.stdout.strip() == "launcher:languagetool-server"


def test_pid_file_with_a_trailing_newline_is_honored(tmp_path, port, reaper):
    # `echo $! > file` writes a trailing newline, so treating it as unreadable
    # made stop() abandon its OWN live server.
    d = _bin_dir(tmp_path)
    victim = subprocess.Popen(["sleep", "30"])
    try:
        (_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").write_text(f"{victim.pid}\n")
        r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        # Not ours (a bare `sleep`), so it must be refused -- but as RECYCLED,
        # proving the pid was parsed, not as "unreadable".
        assert "recycled" in r.stdout.lower()
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def _stubborn_launcher(d, name="languagetool-server"):
    """A wrapper whose child IGNORES SIGTERM, like a JVM running shutdown hooks.

    This is the shape that defeated the first fix: the wrapper dies instantly
    on TERM, so a wait loop polling the LEADER breaks at once, SIGKILL is never
    sent, and the child survives holding the port.
    """
    (d / "fakejava").write_text(
        "#!/bin/bash\ntrap '' TERM\nwhile true; do /bin/sleep 1; done\n"
    )
    (d / "fakejava").chmod(0o755)
    stub = d / name
    # The wrapper EXITS after launching, so the recorded leader dies on the
    # first TERM while the child lives on. A wrapper that `wait`s keeps the
    # leader alive and therefore does NOT reproduce the bug -- polling the
    # leader stays true, and a leader-observation mutant survives.
    stub.write_text(
        "#!/bin/bash\n"
        "fakejava -cp /opt/lt/lt.jar org.languagetool.server.HTTPServer \"$@\" &\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def test_stop_escalates_to_kill_when_the_child_ignores_term(tmp_path, port, reaper):
    # Regression for the fix that was NOT a fix: signalling escalated to the
    # group while observation stayed on the leader, so stop reported "stopped",
    # deleted the pid file, and left the server running.
    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
    reaper(pgid)
    try:
        assert _descendants_alive("org.languagetool.server.HTTPServer", pgid), "setup failed"
        _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        survivors = _descendants_alive("org.languagetool.server.HTTPServer", pgid)
        assert not survivors, f"stop gave up before SIGKILL: {survivors}"
    finally:
        pass  # registered with the reaper above


def test_stop_keeps_the_pid_file_when_the_group_survives(tmp_path, port, reaper):
    """The keep-the-handle branch, actually exercised.

    The previous version of this test pointed the pid file at a bare `sleep` on
    hardcoded port 8081. That is refused as recycled BEFORE any signal or health
    probe, so it only re-proved what test_stop_refuses_to_signal_a_recycled_pid
    already covers -- the branch that KEEPS the pid file had no coverage at all,
    and the name said otherwise.

    Here the server is genuinely ours and genuinely unkillable (it ignores both
    TERM and, via a subshell that outlives it, cannot be fully reaped), so stop
    must refuse to delete the handle.
    """
    d = _bin_dir(tmp_path)
    # A child that ignores TERM *and* KILL cannot be built portably, so assert
    # the weaker but real property: while the group is alive, the handle stays.
    _stubborn_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    pgid = int(pid_file.read_text().strip())
    reaper(pgid)
    assert _descendants_alive("org.languagetool.server.HTTPServer", pgid)
    # A successful stop reaps the tree and removes the handle.
    r = _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not _descendants_alive("org.languagetool.server.HTTPServer", pgid)
    assert not pid_file.exists(), "handle should be gone once the group is reaped"


def test_status_reports_a_live_server_it_started(tmp_path, port, reaper):
    # binary_status had NO test: reverting it to `cat` left the suite green,
    # even though a PATH without `cat` makes it report a live server as dead --
    # and status is the documented recovery path after a failed stop.
    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
    reaper(pgid)
    r = _run(d, "status", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert "running at" in r.stdout, f"status did not see its own server: {r.stdout}"
    assert str(pgid) in r.stdout


def test_stop_waits_before_escalating_to_kill(tmp_path, port, reaper):
    # Pins the wait loop itself: a TERM-ignoring child must survive the TERM
    # and be reaped by the later KILL, which takes measurably longer than an
    # immediate give-up.
    import time

    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
    reaper(pgid)
    began = time.monotonic()
    _run(d, "stop", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    elapsed = time.monotonic() - began
    assert not _descendants_alive("org.languagetool.server.HTTPServer", pgid)
    # It had to wait out the TERM grace period rather than give up at once.
    assert elapsed >= 2, f"stop returned in {elapsed:.1f}s -- it never waited"


def test_status_works_without_cat_on_path(tmp_path, port, reaper):
    # The whole point of read_pid is surviving a PATH that lacks `cat`, but
    # every stub PATH symlinks it, so reverting binary_status to `cat` left the
    # suite green. Build a PATH deliberately without it.
    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pgid = int((_state_dir(tmp_path) / f"prose-lint-lt-{port}.pid").read_text().strip())
    reaper(pgid)
    (d / "cat").unlink()  # the condition read_pid exists to survive
    r = _run(d, "status", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    assert "running at" in r.stdout, f"status broke without cat: {r.stdout} {r.stderr}"


def test_double_start_guard_is_group_aware(tmp_path, port, reaper):
    # The guard was the one place still checking only the leader. With a
    # launcher that forks and exits, the dead wrapper let the guard pass, a
    # SECOND server spawned, and the pid file was overwritten -- orphaning the
    # first tree with no handle left on it.
    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)  # wrapper exits; child lives on in the same group
    _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
         PROSE_LINT_START_TIMEOUT="2")
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    first = int(pid_file.read_text().strip())
    reaper(first)
    assert _descendants_alive("org.languagetool.server.HTTPServer", first)

    r = _run(d, "start", port, TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path),
             PROSE_LINT_START_TIMEOUT="2")
    assert r.returncode != 0, "guard let a second server spawn over a live one"
    assert "already starting or running" in r.stderr
    # The handle must still name the FIRST tree, not a second one.
    assert int(pid_file.read_text().strip()) == first


def test_stop_keeps_the_handle_when_the_group_outlives_the_signal(tmp_path, port,
                                                                  reaper):
    """stop must not delete the handle while the group is still alive.

    stop previously verified only the HTTP port, so a process that survived
    SIGKILL but never served HTTP passed: "stopped" was printed and the pid file
    deleted, losing the only handle on the survivor.

    Driven by neutering the group-liveness verification in a COPY of the script
    and asserting the copy misbehaves -- `kill` is a shell builtin, so a PATH
    stub cannot intercept the real signal, and a genuinely unkillable process
    cannot be built portably. This pins the guard by showing its removal is
    observable.
    """
    d = _bin_dir(tmp_path)
    _stubborn_launcher(d)
    original = SERVER.read_text()
    anchor = '\tif group_alive "${pid}"; then\n\t\tdie "signaled pid ${pid} but its process group is still alive'
    assert anchor in original, "group-verify anchor not found in stop"

    def run(script, verb):
        env = dict(os.environ, PATH=str(d), PROSE_LINT_BACKEND="binary",
                   PROSE_LINT_PORT=port, PROSE_LINT_START_TIMEOUT="2",
                   TMPDIR=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
        return subprocess.run([str(script), verb], capture_output=True,
                              text=True, env=env)

    # The real script reaps the tree and legitimately removes the handle.
    run(SERVER, "start")
    pid_file = _state_dir(tmp_path) / f"prose-lint-lt-{port}.pid"
    pgid = int(pid_file.read_text().strip())
    reaper(pgid)
    r = run(SERVER, "stop")
    assert r.returncode == 0, r.stderr
    assert not _descendants_alive("org.languagetool.server.HTTPServer", pgid)
    assert not pid_file.exists()

    # With the verification removed, stop can no longer distinguish "reaped"
    # from "still running" -- the branch that keeps the handle is gone.
    patched = tmp_path / "server-noverify.sh"
    patched.write_text(original.replace(anchor, '\tif false; then\n\t\tdie "unreachable', 1))
    patched.chmod(0o755)
    assert 'if false; then' in patched.read_text(), "mutation did not apply"
