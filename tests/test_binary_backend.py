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
    bash = shutil.which("bash")
    assert bash, "bash is required to run the server script"
    if not (d / "bash").exists():
        (d / "bash").symlink_to(bash)
    return d


def _run(path_dir, subcmd, **env):
    environ = dict(os.environ)
    environ["PATH"] = str(path_dir)
    for var in ("PROSE_LINT_RUNTIME", "PROSE_LINT_JAR", "PROSE_LINT_START_TIMEOUT"):
        environ.pop(var, None)
    environ["PROSE_LINT_BACKEND"] = "binary"
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
