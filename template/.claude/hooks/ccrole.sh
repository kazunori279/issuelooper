#!/usr/bin/env bash
# Give an agent session a role, from outside the session.
#
# The role comes from the environment rather than from the model: role_guard.py
# reads CC_ROLE to decide what a session may label, merge or close, and how its
# comments are signed.
#
#   ccrole.sh worker -- -p "..."       launch a session as a worker
#   ccrole.sh assign <session-id> ROLE give a role to a session already running
#   ccrole.sh list                     show every assigned session
#   ccrole.sh cli                      show which CLI and model each role launches
#   ccrole.sh revoke <session-id>      take a role away
#
# Roles: worker (one issue to a pull request), merger (lands a pull request that
# conflicts with main), tester (live checks and the UX run), monitor (triage and the
# owner's decisions), reviewer (the review gate's model), aide (an ad-hoc session
# that files and comments). ccpool.py starts worker, merger and tester sessions; the
# monitor is broker.py's long-lived session.
#
# The CLI (agy or claude): CCROLE_<ROLE>_CLI, else CCROLE_CLI, else agy.
# The model: CCROLE_MODEL_<ROLE>, else CCROLE_MODEL, else the CLI's default.
# Both can be set in .issuelooper/config.toml ([agents] and [agents.<role>]).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# No .claude/hooks/__pycache__/ in the shared checkout: the merge gate needs it clean.
export PYTHONDONTWRITEBYTECODE=1
[ -f "$HERE/config.py" ] && eval "$(python3 "$HERE/config.py" env 2>/dev/null || true)"
# This repository's scratch directory (config.py: one per repository, under /tmp).
[ -n "${ISSUELOOPER_TMP:-}" ] && mkdir -p "$ISSUELOOPER_TMP"
# Nothing in a session waits for a person at a terminal. A command that opens an editor
# (`git rebase --continue`, `git merge`, `git commit` without -m) or asks for a username
# never returns; agy moves it to the background and the session sits idle until its
# timeout (many merger sessions timed out this way). An editor is `true` (the default
# message stands), a prompt fails at once, and output is never paged. An exported
# value wins.
export GIT_EDITOR="${GIT_EDITOR:-true}" GIT_SEQUENCE_EDITOR="${GIT_SEQUENCE_EDITOR:-true}" \
  GIT_MERGE_AUTOEDIT="${GIT_MERGE_AUTOEDIT:-no}" GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}" \
  GCM_INTERACTIVE="${GCM_INTERACTIVE:-never}" GH_PROMPT_DISABLED="${GH_PROMPT_DISABLED:-1}" \
  PAGER="${PAGER:-cat}" GIT_PAGER="${GIT_PAGER:-cat}" GH_PAGER="${GH_PAGER:-cat}"

ROLE_DIR="$HOME/.claude/cc-role"
VALID="monitor merger tester reviewer aide worker"

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

cli_for() {
  local per="CCROLE_$(upper "$1")_CLI"
  printf '%s' "${!per:-${CCROLE_CLI:-agy}}"
}

model_for() {
  local per="CCROLE_MODEL_$(upper "$1")"
  printf '%s' "${!per:-${CCROLE_MODEL:-}}"
}

die() { printf '%s\n' "$*" >&2; exit 1; }

check_role() {
  for r in $VALID; do [ "$1" = "$r" ] && return 0; done
  die "unknown role '$1' — one of: $VALID"
}

# Point git at the repository's .githooks, so the hooks also run for commits and
# pushes made from a session's worktree; not where the owner installed with
# --keep-hooks (issuelooper.keepHooks), whose own hooks call these.
for dir in "$(cd "$HERE/../.." 2>/dev/null && pwd)" "$(git rev-parse --show-toplevel 2>/dev/null || true)"; do
  if [ -n "$dir" ] && [ -d "$dir/.githooks" ] \
    && [ "$(git -C "$dir" config --get issuelooper.keepHooks 2>/dev/null || true)" != "true" ]; then
    if [ "$(git -C "$dir" config --get core.hooksPath 2>/dev/null || true)" != ".githooks" ]; then
      git -C "$dir" config core.hooksPath .githooks
    fi
  fi
done

case "${1:-}" in
  assign)
    [ $# -eq 3 ] || die "usage: ccrole.sh assign <session-id> <role>"
    check_role "$3"
    mkdir -p "$ROLE_DIR"
    printf '%s\n' "$3" > "$ROLE_DIR/$2"
    echo "session $2 is now the $3"
    ;;
  revoke)
    [ $# -eq 2 ] || die "usage: ccrole.sh revoke <session-id>"
    rm -f "$ROLE_DIR/$2"
    echo "session $2 has no role; its GitHub writes will be refused"
    ;;
  list)
    mkdir -p "$ROLE_DIR"
    found=0
    for f in "$ROLE_DIR"/*; do
      [ -e "$f" ] || continue
      found=1
      printf '%-40s %s\n' "$(basename "$f")" "$(cat "$f")"
    done
    [ "$found" = 1 ] || echo "no sessions have been assigned a role"
    ;;
  cli)
    for r in $VALID; do printf '%-10s %-8s %s\n' "$r" "$(cli_for "$r")" "$(model_for "$r")"; done
    ;;
  ""|-h|--help)
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    ;;
  *)
    check_role "$1"
    role="$1"; shift
    [ "${1:-}" = "--" ] && shift
    cli="$(cli_for "$role")"
    case "$cli" in
      agy|claude) ;;
      *) die "unknown CLI '$cli' for the $role: agy or claude" ;;
    esac
    command -v "$cli" >/dev/null 2>&1 || die \
      "'$cli' is not on PATH. Set CCROLE_$(upper "$role")_CLI or CCROLE_CLI to pick another."
    # Which CLI is running a role decides how its output should be read, and the
    # session is about to replace this shell, so say it once, on stderr.
    model="$(model_for "$role")"
    printf '%s session on %s%s\n' "$role" "$cli" "${model:+ ($model)}" >&2
    # --add-dir attaches the repository root: agy loads the workspace hooks
    # (.agents/hooks.json) from it in print mode, and a claude session started from
    # a worktree can still read the shared checkout's .claude/.
    root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    set -- --add-dir "$root" "$@"
    [ -n "$model" ] && set -- --model "$model" "$@"
    # No per-call approval prompts: role_guard is the gate, and it is a hook.
    skip=1
    for a in "$@"; do [ "$a" = "--dangerously-skip-permissions" ] && skip=0; done
    [ "$skip" = 1 ] && set -- --dangerously-skip-permissions "$@"
    # Exported, so the guard sees it and so do any subagents this session spawns.
    CC_ROLE="$role" exec "$cli" "$@"
    ;;
esac
