# Config schema and adoption guide

`bin/prose_check.py` reads all rule/severity/allowlist config from a **config
directory** (`--config-dir`, default `config/` in this repo) and an optional
**repo-local `.prose-lint.toml`** in the current working directory. This
document covers every key. Ground truth is `bin/prose_check.py` (`load_bundle`,
`_resolve_i18n_ignore`, `key_ignorer`) and `config/en-US/severity.toml`; if this
doc and the code ever disagree, the code wins.

## Config directory layout

```text
<config-dir>/
  dictionary.txt              # global spelling allowlist, merged into every bundle
  <bundle>/severity.toml      # rules + blocking map for one bundle
  <bundle>/dictionary.txt     # per-bundle spelling allowlist
```

A "bundle" is a language/profile pair such as `en-US` or `en-US-microcopy`
(the client appends `-microcopy` to `--lang` when `--profile microcopy` is
passed, or when an i18n JSON file is checked without an explicit `--profile`).
Each bundle is its own directory under the config dir;
`load_bundle(config_dir, bundle_name)` reads
`<config_dir>/<bundle_name>/severity.toml`.

## `severity.toml`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `language` | string | yes | LanguageTool language code (e.g. `"en-US"`). `load_bundle` raises `KeyError` if this key is missing. |
| `level` | `"default"` \| `"picky"` | no (defaults to `"picky"`) | LanguageTool's rule-set tier. `"picky"` unlocks the pedantic style rules (passive voice, readability, ...) on top of the default set. |
| `enabled_rules` | list of rule IDs | no (defaults to `[]`) | Rules to force-enable, sent to the server as `enabledRules`. Use this to pin a rule that is already active at your `level` but you want explicit, or to turn on a rule the default/picky tier omits. |
| `disabled_rules` | list of rule IDs | no (defaults to `[]`) | Rules to force-disable, sent as `disabledRules`. Use this to silence server rules that misfire on technical Markdown (quote style, dash spacing, whitespace layout, and so on -- see `config/en-US/severity.toml` for a worked example with rationale comments). |
| `disabled_categories` | list of category IDs | no (defaults to `[]`) | Category-level version of `disabled_rules`, sent as `disabledCategories`. |
| `blocking` | list of rule IDs and/or category IDs | no (defaults to `[]`) | Any match whose rule ID *or* category ID appears here fails the commit (exit `1`); everything else is printed but advisory (exit `0`). Keep this list to deterministic, low-false-positive rules -- see "Choosing what to block" below. |

All four list keys accept a mix of server-side rule/category IDs (as returned
by the LanguageTool `/v2/check` response, e.g. `SERIAL_COMMA_ON`) and the
client-side local rule IDs described next.

### Client-side local rules

Three house rules have no usable free-server equivalent, so the client
computes them itself (`local_matches_text` in `bin/prose_check.py`) and reports
them under a synthetic `LOCAL` category:

- `LOCAL_EM_DASH` -- flags em-dashes (`—`); house style prefers a dash,
  comma, or parentheses.
- `LOCAL_DOUBLE_SPACE` -- flags two or more spaces after sentence-ending
  punctuation.
- `LOCAL_BRITISH_SPELLING` -- flags British spellings against a curated,
  whole-word British-to-American map (`config/en-US/british-american.txt`,
  generated from VarCon/SCOWL, plus house overrides in
  `british-american.overrides.txt`). American-English-only: it fires when the
  bundle's `language` starts with `en-us` (case-insensitive). LanguageTool's
  own spelling rule
  (`MORFOLOGIK_RULE_EN_US`) cannot fill this role because it is advisory-only
  in this setup and its dictionary accepts some British variants (e.g.
  `catalogue`) outright.

Because these three are computed client-side, they can appear in `blocking`,
`enabled_rules`, `disabled_rules`, or `disabled_categories` exactly like a
server rule ID, but disabling them has no effect on a server request -- the
client simply skips reporting them (through the same allowlist/partition
pipeline as everything else).

### Choosing what to block

`blocking` is deliberately narrow in the shipped `config/en-US/severity.toml`:
`LOCAL_EM_DASH`, `LOCAL_DOUBLE_SPACE`, `LOCAL_BRITISH_SPELLING`, and
`SERIAL_COMMA_ON`. General spelling (`MORFOLOGIK_RULE_EN_US`) stays advisory on
purpose: a consuming repo's `typos` hook already blocks spelling, and
LanguageTool's dictionary flags bare code identifiers (`BaseItemDto`,
`golangci-lint`, ...) that are not misspellings. Style rules (passive voice,
readability, wordiness) stay advisory too -- they are useful signal but too
subjective/noisy to fail a commit on. When adopting your own config, keep this
shape: block only rules you have verified are deterministic and low-false-
positive for your prose; leave the rest advisory.

## `dictionary.txt` (spelling allowlist)

Two allowlist files are merged for every check:

- `<config-dir>/dictionary.txt` -- global, applies to every bundle.
- `<config-dir>/<bundle>/dictionary.txt` -- bundle-specific.

Format: one word per line, `#`-prefixed lines are comments, blank lines are
ignored. Matching is case-insensitive (`_read_wordlist` lowercases every
entry, and `filter_allowlisted` lowercases the flagged word before comparing).
An allowlist entry only suppresses spelling findings (LanguageTool's `TYPOS`
category) -- it has no effect on grammar/style rules.

## `.prose-lint.toml` (repo-local, i18n key ignores)

A repo being linted can carry its own `.prose-lint.toml` at its root (not in
the config dir -- this file travels with the *content* repo, not the tooling's
config bundle) to skip i18n keys that are not prose:

```toml
[i18n]
ignore_keys = [
    "*.tooltip",   # fnmatch glob: matches any dotted key ending in .tooltip
    "*.log_*",     # fnmatch glob
    "app.version", # exact dotted key
]
```

`ignore_keys` entries are matched against each i18n JSON value's dotted key
(nested objects/arrays flatten to `a.b`, `a.0`, ...) with
`fnmatch.fnmatchcase`, so both glob patterns and exact keys work in the same
list. Only i18n extraction (`--format i18n`, or a `.json` file when `--format`
is not given) consults this file; Markdown checks ignore it entirely.

### Precedence

`_resolve_i18n_ignore` resolves the ignore list in this order, first match
wins:

1. `--i18n-ignore <path>` -- an explicit path, read unconditionally.
2. `./.prose-lint.toml` in the current working directory -- auto-loaded if it
   exists and no `--i18n-ignore` was given.
3. No ignores (empty list) if neither is present.

A malformed TOML file (either path) raises from `tomllib` rather than
silently degrading to "no ignores" -- a broken ignore list should fail loudly,
not quietly re-flag everything (or, worse, silently widen what is ignored).

## Adopting your own config

Two ways to bring your own rules, from least to most involved:

1. **Point `--config-dir` at your own directory.** No changes to this tool are
   needed: build a directory following the layout above (a top-level
   `dictionary.txt` plus one subdirectory per bundle with its own
   `severity.toml` and `dictionary.txt`) and pass
   `--config-dir /path/to/your/config` on every invocation (or bake it into
   your hook wiring). `examples/config/` is a minimal starting point you can
   copy and edit.

2. **Scaffold with `bin/install.sh <repo> --with-config`.** This copies
   `examples/config/` into `<repo>/.prose-lint-config/` (existing files are
   never overwritten -- `cp -Rn`), writes a starter `.prose-lint.toml` at the
   repo root (backing up any existing one to `.prose-lint.toml.bak`), and
   prints hook wiring that already points `--config-dir` at the scaffolded
   directory. See `bin/install.sh --help`-equivalent usage in its header
   comment, or just run it against a scratch repo to see the output.

Either way, the scaffolded/copied `severity.toml` files are yours to edit
freely -- add domain nouns to `dictionary.txt`, tune `disabled_rules` for your
prose, and keep `blocking` narrow per "Choosing what to block" above.

## Backends and runtimes

Where the LanguageTool server lives is configured entirely by environment
variables. With none set, prose-check auto-starts a local container -- the
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
your dev compose stack, a systemd unit). The client probes `PROSE_LINT_SERVER`
and, if it is unreachable, exits 2 with guidance rather than starting a
container.

Prose is sent to whatever server `PROSE_LINT_SERVER` names, so under this
backend the privacy boundary is yours to set: point it at a server you control
and trust. A URL outside your network sends repo content there, and the public
LanguageTool API is never an appropriate target for private content.

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

An empty value means "not set" for both `PROSE_LINT_BACKEND` and
`PROSE_LINT_RUNTIME`, so `PROSE_LINT_BACKEND="${SOME_UNSET_VAR}"` in a CI job
falls back to the default rather than failing. Any other value outside the valid
set is an error, including one that is merely padded with whitespace -- pass
`docker`, not `" docker "`.
