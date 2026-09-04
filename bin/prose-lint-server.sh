#!/usr/bin/env bash
# prose-lint-server.sh -- manage the local LanguageTool server.
#
# The grammar linter checks against a LOCAL LanguageTool server only; repo
# content is never sent to the public API. This starts a stock
# erikvl87/languagetool container bound to localhost with a restart policy so
# it survives reboots.
#
# PROSE_LINT_BACKEND selects how the server runs: `container` (the default) or
# `binary`. Under `url` the server is somebody else's and this script refuses.
# For the container backend the runtime is docker, podman, or nerdctl: set
# PROSE_LINT_RUNTIME to pick one, otherwise the first found on PATH wins.
#
# Usage: prose-lint-server.sh {start|stop|status|restart|runtime}
set -euo pipefail

IMAGE="erikvl87/languagetool:latest"
NAME="prose-lint-lt"
HOST_PORT="${PROSE_LINT_PORT:-8081}"
CONTAINER_PORT="8010" # the image listens on 8010 internally
URL="http://localhost:${HOST_PORT}"

die() {
	echo "prose-lint-server: $*" >&2
	exit 1
}
RUNTIMES="docker podman nerdctl"

resolve_runtime() {
	local candidate
	if [ -n "${PROSE_LINT_RUNTIME:-}" ]; then
		case " ${RUNTIMES} " in
		*" ${PROSE_LINT_RUNTIME} "*) ;;
		*) die "unknown PROSE_LINT_RUNTIME '${PROSE_LINT_RUNTIME}' (valid: ${RUNTIMES})" ;;
		esac
		command -v "${PROSE_LINT_RUNTIME}" >/dev/null 2>&1 ||
			die "PROSE_LINT_RUNTIME '${PROSE_LINT_RUNTIME}' not found on PATH"
		echo "${PROSE_LINT_RUNTIME}"
		return 0
	fi
	for candidate in ${RUNTIMES}; do
		if command -v "${candidate}" >/dev/null 2>&1; then
			echo "${candidate}"
			return 0
		fi
	done
	die "no container runtime found (${RUNTIMES}); set PROSE_LINT_RUNTIME or use PROSE_LINT_BACKEND=url"
}

# Resolved ON FIRST USE, not at load: the binary backend needs no container
# runtime, and resolving here would kill a JVM-only machine before it reached
# the backend it asked for.
RUNTIME=""
require_runtime() {
	[ -n "${RUNTIME}" ] && return 0
	RUNTIME="$(resolve_runtime)" || exit 1
}

BACKENDS="container url binary"

resolve_backend() {
	local value="${PROSE_LINT_BACKEND:-}"
	# An empty value reads as unset, matching the PROSE_LINT_RUNTIME test above
	# and the client's _backend().
	[ -z "${value}" ] && value="container"
	# Exact match per word, NOT a `case " ${BACKENDS} " in *" ${value} "*`
	# substring test: that accepts any contiguous slice, so "url binary" would
	# validate and then skip the url refusal below, which is the one guard that
	# must not be escapable.
	local candidate
	for candidate in ${BACKENDS}; do
		if [ "${value}" = "${candidate}" ]; then
			echo "${value}"
			return 0
		fi
	done
	die "unknown PROSE_LINT_BACKEND '${value}' (valid: ${BACKENDS})"
}

container_running() {
	require_runtime
	[ -n "$("${RUNTIME}" ps -q -f "name=^${NAME}$")" ]
}
container_exists() {
	require_runtime
	[ -n "$("${RUNTIME}" ps -aq -f "name=^${NAME}$")" ]
}

container_start() {
	if container_running; then
		echo "already running at ${URL}"
		return 0
	fi
	if container_exists; then
		"${RUNTIME}" start "${NAME}" >/dev/null
	else
		# --restart unless-stopped: the runtime brings it back on boot.
		# Java_Xmx caps heap; no n-gram data in v1 (large download).
		"${RUNTIME}" run -d \
			--name "${NAME}" \
			--restart unless-stopped \
			-p "127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}" \
			-e "Java_Xmx=1g" \
			"${IMAGE}" >/dev/null
	fi
	echo "starting ${NAME} at ${URL} ..."
	for _ in $(seq 1 30); do
		if curl -fsS "${URL}/v2/languages" >/dev/null 2>&1; then
			echo "ready at ${URL}"
			return 0
		fi
		sleep 1
	done
	die "server did not become ready within 30s (see: ${RUNTIME} logs ${NAME})"
}

container_stop() {
	# Not an `a && b || c` chain: that reports "not running" when the container
	# IS running and the stop merely FAILED (a live case under rootless podman),
	# leaving the user misinformed and the exit code 0.
	if ! container_running; then
		echo "not running"
		return 0
	fi
	"${RUNTIME}" stop "${NAME}" >/dev/null || die "failed to stop ${NAME}"
	echo "stopped"
}

container_status() {
	if container_running; then
		echo "running at ${URL}"
		curl -fsS "${URL}/v2/languages" >/dev/null 2>&1 && echo "health: OK" || echo "health: NOT READY"
	else
		echo "not running"
		exit 1
	fi
}

BACKEND="$(resolve_backend)" || exit 1

# The url backend has no lifecycle: the server belongs to whoever runs it.
if [ "${BACKEND}" = "url" ]; then
	die "PROSE_LINT_BACKEND=url: this server is not ours to manage; start and stop it yourself"
fi

# Dispatch is indirect ("${BACKEND}_start"). A missing function exits 127 with a
# raw "command not found" naming an internal symbol, which tells the user
# nothing actionable. Resolve it to a real message instead.
dispatch() {
	local verb="$1" handler="${BACKEND}_$1"
	declare -F "${handler}" >/dev/null 2>&1 ||
		die "backend '${BACKEND}' does not implement '${verb}'"
	"${handler}"
}

case "${1:-}" in
start) dispatch start ;;
stop) dispatch stop ;;
restart)
	dispatch stop
	dispatch start
	;;
status) dispatch status ;;
runtime)
	# Asking for the runtime IS a request to resolve one, so this stays eager.
	require_runtime
	echo "${RUNTIME}"
	;;
*) die "usage: $0 {start|stop|status|restart|runtime}" ;;
esac
