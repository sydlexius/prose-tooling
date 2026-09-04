#!/usr/bin/env bash
# prose-lint-server.sh -- manage the local LanguageTool container.
#
# The grammar linter checks against a LOCAL LanguageTool server only; repo
# content is never sent to the public API. This starts a stock
# erikvl87/languagetool container bound to localhost with a restart policy so
# it survives reboots.
#
# The container runtime is docker, podman, or nerdctl: set PROSE_LINT_RUNTIME
# to pick one, otherwise the first found on PATH wins (in that order).
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

RUNTIME="$(resolve_runtime)" || exit 1

running() { [ -n "$("${RUNTIME}" ps -q -f "name=^${NAME}$")" ]; }
exists() { [ -n "$("${RUNTIME}" ps -aq -f "name=^${NAME}$")" ]; }

start() {
	if running; then
		echo "already running at ${URL}"
		return 0
	fi
	if exists; then
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

stop() {
	# Not an `a && b || c` chain: that reports "not running" when the container
	# IS running and the stop merely FAILED (a live case under rootless podman),
	# leaving the user misinformed and the exit code 0.
	if ! running; then
		echo "not running"
		return 0
	fi
	"${RUNTIME}" stop "${NAME}" >/dev/null || die "failed to stop ${NAME}"
	echo "stopped"
}

status() {
	if running; then
		echo "running at ${URL}"
		curl -fsS "${URL}/v2/languages" >/dev/null 2>&1 && echo "health: OK" || echo "health: NOT READY"
	else
		echo "not running"
		exit 1
	fi
}

case "${1:-}" in
start) start ;;
stop) stop ;;
restart)
	stop
	start
	;;
status) status ;;
runtime) echo "${RUNTIME}" ;;
*) die "usage: $0 {start|stop|status|restart|runtime}" ;;
esac
