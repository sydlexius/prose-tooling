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
	# A BLANK value reads as unset, matching the client's _backend(), which
	# strips before testing. `[ -z ]` alone catches only the truly empty string,
	# so "   " would die here while the client had already accepted it as
	# container and shelled out to us -- the failure surfacing at the wrong
	# layer, with a message contradicting the half that just validated it.
	case "${value}" in
	*[![:space:]]*) ;;
	*) value="container" ;;
	esac
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

LAUNCHERS="languagetool-server languagetool-http-server"

resolve_launcher() {
	local candidate
	if [ -n "${PROSE_LINT_JAR:-}" ]; then
		[ -f "${PROSE_LINT_JAR}" ] && [ -r "${PROSE_LINT_JAR}" ] ||
			die "PROSE_LINT_JAR '${PROSE_LINT_JAR}' is not a readable file"
		echo "jar:${PROSE_LINT_JAR}"
		return 0
	fi
	for candidate in ${LAUNCHERS}; do
		if command -v "${candidate}" >/dev/null 2>&1; then
			echo "launcher:${candidate}"
			return 0
		fi
	done
	die "no LanguageTool found: set PROSE_LINT_JAR=/path/to/languagetool-server.jar, or install LanguageTool so one of (${LAUNCHERS}) is on PATH"
}

require_java() {
	# NOT `command -v java`: macOS ships /usr/bin/java as a STUB that exists and
	# is executable but errors until a JDK is installed. Probe for real.
	command -v java >/dev/null 2>&1 ||
		die "java not found on PATH; the binary backend needs a JVM"
	java -version >/dev/null 2>&1 ||
		die "java on PATH is not a working runtime (on macOS /usr/bin/java is a stub until a JDK is installed)"
}

pid_file() {
	local dir="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
	echo "${dir%/}/prose-lint-lt-${HOST_PORT}.pid"
}

log_file() {
	local dir="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
	echo "${dir%/}/prose-lint-lt-${HOST_PORT}.log"
}

# True when the pid is alive AND its command line still looks like our server.
# `ps -ww` because a `java -cp` command line is long, and a truncated one would
# fail to match, making stop() refuse to act on its own process.
pid_is_ours() {
	local pid="$1"
	kill -0 "${pid}" 2>/dev/null || return 1
	ps -ww -o command= -p "${pid}" 2>/dev/null |
		grep -qiE 'languagetool|HTTPServer'
}

binary_start() {
	local resolved kind value pidf logf timeout waited
	if curl -fsS "${URL}/v2/languages" >/dev/null 2>&1; then
		echo "already running at ${URL}"
		return 0
	fi
	resolved="$(resolve_launcher)" || exit 1
	kind="${resolved%%:*}"
	value="${resolved#*:}"
	pidf="$(pid_file)"
	logf="$(log_file)"
	timeout="${PROSE_LINT_START_TIMEOUT:-30}"

	# Detached, with output redirected to the log: an inherited stdout would
	# keep a CI step's pipe open and hang the job until the runner's timeout.
	# No --public flag, so the server binds loopback only.
	if [ "${kind}" = "jar" ]; then
		require_java
		nohup java -Xmx1g -cp "${value}" org.languagetool.server.HTTPServer \
			--port "${HOST_PORT}" >"${logf}" 2>&1 &
	else
		nohup "${value}" --port "${HOST_PORT}" >"${logf}" 2>&1 &
	fi
	echo $! >"${pidf}"

	echo "starting LanguageTool at ${URL} ..."
	waited=0
	while [ "${waited}" -lt "${timeout}" ]; do
		if curl -fsS "${URL}/v2/languages" >/dev/null 2>&1; then
			echo "ready at ${URL}"
			return 0
		fi
		sleep 1
		waited=$((waited + 1))
	done
	die "server did not become ready within ${timeout}s (see: ${logf})"
}

binary_stop() {
	local pidf pid waited
	pidf="$(pid_file)"
	if [ ! -f "${pidf}" ]; then
		echo "not running"
		return 0
	fi
	pid="$(cat "${pidf}" 2>/dev/null || true)"
	case "${pid}" in
	'' | *[!0-9]*)
		rm -f "${pidf}"
		echo "not running (unreadable pid file removed)"
		return 0
		;;
	esac
	if ! kill -0 "${pid}" 2>/dev/null; then
		rm -f "${pidf}"
		echo "not running (stale pid file removed)"
		return 0
	fi
	if ! pid_is_ours "${pid}"; then
		# NEVER signal a process we cannot identify as ours: PIDs are recycled,
		# and killing an unrelated process is the failure mode to avoid.
		rm -f "${pidf}"
		echo "not running (pid ${pid} was recycled by another process; not signaled)"
		return 0
	fi
	kill "${pid}" 2>/dev/null || die "failed to signal pid ${pid}"
	waited=0
	while [ "${waited}" -lt 10 ]; do
		kill -0 "${pid}" 2>/dev/null || break
		sleep 1
		waited=$((waited + 1))
	done
	if kill -0 "${pid}" 2>/dev/null; then
		kill -9 "${pid}" 2>/dev/null || true
		echo "stopped (SIGKILL after ${waited}s)"
	else
		echo "stopped"
	fi
	rm -f "${pidf}"
}

binary_status() {
	local pidf pid
	pidf="$(pid_file)"
	pid=""
	[ -f "${pidf}" ] && pid="$(cat "${pidf}" 2>/dev/null || true)"
	if [ -n "${pid}" ] && pid_is_ours "${pid}"; then
		echo "running at ${URL} (pid ${pid})"
		curl -fsS "${URL}/v2/languages" >/dev/null 2>&1 &&
			echo "health: OK" || echo "health: NOT READY"
	else
		echo "not running"
		exit 1
	fi
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
launcher) resolve_launcher ;;
*) die "usage: $0 {start|stop|status|restart|runtime|launcher}" ;;
esac
