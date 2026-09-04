#!/usr/bin/env bash
# install.sh -- scaffold prose-lint config into a target repo and print the hook.
#
# Writes a starter .prose-lint.toml (backing up any existing one), optionally
# copies a starter config dir with --with-config, and prints the git-hook
# wiring. It never edits .git/ or an existing hook -- paste the printed snippet
# as part of the target repo's own change.
#
# Usage: install.sh [/path/to/target/repo] [--with-config]
set -euo pipefail

TOOLING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="${TOOLING_DIR}/.venv/bin/python ${TOOLING_DIR}/bin/prose_check.py"
EXAMPLES="${TOOLING_DIR}/examples"

TARGET="."
WITH_CONFIG=0
for arg in "$@"; do
	case "$arg" in
		--with-config) WITH_CONFIG=1 ;;
		-*) echo "unknown option: $arg" >&2; exit 2 ;;
		*) TARGET="$arg" ;;
	esac
done

[ -d "${TARGET}" ] || { echo "target is not a directory: ${TARGET}" >&2; exit 2; }
# -e (not -d): a worktree or submodule checkout has .git as a FILE (a gitdir pointer).
[ -e "${TARGET}/.git" ] || { echo "target is not a git repo: ${TARGET}" >&2; exit 2; }
TARGET_ABS="$(cd "${TARGET}" && pwd)"

echo "prose-tooling dir : ${TOOLING_DIR}"
echo "target repo       : ${TARGET_ABS}"
echo

# 1) Scaffold .prose-lint.toml (never clobber).
DEST="${TARGET_ABS}/.prose-lint.toml"
if [ -f "${DEST}" ] && cmp -s "${DEST}" "${EXAMPLES}/.prose-lint.toml"; then
	echo ".prose-lint.toml already matches the starter; left unchanged."
else
	if [ -f "${DEST}" ]; then
		BAK="${DEST}.bak"
		if [ -e "${BAK}" ]; then
			n=1
			while [ -e "${DEST}.bak.${n}" ]; do n=$((n + 1)); done
			BAK="${DEST}.bak.${n}"
		fi
		cp "${DEST}" "${BAK}"
		echo "backed up existing .prose-lint.toml -> $(basename "${BAK}")"
	fi
	cp "${EXAMPLES}/.prose-lint.toml" "${DEST}"
	echo "wrote ${DEST}"
fi

# 2) Optionally copy a starter config dir (skip pre-existing files).
CONFIG_FLAG=""
if [ "${WITH_CONFIG}" -eq 1 ]; then
	DESTCFG="${TARGET_ABS}/.prose-lint-config"
	mkdir -p "${DESTCFG}"
	# -n: never overwrite an existing file (preserve adopter edits on re-run).
	# GNU coreutils >= 9.2 exits non-zero when -n skips a file; that skip is the
	# intended outcome here, not an error, so tolerate it rather than let `set -e`
	# abort the run before the hook snippet prints.
	cp -Rn "${EXAMPLES}/config/." "${DESTCFG}/" || true
	echo "copied starter config -> ${DESTCFG} (existing files skipped)"
	CONFIG_FLAG=" --config-dir ${DESTCFG}"
fi
echo

# 3) Detect hook mechanism and print the wiring (single source: examples/hooks).
if [ -f "${TARGET_ABS}/.githooks/pre-commit" ]; then
	echo "Detected .githooks/pre-commit (core.hooksPath style)."
elif [ -f "${TARGET_ABS}/.pre-commit-config.yaml" ]; then
	echo "Detected .pre-commit-config.yaml (pre-commit framework)."
else
	echo "No known hook mechanism detected; choose one below."
fi
echo
echo "The client auto-starts the container on demand if it is stopped."
echo "To manage it manually: ${TOOLING_DIR}/bin/prose-lint-server.sh start"
echo "The container runtime is auto-detected (docker, podman, nerdctl);"
echo "set PROSE_LINT_RUNTIME to pin one."
echo "Already run a shared LanguageTool server? Set PROSE_LINT_BACKEND=url and"
echo "PROSE_LINT_SERVER=<its url>; prose-check will then never start a container."
echo
echo "2a) For a .githooks/pre-commit (bash) repo, add this section:"
echo
sed "s|__CLIENT__|${CLIENT}${CONFIG_FLAG}|g" "${EXAMPLES}/hooks/githooks-pre-commit.sh"
echo
echo "2b) For a pre-commit-framework repo, add a repo: local hook:"
echo
sed "s|__CLIENT__|${CLIENT}${CONFIG_FLAG}|g" "${EXAMPLES}/hooks/pre-commit-config.yaml"
echo
echo "The client exits 1 on a blocking finding, 2 if the server is unreachable."
