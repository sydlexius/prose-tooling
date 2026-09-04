# Genericize A1: container-runtime abstraction + backend selection

Design doc. Status: approved 2026-07-11. Scope: the runtime-portability slice of
part A (#15, under umbrella #2). Make the LanguageTool server runtime-agnostic
(docker/podman/nerdctl) and add a `container | url` backend selection, all via
environment variables. The local binary/JAR backend is separate (#18).

## Problem

`bin/prose-lint-server.sh` hardcodes `docker` (and its comments say OrbStack),
and the client always tries to auto-start that container when the server is
unreachable. Podman, nerdctl, rootless, and shared/remote-server users cannot
adopt the tool without editing it. The URL backend effectively works already
(`PROSE_LINT_SERVER` + the client's reachability probe), but there is no way to
say "use this preexisting server, never start a container," and no way to pick a
non-Docker runtime.

## Constraints

- **Env-var configuration**, consistent with the existing `PROSE_LINT_SERVER` /
  `PROSE_LINT_PORT`. No new CLI flags.
- **Additive, backward-compatible.** With no env set, behavior is identical to
  today (auto-detect docker, container backend).
- Bash stays `shellcheck` clean; Python stdlib-only. Linux + macOS.
- Local-first privacy unchanged: still a local/self-hosted server; the URL
  backend points at the adopter's own server, never a public API.
- No silent failures: an invalid runtime/backend value errors loudly.

## Runtime abstraction

The three runtimes are CLI-compatible for every command the script uses
(`run -d --name --restart -p -e`, `ps -q -f name=`, `start`, `stop`, `logs`), so
abstraction is a single resolved `$RUNTIME` variable substituted for `docker`.

Resolution order (in `prose-lint-server.sh`):
1. If `PROSE_LINT_RUNTIME` is set: it must be one of `docker|podman|nerdctl` AND
   on `PATH`, else die with a clear message (no silent fallback).
2. Else auto-detect: the first of `docker`, `podman`, `nerdctl` found on `PATH`.
3. Else die: "no container runtime found (docker/podman/nerdctl); set
   PROSE_LINT_RUNTIME or use the url backend."

All `docker ...` invocations become `"${RUNTIME}" ...`. The `--restart
unless-stopped` policy is supported by all three. Rootless podman/nerdctl binding
to `127.0.0.1:${HOST_PORT}` works. The header comment drops the OrbStack-specific
wording.

### Testability seam

Add a `runtime` subcommand: `prose-lint-server.sh runtime` prints the resolved
runtime binary name and exits 0 (or errors per step 3). This lets tests assert
selection with stub executables on a temp `PATH`, with no real container.

## Backend selection

`PROSE_LINT_BACKEND` env: `container` (default) | `url`.

- `container`: the current path, now runtime-agnostic. The client auto-starts via
  the server script when the server is unreachable (unchanged behavior).
- `url`: the server is preexisting/remote (its address is `PROSE_LINT_SERVER`).
  The client probes reachability but NEVER starts a container; if unreachable it
  errors with guidance (the server is not ours to start). An invalid value errors.

The server script itself is only invoked for the `container` backend; under `url`
the client never calls it.

## Client changes (`bin/prose_check.py`)

- `ensure_server(server, start_fn, is_up)` gains backend awareness: when
  `PROSE_LINT_BACKEND=url`, it checks `is_up` and returns that result WITHOUT
  calling `start_fn` (no autostart). Default/`container` keeps today's behavior.
- A small `_backend()` reader returns the validated backend (`container` default;
  invalid value raises). The autostart decision in `main` consults it.
- No change to checking/rule logic.

## install.sh + docs

- `install.sh`: its server-start guidance notes the runtime is auto-detected and
  that `PROSE_LINT_RUNTIME` / `PROSE_LINT_BACKEND` (and the `url` backend for a
  shared server) are available. No behavior change to the scaffold.
- `docs/CONFIG.md`: a "Backends and runtimes" section documenting
  `PROSE_LINT_SERVER`, `PROSE_LINT_PORT`, `PROSE_LINT_RUNTIME`,
  `PROSE_LINT_BACKEND`, the resolution/auto-detect order, and when to use `url`.
- `README.md` / `CLAUDE.md`: de-OrbStack the one-liners describing the server
  (state docker/podman/nerdctl), keep them accurate.

## Error handling

- Unknown `PROSE_LINT_RUNTIME` or a set-but-absent runtime -> die, exit non-zero,
  naming the offending value and the valid set. No fallback to another runtime
  (silent fallback would mask a typo).
- `container` backend with no runtime available -> die with the "set
  PROSE_LINT_RUNTIME or use the url backend" guidance.
- `url` backend unreachable -> the client errors (exit 2, the existing
  server-unreachable code path) without attempting a start.
- Unknown `PROSE_LINT_BACKEND` -> raise/die loudly.

## Testing

- **Server script (subprocess + stub PATH):** `prose-lint-server.sh runtime`
  returns the `PROSE_LINT_RUNTIME` value when valid+present; auto-detects the
  first stub of docker/podman/nerdctl on a temp `PATH`; dies on an unknown value;
  dies when none present. Stubs are tiny executables on a temp `PATH`; no real
  container. `shellcheck` clean.
- **Client (pytest):** `ensure_server` with `PROSE_LINT_BACKEND=url` and
  `is_up=lambda: False` returns False and never calls `start_fn` (spy); with
  `container` it calls `start_fn` as today. `_backend()` returns `container` by
  default and raises on an invalid value.
- Full gate: `pytest`, `ruff`, `shellcheck` green.

## Out of scope

- Local binary/JAR backend (#18) - the third backend, added on this selection
  layer.
- Windows (the AC targets Linux + macOS).
- Auto-installing/pulling a runtime or the image - the script still assumes the
  chosen runtime and image are available/pullable as today.

## Rollout

One PR closing #15, off `main`, well under 1KLOC (a bash refactor + a small
client seam + docs + tests). #2 stays open until #18 (A2) also lands.
