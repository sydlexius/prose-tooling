# prose-tooling

Cross-repo grammar and prose linting for the `~/Developer` repos, built on a
local [LanguageTool](https://languagetool.org) server. Fills the gap left by
`typos` (spelling) and `markdownlint` (markup): real, parser-based grammar and
style checking, driven by one central house-style config, wired into git hooks.

Design: [`docs/superpowers/specs/2026-07-05-cross-repo-grammar-tooling-design.md`](docs/superpowers/specs/2026-07-05-cross-repo-grammar-tooling-design.md).

## Layout

- `bin/prose-lint-server.sh` -- start/stop/status the LanguageTool container
  (docker, podman, or nerdctl; auto-detected). See "Backends and runtimes" in
  `docs/CONFIG.md` to pin a runtime or point at a server you already run.
- `bin/prose_check.py` -- the Markdown-aware client (deps in `.venv`, so target repos stay dependency-free).
- `bin/install.sh` -- scaffold a starter `.prose-lint.toml` into a target repo and print the git-hook wiring (`--with-config` also copies a starter config dir).
- `config/<lang>/severity.toml` -- per-language rules + blocking-vs-advisory map.
- `config/dictionary.txt`, `config/<lang>/dictionary.txt` -- spelling allowlists.

See [docs/CONFIG.md](docs/CONFIG.md) for the full config schema and adoption guide.

## Use

```sh
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
bin/prose-lint-server.sh start
./.venv/bin/python bin/prose_check.py path/to/doc.md
```

Exit codes: `0` clean or advisory-only, `1` a blocking finding, `2` server
unreachable. The client auto-starts the container if it is stopped (one-time
~15s cost on the first commit after a stop); pass `--no-autostart` to disable.

## How it works

The client parses Markdown, feeds only prose to LanguageTool via its `data`
annotation API (markup excluded, source offsets preserved), merges in
client-side local rules, drops allowlisted spellings, then splits findings into
blocking (fails the commit) and advisory (printed, exit 0) per
`config/<lang>/severity.toml`. Everything runs against a LOCAL server -- repo
content is never sent to the public API.

### House rules (en-US)

Blocking: em-dash (`LOCAL_EM_DASH`), one space after sentence punctuation
(`LOCAL_DOUBLE_SPACE`), British spellings (`LOCAL_BRITISH_SPELLING`), Oxford/serial
comma (`SERIAL_COMMA_ON`). Advisory: general spelling (`MORFOLOGIK_RULE_EN_US`;
`typos` already blocks spelling in the hooks, and LT flags bare code
identifiers), passive voice, readability, and wordiness (the last is partly
premium-gated on the free server). Em-dash, double-space, and British-spelling
are deterministic client-side rules because the free server has no usable rule
for them - MORFOLOGIK reports British spellings only advisory-side and its en-US
dictionary accepts some (`catalogue`) outright, so a curated whole-word
British->American map does the enforcing. See the design spec's "Calibration
outcomes" for the disabled-rule list and rationale.

The British-spelling list is a corpus generated from VarCon (SCOWL, public
domain) by `bin/gen_british_spellings.py` into `config/en-US/british-american.txt`,
with house-style adds/exclusions in `config/en-US/british-american.overrides.txt`.
Regenerate after editing overrides or refreshing the vendored `config/en-US/varcon.txt`.

## Tests

```sh
./.venv/bin/python -m pytest tests/
```

Unit tests are hermetic; the integration tests skip unless a server is running.
