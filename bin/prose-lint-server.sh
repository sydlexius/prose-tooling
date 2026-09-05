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
	# BOUNDED: an unbounded probe lets a wedged JVM (stalled NFS mount, a bad
	# _JAVA_OPTIONS) hang a pre-commit hook or CI step with no output at all.
	# PROSE_LINT_START_TIMEOUT does not cover this -- the probe runs first.
	local timeout_cmd=""
	command -v timeout >/dev/null 2>&1 && timeout_cmd="timeout"
	[ -z "${timeout_cmd}" ] && command -v gtimeout >/dev/null 2>&1 && timeout_cmd="gtimeout"
	if [ -n "${timeout_cmd}" ]; then
		"${timeout_cmd}" 10 java -version >/dev/null 2>&1 ||
			die "java on PATH is not a working runtime, or did not respond within 10s (on macOS /usr/bin/java is a stub until a JDK is installed)"
	else
		java -version >/dev/null 2>&1 ||
			die "java on PATH is not a working runtime (on macOS /usr/bin/java is a stub until a JDK is installed)"
	fi
}

# Every probe is BOUNDED. An unbounded curl against a JVM that has accepted the
# connection but is mid-shutdown hangs a pre-commit hook or CI step with no
# output at all -- the same defect this file already bounds require_java for.
server_responds() {
	curl -fsS --connect-timeout 2 --max-time 5 "${URL}/v2/languages" >/dev/null 2>&1
}

state_dir() {
	local dir="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
	echo "${dir%/}"
}

pid_file() { echo "$(state_dir)/prose-lint-lt-${HOST_PORT}.pid"; }
log_file() { echo "$(state_dir)/prose-lint-lt-${HOST_PORT}.log"; }

# Echoes a sane pid from a pid file, or nothing. Trailing newlines and stray
# whitespace are TRIMMED rather than rejected: `echo $! > file` writes one, and
# treating our own output as unreadable made stop() abandon a live server.
# 0 and 1 are refused outright -- `kill -0 0` SUCCEEDS (it means "my whole
# process group"), so accepting 0 would leave only `ps` between us and
# signaling the caller's shell.
read_pid() {
	local raw
	# `read` is a BUILTIN, unlike `cat`: reading our own pid file must not
	# depend on an external binary being resolvable. When it did, a PATH that
	# lacked `cat` made every pid look unreadable, so stop() deleted the file
	# and abandoned a live server -- silently, and with a message blaming the
	# file. Reading state is not the place to take a dependency.
	# `|| true`, NOT `|| raw=""`: `read` returns non-zero at EOF-without-newline
	# while still assigning, so clearing on failure would DISCARD a good pid and
	# send stop down the "no usable pid" path -- deleting the file and
	# abandoning a live server, the exact bug this function exists to prevent.
	[ -f "$1" ] || return 0
	IFS= read -r raw <"$1" 2>/dev/null || true
	raw="${raw//[[:space:]]/}"
	case "${raw}" in
	'' | *[!0-9]*) return 0 ;;
	esac
	[ "${raw}" -gt 1 ] 2>/dev/null || return 0
	echo "${raw}"
}

# True when the pid is alive AND its command line still looks like our server.
# `ps -ww` because a `java -cp` command line is long, and a truncated one would
# fail to match, making stop() refuse to act on its own process.
# Does any process in the group still look like our server? Used when the
# recorded leader has exited but the group may still hold the real process.
group_looks_ours() {
	local pgid="$1" line
	command -v ps >/dev/null 2>&1 || return 1
	# Parsed with shell builtins, no awk. Adding an external dependency here
	# would repeat the very mistake this function exists to undo -- `cat` and
	# `grep` already made stop abandon a live server when they were missing.
	local first rest
	[ -n "${pgid}" ] || return 1
	while IFS= read -r line; do
		# `ps` RIGHT-PADS the pgid column, so most lines start with spaces --
		# and `${line%%[[:space:]]*}` on a space-leading line strips the WHOLE
		# line, yielding "". Without this trim the comparison only ever matched
		# a pgid exactly as wide as the column (5 digits here), so the function
		# silently never fired for a 4-digit pgid: stop then called a live
		# server "recycled", removed the pid file and abandoned it.
		line="${line#"${line%%[![:space:]]*}"}"
		first="${line%%[[:space:]]*}"
		[ "${first}" = "${pgid}" ] || continue
		rest="${line#"${first}"}"
		case "${rest}" in
		*[Ll]anguage[Tt]ool* | *HTTPServer*) return 0 ;;
		esac
	done <<EOF
$(ps -ww -eo pgid=,command= 2>/dev/null)
EOF
	return 1
}

pid_is_ours() {
	local pid="$1" cmdline
	kill -0 "${pid}" 2>/dev/null || return 1
	# `ps` is external and could be unavailable (a stripped container, a
	# restricted PATH). Distinguish "ps says this is not ours" from "ps could
	# not run": the first is a refusal, the second must not silently become
	# one, or a missing binary makes the tool abandon a live server.
	if ! command -v ps >/dev/null 2>&1; then
		# Fatal only for a caller about to SIGNAL. For a caller merely asking
		# "is one already running?", refusing to launch is the worse answer --
		# it turns a missing tool into a hard failure on a path that worked.
		[ "${2:-signal}" = "signal" ] &&
			die "cannot verify pid ${pid} -- 'ps' is not available; refusing to signal blindly"
		return 1
	fi
	cmdline="$(ps -ww -o command= -p "${pid}" 2>/dev/null || true)"
	case "${cmdline}" in
	*[Ll]anguage[Tt]ool* | *HTTPServer*) return 0 ;;
	*) return 1 ;;
	esac
}

# Launch $@ detached, in its own process group, with output to $1.
# Sets SPAWNED_PID to the group leader.
#
# BOTH properties are required and they come from different mechanisms:
#   - `nohup` makes the child immune to the SIGHUP the shell sends its jobs on
#     exit. Without it the server dies with the script that started it.
#   - a NEW PROCESS GROUP is what lets stop() take down the whole tree. The
#     recorded pid is the wrapper for a launcher that runs java without exec,
#     so signaling it alone orphans the JVM.
# `set -m` gives the group but NOT hup-immunity (measured: the child dies when
# the parent exits), so dropping nohup for it silently breaks detachment.
spawn_detached() {
	local logf="$1"
	shift
	# PROBE setsid, do not just look for it: a broken symlink satisfies
	# `command -v` and then fails to execute -- the same "exists but does not
	# work" trap as the macOS java stub, and it silently loses the launch.
	if command -v setsid >/dev/null 2>&1 && setsid true >/dev/null 2>&1; then
		setsid nohup "$@" >"${logf}" 2>&1 &
	else
		# macOS has no setsid. Job control gives the background job its own
		# process group; nohup supplies the hup-immunity it does not.
		set -m
		{ nohup "$@" >"${logf}" 2>&1 & }
		set +m
	fi
	SPAWNED_PID=$!
}

binary_start() {
	local resolved kind value pidf logf timeout waited runtime_dir existing
	if server_responds; then
		echo "already running at ${URL}"
		return 0
	fi
	resolved="$(resolve_launcher)" || exit 1
	kind="${resolved%%:*}"
	value="${resolved#*:}"
	pidf="$(pid_file)"
	logf="$(log_file)"
	timeout="${PROSE_LINT_START_TIMEOUT:-30}"
	runtime_dir="$(state_dir)"
	[ -d "${runtime_dir}" ] && [ -w "${runtime_dir}" ] ||
		die "state directory '${runtime_dir}' is not writable; set XDG_RUNTIME_DIR or TMPDIR to a writable directory"

	# Refuse to launch a second server over a live one. The health probe above
	# misses the STARTUP WINDOW -- a real JVM takes 10-30s to bind -- which is
	# exactly when a pre-commit hook and a manual start collide.
	if [ -f "${pidf}" ]; then
		existing="$(read_pid "${pidf}")"
		if [ -n "${existing}" ] && pid_is_ours "${existing}" probe; then
			die "a LanguageTool process (pid ${existing}) is already starting or running; stop it first"
		fi
	fi

	# `setsid` (or a setsid-free fallback) puts the child in its OWN PROCESS
	# GROUP. This is load-bearing, not tidiness: LanguageTool's own launcher is
	# a shell wrapper that runs java WITHOUT exec, so the recorded pid is the
	# WRAPPER and killing it alone orphans the JVM, which keeps holding the
	# port. Recording a group lets stop take down the whole tree.
	#
	# Output goes to the log, never inherited: an inherited stdout keeps a CI
	# step's pipe open and hangs the job until the runner's timeout.
	# No --public flag, so the server binds loopback only.
	if [ "${kind}" = "jar" ]; then
		require_java
		spawn_detached "${logf}" java -Xmx1g -cp "${value}" \
			org.languagetool.server.HTTPServer --port "${HOST_PORT}"
	else
		spawn_detached "${logf}" "${value}" --port "${HOST_PORT}"
	fi
	if ! echo "${SPAWNED_PID}" >"${pidf}"; then
		# The child is already running. Dying here without reaping it orphans a
		# server that nothing records -- unfindable by the double-start guard,
		# because the file it would consult was never written.
		kill -- -"${SPAWNED_PID}" 2>/dev/null || kill "${SPAWNED_PID}" 2>/dev/null || true
		die "cannot write pid file '${pidf}'; the server just started was stopped again"
	fi

	echo "starting LanguageTool at ${URL} ..."
	waited=0
	while [ "${waited}" -lt "${timeout}" ]; do
		if server_responds; then
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
	if [ ! -r "${pidf}" ]; then
		# Do NOT delete: the file exists but cannot be read (permissions, a
		# transient fault). Deleting it would abandon a possibly-live server
		# and lose the only handle we have on it.
		die "pid file '${pidf}' exists but is not readable; cannot safely stop"
	fi
	pid="$(read_pid "${pidf}")"
	if [ -z "${pid}" ]; then
		rm -f "${pidf}"
		echo "not running (pid file held no usable pid; removed)"
		return 0
	fi
	# Liveness is a property of the GROUP, not the leader. A launcher that
	# forks and exits leaves the recorded leader dead while its child lives on
	# in the same group -- reparented to init, but the pgid is retained. Gating
	# on the leader here declared that live server "stale", removed the pid
	# file, and returned success without ever trying to stop it.
	if ! kill -0 -- -"${pid}" 2>/dev/null && ! kill -0 "${pid}" 2>/dev/null; then
		rm -f "${pidf}"
		echo "not running (stale pid file removed)"
		return 0
	fi
	if ! pid_is_ours "${pid}" && ! group_looks_ours "${pid}"; then
		# NEVER signal a process we cannot identify as ours: PIDs are recycled,
		# and killing an unrelated process is the failure mode to avoid.
		rm -f "${pidf}"
		echo "not running (pid ${pid} was recycled by another process; not signaled)"
		return 0
	fi

	# Signal the process GROUP, not just the recorded pid. LanguageTool's
	# launcher is a shell wrapper that runs java WITHOUT exec, so the recorded
	# pid is the wrapper: killing it alone leaves the JVM holding the port,
	# whereupon `start` reports "already running" and the user has a server
	# this tool can never stop.
	signal_tree() { kill -"$1" -- -"${pid}" 2>/dev/null || kill -"$1" "${pid}" 2>/dev/null || true; }

	# Observe the GROUP, not the leader. The leader here is a shell wrapper
	# that dies instantly on TERM while the JVM it launched runs shutdown
	# hooks -- so polling `kill -0 $pid` breaks on the first iteration, SIGKILL
	# is never sent, and the JVM survives holding the port. Once the group is
	# the unit of SIGNALLING it must also be the unit of OBSERVATION.
	group_alive() { kill -0 -- -"${pid}" 2>/dev/null || kill -0 "${pid}" 2>/dev/null; }

	signal_tree TERM
	waited=0
	while [ "${waited}" -lt 10 ] && group_alive; do
		sleep 1
		waited=$((waited + 1))
	done
	if group_alive; then
		signal_tree KILL
		sleep 1
	fi

	# VERIFY BEFORE REMOVING THE HANDLE. A `die` here with the pid file already
	# gone leaves no way to identify or stop what is still running -- strictly
	# worse than not having tried.
	if server_responds; then
		die "signaled pid ${pid} but ${URL} is still serving; the pid file is kept at ${pidf} so you can investigate (something else may hold this port)"
	fi
	rm -f "${pidf}"
	echo "stopped"
}

binary_status() {
	local pidf pid
	pidf="$(pid_file)"
	pid=""
	[ -f "${pidf}" ] && pid="$(read_pid "${pidf}")"
	# Group-aware, like stop: a launcher that forks and exits leaves the
	# recorded leader dead while the real server lives on in the same group.
	# Checking only the leader reported a live server as "not running" -- and
	# status is the documented recovery path after a failed stop.
	if [ -n "${pid}" ] && { pid_is_ours "${pid}" probe || group_looks_ours "${pid}"; }; then
		echo "running at ${URL} (pid ${pid})"
		server_responds &&
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
		if server_responds; then
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
		server_responds && echo "health: OK" || echo "health: NOT READY"
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
