"""prose_check -- Markdown-aware grammar/prose lint client for LanguageTool.

See docs/superpowers/specs/2026-07-05-cross-repo-grammar-tooling-design.md.

Location mapping (per-block engine): Markdown is split into prose blocks, each
tagged with its source line. The blocks are joined into ONE pure-prose string
(no markup) and checked in a single request; because the checked string
contains no markup, LanguageTool's offsets cannot drift against it, and each
offset maps back to a source line via the block it falls in.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from markdown_it import MarkdownIt

# commonmark plus the GFM table rule, so table syntax is structure (excluded),
# not prose. (Plain commonmark would leak `|`/`---` rows as text.) We avoid the
# full gfm-like preset because it enables linkify, which needs an extra package.
_MD = MarkdownIt("commonmark").enable("table")

# A leading YAML frontmatter block: `---` on line 1, prose lines, closing `---`.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL)

# Suppression directives (HTML comments; never rendered). Matched exactly so
# `disable` does not also match `disable-line`.
_DIRECTIVE_RE = re.compile(
    r"<!--\s*prose-lint-(disable-next-line|disable-line|disable|enable)\s*-->"
)


def _strip_frontmatter(markdown_text):
    """Return (body, leading_line_count) with any YAML frontmatter removed."""
    match = _FRONTMATTER_RE.match(markdown_text)
    if not match:
        return markdown_text, 0
    return markdown_text[match.end():], match.group(0).count("\n")


def suppressed_lines(text):
    """Return the set of 1-indexed source lines to skip per suppression directives."""
    suppressed = set()
    in_region = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        directives = _DIRECTIVE_RE.findall(line)
        # Apply enable/disable region toggles first (line-order within the line).
        for d in directives:
            if d == "enable":
                in_region = False
            elif d == "disable":
                in_region = True
        if in_region:
            suppressed.add(lineno)
        if "disable-line" in directives:
            suppressed.add(lineno)
        if "disable-next-line" in directives:
            suppressed.add(lineno + 1)
    return suppressed


class Block:
    """A run of prose plus the source line its first character sits on.

    ``text`` is the reconstructed prose sent to the server (inline code becomes
    a space, markup dropped). ``children`` is the list of (raw_text, line) for
    each source text child -- used by the local rules, which must see genuine
    source whitespace, not the reconstruction's artifacts.
    """

    __slots__ = ("text", "base_line", "children")

    def __init__(self, text, base_line, children):
        self.text = text
        self.base_line = base_line
        self.children = children

    def line_of(self, offset):
        """Source line of a character offset within this block's text."""
        return self.base_line + self.text.count("\n", 0, offset)


def extract_blocks(markdown_text):
    """Split Markdown into prose Blocks, each tagged with its source line."""
    body, frontmatter_lines = _strip_frontmatter(markdown_text)
    skip = suppressed_lines(markdown_text)
    blocks = []
    for token in _MD.parse(body):
        if token.type != "inline":
            continue
        base_line = (token.map[0] if token.map else 0) + frontmatter_lines + 1
        parts = []
        children = []
        line = base_line
        for child in token.children or []:
            kind = child.type
            if kind == "text":
                if line not in skip:
                    parts.append(child.content)
                    children.append((child.content, line))
            elif kind in ("softbreak", "hardbreak"):
                parts.append("\n")
                line += 1
            elif kind == "code_inline":
                parts.append(" ")
        text = "".join(parts)
        if text.strip():
            blocks.append(Block(text, base_line, children))
    return blocks


def combine_blocks(blocks):
    """Join blocks into one prose string; return (text, spans).

    spans is a list of (start, end, block) so an offset into the combined text
    maps back to the owning block. Blocks are separated by a blank line so
    LanguageTool treats them as distinct sentences.
    """
    parts = []
    spans = []
    pos = 0
    for block in blocks:
        start = pos
        parts.append(block.text)
        pos += len(block.text)
        spans.append((start, pos, block))
        parts.append("\n\n")
        pos += 2
    return "".join(parts), spans


def line_for_offset(spans, offset):
    """Map a combined-text offset back to a source line via its block."""
    previous = None
    for start, end, block in spans:
        if start <= offset < end:
            return block.line_of(offset - start)
        if offset >= start:
            previous = block
    # Offset landed in a block-separator gap: use the preceding block's end.
    if previous is not None:
        return previous.line_of(len(previous.text))
    return None


# --------------------------------------------------------------------------
# Client-side local rules for house rules the free server does not cover.
# --------------------------------------------------------------------------
_EM_DASH_RE = re.compile("—")
_DOUBLE_SPACE_RE = re.compile(r"(?<=[.!?]) {2,}")

# British -> American spellings, for American-English bundles only. A curated
# whole-word MAP (not a suffix rule) keeps this deterministic and low false
# positive, so it is safe to BLOCK on: only listed words fire, so lookalikes
# that are valid American (advise, surprise, greyhound, cancellation) never do.
# LanguageTool's MORFOLOGIK spelling rule is advisory here (it also flags code
# identifiers) and its en-US dictionary accepts some British variants outright
# (catalogue), so neither severity tuning nor the server can enforce this. Keep
# every entry unambiguously British; skip contentious cases (theatre, judgement,
# dialogue, disc) that are also valid American. Comparison is case-insensitive.
# Whole-word tokens only: the negative lookarounds on \w restore the \b-on-\w
# boundary the old regex had, so a British fragment inside a snake_case or
# digit-suffixed identifier in prose (my_colour_var, colour2) is not flagged --
# critical because LOCAL_BRITISH_SPELLING blocks commits.
_WORD_RE = re.compile(r"(?<!\w)[A-Za-z]+(?!\w)")
_BRITISH_MAP_CACHE = None


def _load_british_map(path):
    """Load the British->American corpus. Raises if missing/empty: a blocking
    rule must never silently degrade to a no-op (no-silent-failure house rule)."""
    mapping = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        brit, _tab, amer = line.partition("\t")
        if amer:
            mapping[brit.strip().lower()] = amer.strip().lower()
    if not mapping:
        raise ValueError(f"British-spelling corpus is empty: {path}")
    return mapping


def _british_map():
    global _BRITISH_MAP_CACHE
    if _BRITISH_MAP_CACHE is None:
        # This corpus ships beside the code (fixed path off _DEFAULT_CONFIG_DIR),
        # so unlike the severity/dictionary bundles it is intentionally NOT
        # relocated by --config-dir.
        _BRITISH_MAP_CACHE = _load_british_map(
            _DEFAULT_CONFIG_DIR / "en-US" / "british-american.txt"
        )
    return _BRITISH_MAP_CACHE


def _match_case(source, target):
    """Cast an American suggestion to the source token's capitalization."""
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _is_american_english(language):
    """True for American-English bundles (and the None default), where British
    spellings should be flagged. A future en-GB bundle passes 'en-GB' and opts
    out; the bare-string test callers pass nothing and get the American default."""
    if language is None:
        return True
    return str(language).replace("_", "-").lower().startswith("en-us")

# i18n placeholder masking: {name}, {{name}}, %s, %d, %(name)s (plus flanking whitespace)
# -> a single space, so masking never manufactures a double space after a period.
_PLACEHOLDER_RE = re.compile(r"\s*(?:\{\{[^}]*\}\}|\{[^}]*\}|%\([^)]*\)[sd]|%[sd])\s*")


def _mask_placeholders(value):
    return _PLACEHOLDER_RE.sub(" ", value)


def _local_match(rule_id, offset, length, message, replacements):
    return {
        "rule": {"id": rule_id, "category": {"id": "LOCAL"}},
        "offset": offset,
        "length": length,
        "message": message,
        "replacements": [{"value": r} for r in replacements],
    }


def local_matches_text(text, language=None):
    """Scan a prose string for house rules with no free-server rule.

    ``language`` gates dialect-specific rules; it defaults to American English
    so the bare-string unit callers keep the British-spelling check.
    """
    matches = []
    if _is_american_english(language):
        mapping = _british_map()
        for m in _WORD_RE.finditer(text):
            american = mapping.get(m.group(0).lower())
            if american is not None:
                american = _match_case(m.group(0), american)
                matches.append(
                    _local_match(
                        "LOCAL_BRITISH_SPELLING",
                        m.start(),
                        m.end() - m.start(),
                        f"British spelling: prefer American '{american}'.",
                        [american],
                    )
                )
    for m in _EM_DASH_RE.finditer(text):
        matches.append(
            _local_match(
                "LOCAL_EM_DASH",
                m.start(),
                m.end() - m.start(),
                "Em-dash: prefer a dash, comma, or parentheses.",
                ["-"],
            )
        )
    for m in _DOUBLE_SPACE_RE.finditer(text):
        matches.append(
            _local_match(
                "LOCAL_DOUBLE_SPACE",
                m.start(),
                m.end() - m.start(),
                "Use a single space after sentence-ending punctuation.",
                [" "],
            )
        )
    return matches


_SPELLING_CATEGORY = "TYPOS"


def filter_allowlisted(matches, text, allowlist):
    """Drop spelling matches whose flagged word is in the allowlist."""
    kept = []
    for match in matches:
        category = match.get("rule", {}).get("category", {}).get("id")
        if category == _SPELLING_CATEGORY:
            start = match["offset"]
            word = text[start : start + match["length"]]
            if word.lower() in allowlist:
                continue
        kept.append(match)
    return kept


def partition_matches(matches, blocking_ids):
    """Split matches into (blocking, advisory) by rule ID or category ID."""
    blocking, advisory = [], []
    for match in matches:
        rule = match.get("rule", {})
        if rule.get("id") in blocking_ids or rule.get("category", {}).get("id") in blocking_ids:
            blocking.append(match)
        else:
            advisory.append(match)
    return blocking, advisory


# --------------------------------------------------------------------------
# Config, server I/O, and CLI.
# --------------------------------------------------------------------------
DEFAULT_SERVER = os.environ.get("PROSE_LINT_SERVER", "http://localhost:8081")
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_SERVER_SCRIPT = Path(__file__).resolve().parent / "prose-lint-server.sh"
_BACKENDS = ("container", "url")


def _backend():
    """The selected server backend: 'container' (default) or 'url'.

    Read at call time (not import) so the environment can change under tests
    and under a hook that sets it per-repo. An unknown value is an error, not
    a silent fallback.
    """
    # `or` (not a get() default) so an empty/whitespace value reads as unset,
    # matching the shell side's `[ -n "${PROSE_LINT_RUNTIME:-}" ]` test. A blank
    # var almost always means "not configured", not "configured to nothing".
    value = os.environ.get("PROSE_LINT_BACKEND", "").strip() or "container"
    if value not in _BACKENDS:
        raise ValueError(
            f"unknown PROSE_LINT_BACKEND {value!r} (valid: {', '.join(_BACKENDS)})"
        )
    return value


def _read_wordlist(path):
    if not path.exists():
        return set()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(line.lower())
    return words


def load_bundle(config_dir, bundle_name):
    """Load the severity.toml bundle for a bundle directory plus its merged allowlist."""
    config_dir = Path(config_dir)
    with open(config_dir / bundle_name / "severity.toml", "rb") as handle:
        bundle = tomllib.load(handle)
    if "language" not in bundle:
        raise KeyError(
            f"severity.toml for bundle '{bundle_name}' must set a 'language' key"
        )
    bundle.setdefault("level", "picky")
    for key in ("enabled_rules", "disabled_rules", "disabled_categories", "blocking"):
        bundle.setdefault(key, [])
    bundle["allowlist"] = _read_wordlist(config_dir / "dictionary.txt") | _read_wordlist(
        config_dir / bundle_name / "dictionary.txt"
    )
    return bundle


class ServerUnreachable(RuntimeError):
    pass


def server_is_up(server):
    """True if the LanguageTool server answers /v2/languages."""
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/v2/languages", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _start_server():
    """Start the container via the server script (blocks until ready)."""
    import subprocess

    subprocess.run([str(_SERVER_SCRIPT), "start"], check=False)


def ensure_server(server, start_fn=_start_server, is_up=server_is_up, backend=None):
    """Ensure the server is reachable, starting it once if it is not.

    Under the 'url' backend the server is preexisting/remote, so it is probed
    but never started.
    """
    if is_up(server):
        return True
    if (backend or _backend()) == "url":
        return False
    start_fn()
    return is_up(server)


def _unreachable_hint(backend):
    """What to tell the user when the server cannot be reached, per backend."""
    if backend == "url":
        return (
            "PROSE_LINT_BACKEND=url: start your own server, or unset it "
            "to let prose-check run a container."
        )
    return "Start it manually with: bin/prose-lint-server.sh start"


def _post_check(server, text, bundle):
    fields = {
        "text": text,
        "language": bundle["language"],
        "level": bundle["level"],
    }
    if bundle["enabled_rules"]:
        fields["enabledRules"] = ",".join(bundle["enabled_rules"])
    if bundle["disabled_rules"]:
        fields["disabledRules"] = ",".join(bundle["disabled_rules"])
    if bundle["disabled_categories"]:
        fields["disabledCategories"] = ",".join(bundle["disabled_categories"])
    data = urllib.parse.urlencode(fields).encode()
    url = server.rstrip("/") + "/v2/check"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
            return json.load(resp).get("matches", [])
    except (urllib.error.URLError, OSError) as exc:
        raise ServerUnreachable(str(exc)) from exc


def select_extractor(path, fmt):
    """Choose the extractor: explicit --format wins, else by file extension."""
    if fmt:
        return fmt
    return "i18n" if str(path).endswith(".json") else "markdown"


def check_blocks(blocks, server, bundle):
    """Run the shared pipeline over already-extracted blocks; return findings."""
    combined, spans = combine_blocks(blocks)
    findings = []
    if combined.strip():
        matches = _post_check(server, combined, bundle)
        matches = filter_allowlisted(matches, combined, bundle["allowlist"])
        for match in matches:
            enriched = dict(match)
            enriched["line"] = line_for_offset(spans, match.get("offset", 0))
            findings.append(enriched)
    # Local rules scan the RAW text children (genuine source whitespace), not
    # the reconstructed combined text, so inline-code removal cannot fabricate
    # findings (e.g. a spurious double space after a period).
    for block in blocks:
        for content, line in block.children:
            for match in local_matches_text(content, bundle.get("language")):
                enriched = dict(match)
                enriched["line"] = line
                findings.append(enriched)
    findings.sort(key=lambda f: (f["line"] or 0, f.get("offset", 0)))
    return findings


def _iter_i18n(data, prefix=""):
    """Yield (dotted_key, string_value) for every string leaf, recursing into
    nested objects and arrays so {"a": {"b": "x"}} yields ("a.b", "x") and
    {"a": ["x", "y"]} yields ("a.0", "x"), ("a.1", "y")."""
    if isinstance(data, dict):
        for key, value in data.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_i18n(value, dotted)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            dotted = f"{prefix}.{index}" if prefix else str(index)
            yield from _iter_i18n(value, dotted)
    elif isinstance(data, str):
        yield prefix, data
    # Non-string scalar leaves (numbers, bools, null) are not checkable copy.


def _line_for_key(json_text, dotted_key):
    """1-indexed line for a (possibly dotted) i18n key. A flat key containing
    dots ("greeting.hello") appears literally; a nested path ("section.title")
    is located by walking its quoted parts in order. Best-effort on duplicates,
    matching the flat behavior it replaces."""
    idx = json_text.find('"' + dotted_key + '"')
    if idx < 0:
        pos = 0
        for part in dotted_key.split("."):
            nxt = json_text.find('"' + part + '"', pos)
            if nxt < 0:
                break
            idx, pos = nxt, nxt + len(part) + 1
    if idx < 0:
        return 1
    return json_text.count("\n", 0, idx) + 1


def extract_i18n(json_text, ignore=None):
    """Extract checkable string values from an i18n locale JSON as Blocks.

    Nested objects are flattened to dotted keys, so both flat and nested locale
    shapes are checked; keys (flat or dotted) are never checked, only values."""
    ignore = ignore or (lambda key: False)
    data = json.loads(json_text)
    blocks = []
    for key, value in _iter_i18n(data):
        if ignore(key):
            continue
        text = _mask_placeholders(value)
        if not text.strip():
            continue
        line = _line_for_key(json_text, key)
        blocks.append(Block(text, line, [(text, line)]))
    return blocks


def key_ignorer(patterns):
    """Return a predicate: key matches any glob pattern or exact listed key."""
    patterns = list(patterns or [])

    def ignore(key):
        return any(fnmatch.fnmatchcase(key, p) for p in patterns)

    return ignore


def load_i18n_ignore(path):
    """Read [i18n] ignore_keys from a repo-local .prose-lint.toml (or [])."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return data.get("i18n", {}).get("ignore_keys", [])


def _resolve_i18n_ignore(explicit):
    """Ignore-key list by precedence: explicit --i18n-ignore path, else an
    auto-loaded ./.prose-lint.toml in the cwd if present, else none. A malformed
    file raises (tomllib) rather than silently yielding no ignores."""
    if explicit:
        return load_i18n_ignore(explicit)
    default = Path(".prose-lint.toml")
    return load_i18n_ignore(default) if default.exists() else []


def _format_finding(path, finding, severity):
    rule_id = finding.get("rule", {}).get("id", "?")
    message = finding.get("message", "")
    reps = finding.get("replacements") or []
    suggestion = f"  (suggest: {reps[0].get('value', '')!r})" if reps else ""
    return f"{path}:{finding.get('line')}: [{severity}] {rule_id} {message}{suggestion}"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Markdown-aware grammar/prose lint via a local LanguageTool server."
    )
    parser.add_argument("files", nargs="*", help="Markdown/text files to check")
    parser.add_argument("--lang", default="en-US")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--config-dir", default=str(_DEFAULT_CONFIG_DIR))
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        help="do not try to start the LanguageTool container if it is down",
    )
    parser.add_argument("--format", choices=["markdown", "i18n"], default=None)
    parser.add_argument("--profile", choices=["docs", "microcopy"], default=None)
    parser.add_argument(
        "--i18n-ignore",
        default=None,
        help="path to a .prose-lint.toml with [i18n] ignore_keys "
        "(default: ./.prose-lint.toml if present)",
    )
    args = parser.parse_args(argv)

    had_blocking = False

    # Validated unconditionally: a typo must not pass silently just because
    # --no-autostart or an empty file list happens to skip the probe below.
    try:
        backend = _backend()
    except ValueError as exc:
        print(f"prose-check: {exc}", file=sys.stderr)
        return 2

    if not args.no_autostart and args.files:
        if not ensure_server(args.server, backend=backend):
            print(
                f"prose-check: LanguageTool server unreachable at {args.server}.",
                file=sys.stderr,
            )
            print(_unreachable_hint(backend), file=sys.stderr)
            return 2

    profile = args.profile
    ignore_patterns = _resolve_i18n_ignore(args.i18n_ignore)
    ignore = key_ignorer(ignore_patterns)

    for path in args.files:
        fmt = select_extractor(path, args.format)
        bundle_name = args.lang
        if profile == "microcopy" or (profile is None and fmt == "i18n"):
            bundle_name = f"{args.lang}-microcopy"
        bundle = load_bundle(args.config_dir, bundle_name)
        blocking_ids = set(bundle["blocking"])
        try:
            source = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"prose-check: cannot read {path}: {exc}", file=sys.stderr)
            had_blocking = True
            continue
        try:
            if fmt == "i18n":
                blocks = extract_i18n(source, ignore)
                if not blocks and source.strip():
                    print(
                        f"prose-check: {path}: no checkable string values found",
                        file=sys.stderr,
                    )
                    had_blocking = True
                    continue
            else:
                blocks = extract_blocks(source)
            findings = check_blocks(blocks, args.server, bundle)
        except ServerUnreachable as exc:
            print(
                f"prose-check: LanguageTool server unreachable at {args.server}: {exc}",
                file=sys.stderr,
            )
            print(_unreachable_hint(backend), file=sys.stderr)
            return 2
        blocking, advisory = partition_matches(findings, blocking_ids)
        for finding in blocking:
            print(_format_finding(path, finding, "ERROR"))
        for finding in advisory:
            print(_format_finding(path, finding, "warn"))
        if blocking:
            had_blocking = True

    return 1 if had_blocking else 0


if __name__ == "__main__":
    sys.exit(main())
