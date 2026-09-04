# prose-tooling - Claude Code Project Instructions

A local-first grammar/prose linter built on a local LanguageTool server, wired
into git hooks and CI across repos. Hand-maintained; keep it accurate and lean.
(Global style basics - no emoji, no em-dashes, Pacific-labeled times, TOML over
YAML - live in the user-global CLAUDE.md; only repo-specific deltas here.)

## >> ON SESSION START / RESUME: read SESSION-STATE.md FIRST (if present) <<

`SESSION-STATE.md` (repo root; gitignored, machine-local) is the orchestrate session
checkpoint - read it before doing anything when asked to resume / pick up work. It holds
only non-derivable intent + pointers (status banner, next actions); reboot-durable
derivables (in-flight PRs via `gh pr list`, worktrees via `git worktree list`) are
reconstructed on demand, not mirrored. Absent on a fresh checkout; created on the first
orchestrate session.

## Project Overview

`bin/prose_check.py` is a Markdown-aware client that feeds prose (not markup) to
a local LanguageTool server, maps findings back to source lines, and splits them
into blocking vs advisory per a central per-language house-style config. See
`README.md` and the design specs under `docs/superpowers/specs/`.

## Style and Conventions

- Python: stdlib-first; the ONLY runtime dependency is `markdown-it-py`. Lint
  gate is `ruff check` (config `ruff.toml`, select F,E741). Deps for the client
  live in `.venv`, so consuming repos gain none.
- Shell: `shellcheck` clean.
- Tests: `pytest` - hermetic unit tests plus live integration tests that
  auto-skip when no LanguageTool server is reachable.
- Privacy: all checking runs against a LOCAL LanguageTool server; repo content
  is never sent to the public API.

## Architecture

- `bin/prose_check.py` - the client. Extractor (Markdown; i18n planned) ->
  prose blocks tagged with source line -> one LanguageTool check -> allowlist
  filter -> severity partition -> `path:line` findings. Auto-starts the
  container when it is down (`--no-autostart` to disable).
- `bin/prose-lint-server.sh` - LanguageTool container lifecycle. The runtime is
  resolved once (`PROSE_LINT_RUNTIME`, else first of docker/podman/nerdctl on
  PATH, else die); `prose-lint-server.sh runtime` prints the choice.
  `PROSE_LINT_BACKEND=url` makes the client use a preexisting server and never
  autostart. Documented in `docs/CONFIG.md`.
- `bin/install.sh` - prints the git-hook wiring for a consuming repo.
- `config/<lang>/severity.toml` + dictionaries - per-language rule set,
  blocking/advisory map, and spelling allowlist. `config/dictionary.txt` is the
  global allowlist merged into every language.
  The en-US British-spelling corpus (`config/en-US/british-american.txt`) is
  generated from a vendored VarCon by `bin/gen_british_spellings.py`; edit
  `british-american.overrides.txt` (not the generated file) then regenerate.
- `docs/superpowers/specs|plans/` - design and implementation docs.

## Common Commands

```sh
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt  # setup
bin/prose-lint-server.sh start|stop|status                           # server
./.venv/bin/python bin/prose_check.py <files>                        # lint
./.venv/bin/python -m pytest                                         # tests
ruff check bin/ tests/ ; shellcheck bin/*.sh                         # lint
make hooks                                                           # install pre-commit hook
```

## Key Rules

- Exit codes: `0` clean or advisory-only, `1` a blocking finding, `2` server
  unreachable.
- Blocking rules are deterministic, low-false-positive only (em-dash, one-space,
  serial comma, and en-US British spellings via the curated client-side
  `LOCAL_BRITISH_SPELLING` map). General spelling and style stay ADVISORY (the
  consuming repo's `typos` already blocks spelling; LanguageTool flags bare code
  identifiers, and its en-US dictionary accepts some British variants like
  `catalogue` that only the curated map catches). Tune per-language in
  `config/<lang>/severity.toml`.
- Line mapping uses the PER-BLOCK engine. Do NOT reintroduce the LanguageTool
  `data`-annotation global-offset approach - it mislocates findings on inline
  markup (see the design spec's "Calibration outcomes").

## PR Workflow

- CI (`.github/workflows/ci.yml`) runs pytest against a digest-pinned
  LanguageTool service container plus ruff + shellcheck. Branch protection on
  `main` requires the Tests + Lint checks, a PR, and conversation resolution.
- Land changes via a GitHub squash-merge - GitHub signs the squash commit
  (Verified) and sets the author to the repo owner. NEVER direct-push to `main`.
- CodeRabbit auto-review is OFF (`.coderabbit.yaml`); it reviews only when the
  maintainer runs `@coderabbitai review`. Codoki auto-reviews every PR.
