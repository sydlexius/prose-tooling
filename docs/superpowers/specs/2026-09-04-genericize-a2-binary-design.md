# Genericize A2: local binary/JAR LanguageTool backend

Design doc. Status: approved 2026-09-04. Scope: the third backend for #18, under
umbrella #2. Run the LanguageTool server as a plain Java process, with no
container, selected by `PROSE_LINT_BACKEND=binary`. A1 (#15, PR #41) landed the
runtime abstraction and the `container | url` selection layer this builds on.

## Problem

The server can run in a container (`container`) or be pointed at one somebody
else runs (`url`). Neither serves an adopter who has Java and wants a local
server without a container runtime: a machine with no Docker/Podman/nerdctl, a
locked-down host where containers are unavailable, a CI job that already has a
JVM, or someone who simply installed LanguageTool from a package manager
(`brew install languagetool` ships 6.8 today).

Today that adopter is stuck. `container` dies with "no container runtime found",
and `url` requires them to hand-start a server and manage its lifetime
themselves, which is exactly the work `prose-lint-server.sh` exists to do.

## Constraints

- **Env-var configuration**, consistent with `PROSE_LINT_SERVER`,
  `PROSE_LINT_PORT`, `PROSE_LINT_RUNTIME`, `PROSE_LINT_BACKEND`. No new CLI flags.
- **Additive and backward-compatible.** With nothing set, behavior is unchanged:
  auto-detect a container runtime, container backend.
- **Bring your own JAR.** Nothing is downloaded, ever. See "Rejected: fetching".
- Bash stays `shellcheck` clean; Python stdlib-only (plus `markdown-it-py`).
  Linux + macOS. Windows out of scope.
- Local-first privacy unchanged: the process binds to `127.0.0.1`.
- No silent failures: every unresolvable condition dies loudly and actionably.
- **Tests must not require Java.** The development machine has no JVM (see
  "The Java stub trap"), so the unit tests are stub-driven. (A separate CI job
  DOES install a JVM to prove the backend really works -- see "Running in CI".)
- **Must run unattended on any CI runner** -- GitHub Actions, a Gitea/act
  runner, or anything else. That means: no TTY, no prompts, no GitHub-specific
  machinery, no assumption that a container runtime is available, a start that
  returns promptly without holding the step's output pipe open, a configurable
  readiness timeout, and meaningful exit codes. This is a first-class use case,
  not an afterthought: a runner that cannot nest containers is precisely the
  environment the binary backend exists for.

## Backend selection

`_BACKENDS` in `bin/prose_check.py` gains `binary`:
`PROSE_LINT_BACKEND` = `container` (default) | `url` | `binary`.

The client needs no other change. Its autostart path already shells out to
`bin/prose-lint-server.sh start`, and that script dispatches on the backend, so
`binary` autostarts a JVM by the same route `container` starts a container. The
`url` short-circuit in `ensure_server` (probe, never start) is unaffected.

`bin/prose-lint-server.sh` reads `PROSE_LINT_BACKEND` itself and dispatches
`start` / `stop` / `status` / `restart` to either the container implementation
(today's code) or the new binary implementation. Invoking the script directly
under `PROSE_LINT_BACKEND=url` is an error: there is no lifecycle to manage for a
server that is not ours.

## Lazy runtime resolution (a bug this fixes)

`bin/prose-lint-server.sh` currently resolves the container runtime at script
load:

```bash
RUNTIME="$(resolve_runtime)" || exit 1
```

That is unconditional, so a Java-only box with no container runtime dies at line
48 before it can reach the binary backend it asked for. Resolution becomes lazy:
`resolve_runtime` is called only from the container implementation's functions,
never at load. The `runtime` subcommand keeps its current behavior (resolve and
print, or die), since asking for the runtime is an explicit request to resolve
one.

This also means `PROSE_LINT_RUNTIME` is not validated under the binary backend,
which is correct: it names a container runtime that the binary backend does not
use.

## JAR / launcher discovery

Resolution order, first match wins:

1. **`PROSE_LINT_JAR`** if set. Must be an existing readable file, else die
   naming the path. No glob expansion, no directory search.
2. **A launcher on `PATH`**, tried in order: `languagetool-server`,
   `languagetool-http-server`. These are what distribution packages install
   (Homebrew's `languagetool` formula provides `languagetool-server`).
3. **Else die**, naming both remedies: set `PROSE_LINT_JAR=/path/to/languagetool-server.jar`,
   or install LanguageTool so a launcher is on `PATH`.

A launcher wins over nothing but loses to an explicit `PROSE_LINT_JAR`, so an
adopter with both installed and vendored can pin the one they mean.

### The Java stub trap

`command -v java` is NOT a sufficient check, and this is not hypothetical: macOS
ships `/usr/bin/java` as a stub that exists, is executable, and prints "Unable to
locate a Java Runtime" while exiting non-zero when no JDK is installed. The
development machine for this work is in exactly that state. A `command -v` guard
would pass there and then fail at `java -jar` with a confusing error.

So the JAR path (only) probes Java for real:

```sh
java -version >/dev/null 2>&1
```

A non-zero exit dies with an actionable message ("java on PATH is not a working
runtime -- on macOS /usr/bin/java is a stub until a JDK is installed"). The
launcher path does not probe: a packaged launcher is responsible for finding its
own JVM, and second-guessing it would reject valid setups (a launcher with a
bundled or pinned JVM).

## Process lifecycle

### PID file

Path: `${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/prose-lint-lt-${HOST_PORT}.pid`.

Keyed by port so two servers on different ports do not collide, matching the way
`PROSE_LINT_PORT` already parameterizes the container's host binding.

### start

1. If already healthy at `${URL}`, print "already running" and exit 0. This is
   backend-agnostic and cheap, and it makes `start` idempotent.
2. Resolve the JAR or launcher; probe Java if it is a JAR.
3. Launch detached, output to a log file next to the PID file:
   - JAR: `java -Xmx1g -cp <jar> org.languagetool.server.HTTPServer --port <port> --allow-origin '*'`
     (matching the container image's entry point; heap capped as the container's
     `Java_Xmx=1g` does).
   - Launcher: `<launcher> --port <port>`.
   Bound to `127.0.0.1` via the server's own `--public` being ABSENT (the
   LanguageTool HTTP server binds loopback unless `--public` is passed), which is
   the privacy default this tool requires.
4. Write the PID.
5. Poll `${URL}/v2/languages` until ready, up to `PROSE_LINT_START_TIMEOUT`
   seconds (default 30, matching the container path). A cold JVM on a slow or
   contended shared runner can exceed 30s, so CI needs this knob; the default
   keeps local behavior unchanged.
   On timeout: die pointing at the log file.

`start` must **return promptly and release the caller's stdout/stderr**. The
child is detached with its output redirected to the log file, never inherited.
A background child holding the pipe open hangs a CI step forever -- the step
waits on EOF that never comes, and the job dies at the runner's global timeout
with no useful error. Verified locally: the launcher returns immediately and the
child survives it.

### stop

A naive `kill "$(cat pidfile)"` is unsafe: PIDs are recycled, so a stale file can
name an unrelated process, and killing it would be indiscriminate. `stop`
therefore verifies before signaling:

1. No PID file -> "not running", exit 0.
2. PID not alive (`kill -0` fails) -> stale file; remove it, report "not running
   (stale pid file removed)", exit 0.
3. PID alive but its command line does not look like our LanguageTool server
   (checked with `ps -ww -o command= -p <pid>` matching `languagetool` or
   `HTTPServer`; `-ww` because a `java -cp` command line is long and a
   truncated one could fail to match our own process) -> **do not signal it**. Remove the stale file and report that
   the PID was recycled. Signaling here is the failure mode to avoid.
4. Otherwise `TERM`, poll up to 10s for exit, then `KILL` as a last resort.
   Remove the PID file. Report which signal ended it.

Consistent with the A1 `stop()` fix: a failure to stop is reported loudly and
exits non-zero, never a cheerful "not running".

### status

Reports the PID and liveness from the PID file plus a health probe of
`${URL}/v2/languages`, mirroring the container path's output shape. Exits
non-zero when not running, as today.

### restart

`stop` then `start`, unchanged in shape.

## Running in CI (any runner)

This backend is the one that works where a container runtime is unavailable or
cannot be nested, which is a common CI shape. Nothing about the design is
GitHub-specific: it is a shell script driven by environment variables, so any
runner that can run bash drives it identically. No composite action, no
marketplace dependency, no `GITHUB_*` variables.

**What the runner must provide:** bash, `curl`, a working JVM, and a JAR or
launcher. That is the whole contract.

**Where the JAR comes from on a runner.** The tool never downloads (see
"Rejected: fetching"), but a runner has no `brew`, so the *workflow* fetches it
in one step. That is the right split: the fetch is visible in the job
definition, pinned and cacheable by the adopter, and auditable in review --
rather than hidden inside a git hook that runs on every commit. The tool's trust
surface stays empty either way.

A generic recipe, portable to GitHub Actions, Gitea Actions, or any runner:

```sh
# 1. A JVM must be on PATH (setup-java on GH/Gitea, or the distro package).
# 2. Fetch a pinned LanguageTool once; cache the directory on LT_VERSION.
curl -fsSLo lt.zip "https://languagetool.org/download/LanguageTool-${LT_VERSION}.zip"
unzip -q lt.zip
# 3. Point the backend at it and run.
export PROSE_LINT_BACKEND=binary
export PROSE_LINT_JAR="$PWD/LanguageTool-${LT_VERSION}/languagetool-server.jar"
export PROSE_LINT_START_TIMEOUT=120     # cold JVM on a shared runner
bin/prose-lint-server.sh start
python bin/prose_check.py docs/*.md
bin/prose-lint-server.sh stop
```

Adopters who prefer not to fetch at all can vendor the JAR or bake it into a
runner image; `PROSE_LINT_JAR` does not care which.

**Why each CI-shaped hazard is handled:**

| Hazard | Handling |
| --- | --- |
| No TTY, no prompts | Pure env-var config; nothing reads stdin. |
| Step hangs on an inherited pipe | `start` detaches the child and redirects its output to the log; it never holds the caller's stdout. |
| Cold JVM slower than 30s | `PROSE_LINT_START_TIMEOUT` (default 30). |
| No container runtime on the runner | Runtime resolution is lazy; the binary backend never asks for one. |
| Failure must fail the job | Every error path dies non-zero; the client keeps exit 1 blocking / 2 unreachable. |
| Debugging a failed start | The readiness timeout names the log file, which the job can `cat` in an always-run step. |

**Proof, not assertion.** This repo's own CI gains a job that installs a JVM,
fetches a pinned LanguageTool, and runs a real start / check / stop cycle
against the binary backend. The unit tests stay stub-driven and JVM-free; this
job exists so the claim "it works on a runner" is measured rather than believed.
It runs on `ubuntu-latest` and is written so the same steps work unmodified on a
Gitea runner.

## Error handling

- `PROSE_LINT_JAR` set but missing/unreadable -> die naming the path.
- No JAR and no launcher -> die naming both remedies.
- `java` absent, or present but not a working runtime (the macOS stub) -> die
  with the stub-specific hint.
- Server does not become ready in 30s -> die pointing at the log file.
- Stale or recycled PID -> never signal; clean up and report.
- `prose-lint-server.sh` invoked under `PROSE_LINT_BACKEND=url` -> die: the
  server is not ours to manage.
- Unknown `PROSE_LINT_BACKEND` -> die naming the valid set (existing behavior,
  now three values).

## Testing

Everything is stub-driven; no JVM is required, on the dev machine or in CI.

- **Discovery:** `PROSE_LINT_JAR` wins over a launcher on PATH; a launcher is
  found when no JAR is set; a missing `PROSE_LINT_JAR` dies naming the path; no
  JAR and no launcher dies naming both remedies.
- **Java stub:** a stub `java` that exits non-zero (mimicking macOS) makes the
  JAR path die with the stub hint, rather than proceeding to `java -jar`. A
  working stub `java` proceeds. The launcher path does NOT probe java.
- **Lazy runtime:** with NO container runtime on PATH at all,
  `PROSE_LINT_BACKEND=binary` still starts. This is the regression test for the
  load-time-resolution bug, and it fails against the current script.
- **stop safety:** no PID file -> "not running"; a dead PID -> stale file removed,
  no signal; a live PID whose command line does not match -> NOT signaled and
  reported (asserted by having the "process" be a sentinel the test can prove
  survived); a matching live process -> TERM'd and the file removed.
- **Client:** `_backend()` accepts `binary`; `ensure_server` treats it like
  `container` (autostart allowed), not like `url`.
- Full gate: `pytest`, `ruff`, `shellcheck` green.

Every new test is verified to fail against the unfixed implementation, per this
repo's established practice of mutation-checking each guard.

## Rejected: fetching the JAR

An earlier option was an explicit `fetch-jar` subcommand (pinned version +
SHA-256) or auto-download on first start. Both rejected for v1:

- A git pre-commit hook that silently downloads a large archive is surprising and
  awkward on a locked-down or offline machine.
- A pinned version and checksum is a maintenance obligation on every LanguageTool
  release, for a convenience `brew install languagetool` already provides.
- It enlarges the trust surface of a tool whose whole premise is local-first.

If adopters ask for it, it is additive on top of this design: discovery grows a
step, nothing here changes.

This is not in tension with CI. A runner has no package manager to BYO from, so
the workflow fetches the JAR itself in one visible, pinnable, cacheable step
(see "Running in CI"). The distinction that matters is not whether a download
ever happens -- it is WHO performs it and whether it is auditable. A fetch
declared in a job definition is reviewable and version-pinned by the adopter; a
fetch hidden inside a pre-commit hook is neither.

## Out of scope

- Downloading or installing the JAR, a JVM, or n-gram data.
- LanguageTool server config files (`--config`), custom rules, premium features.
- Running as a system service (systemd unit, launchd plist). The container
  backend's `--restart unless-stopped` has no binary-backend equivalent, and
  saying so in the docs is the honest answer rather than half-implementing one.
- Windows.
- The runtime support matrix (#42), which is orthogonal and already tracked.

## Rollout

One PR closing #18, off `main`. Closes umbrella #2 as well, since A1 (#15) and B
(#16) have both landed and A2 is the last piece. Docs: a "binary backend" section
in `docs/CONFIG.md` covering discovery, the Java requirement, the macOS stub, and
the absence of a restart-on-boot equivalent; `install.sh` guidance; `README.md`
and `CLAUDE.md` one-liners.
