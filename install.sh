#!/usr/bin/env bash
# Install issuelooper into a project's repository, or upgrade an earlier install.
#
#   ./install.sh <repo-dir>              install, create the labels, commit (no push)
#   ./install.sh <repo-dir> --upgrade    replace the framework files, keep .issuelooper/
#   options: --no-labels    skip creating the GitHub labels
#            --no-commit    leave the changes staged instead of committing
#            --force        overwrite files at the framework's paths that are not an
#                           earlier install's (listed and refused otherwise)
#            --chain-hooks  the repository already runs git hooks: point core.hooksPath
#                           at .githooks and have it run those hooks after its own
#            --keep-hooks   the repository already runs git hooks: leave core.hooksPath
#                           alone (run .githooks/pre-commit and pre-push from your hooks)
#
# What it writes into <repo-dir>, and the only files it stages:
#   .claude/hooks/                 the supervisor, guard, sweep, review gate and launcher
#   .claude/skills/issuelooper-*   what each role's agent reads
#   .claude/settings.json          the role_guard hook for Claude Code (merged)
#   .agents/                       the same hook, skills and rules for agy (merged)
#   .githooks/                     protect .claude/ and main (only files that are absent)
#   .issuelooper/                  config.toml, project.md, review_rules.md (only if absent)
#   docs/agent-framework.md, a section in CLAUDE.md, .gitignore entries
# Other files under those directories, tracked or not, are neither changed nor staged.
set -euo pipefail

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TPL="$HERE/template"
upgrade=0 labels=1 commit=1 force=0 hooks_mode="" repo=""
for a in "$@"; do
  case "$a" in
    --upgrade) upgrade=1 ;;
    --no-labels) labels=0 ;;
    --no-commit) commit=0 ;;
    --force) force=1 ;;
    --chain-hooks) hooks_mode=chain ;;
    --keep-hooks) hooks_mode=keep ;;
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; exit 0 ;;
    -*) die "unknown option $a" ;;
    *) [ -z "$repo" ] || die "one repository at a time"; repo="$a" ;;
  esac
done
[ -n "$repo" ] || die "usage: install.sh <repo-dir> [--upgrade] [--no-labels] [--no-commit] [--force] [--chain-hooks|--keep-hooks]"
REPO="$(cd "$repo" && pwd -P)" || die "no such directory: $repo"

# --- prerequisites ---------------------------------------------------------------
git -C "$REPO" rev-parse --show-toplevel >/dev/null 2>&1 || die "$REPO is not a git repository"
[ "$(git -C "$REPO" rev-parse --show-toplevel)" = "$REPO" ] || die "$REPO is not the repository root"
git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || die "$REPO has uncommitted changes"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 3.11 or later is required"
command -v gh >/dev/null || die "the GitHub CLI (gh) is required"
if [ -e "$REPO/.claude/hooks/ccpool.py" ] && [ "$upgrade" = 0 ]; then
  die "$REPO already has issuelooper; run with --upgrade to replace the framework files"
fi

# --- the git hooks the repository already runs ---------------------------------------
# issuelooper's hooks need core.hooksPath=.githooks. Switching it silently would stop
# whatever ran before (Husky's hooks, a secret scanner in .git/hooks), so an existing
# setup is reported and needs --chain-hooks or --keep-hooks.
cur_hooks="$(git -C "$REPO" config --get core.hooksPath || true)"
prev_dir="${cur_hooks:-$(git -C "$REPO" rev-parse --git-path hooks)}"  # relative to the repository root, or absolute
prev_hooks=()
if [ "$cur_hooks" != ".githooks" ] && [ -n "$prev_dir" ]; then
  case "$prev_dir" in /*) abs_prev="$prev_dir" ;; *) abs_prev="$REPO/$prev_dir" ;; esac
  if [ -d "$abs_prev" ]; then
    for h in "$abs_prev"/*; do
      name="$(basename "$h")"
      case "$name" in *.sample|_*|.*) continue ;; esac
      [ -f "$h" ] && [ -x "$h" ] && prev_hooks+=("$name")
    done
  fi
fi
if [ "${#prev_hooks[@]}" -gt 0 ] && [ -z "$hooks_mode" ]; then
  die "$REPO already runs git hooks from $prev_dir: ${prev_hooks[*]}.
  issuelooper's hooks (keep agent sessions from committing .claude/ and from force-pushing
  main) need core.hooksPath=.githooks, which would stop those from running. Rerun with
    --chain-hooks  core.hooksPath=.githooks, which runs the hooks above after its own, or
    --keep-hooks   core.hooksPath stays as it is; call .githooks/pre-commit and
                   .githooks/pre-push from your own pre-commit and pre-push hooks."
fi

# --- what this run writes, and what it would overwrite ---------------------------------
# managed: the paths written (relative to REPO), the only ones staged at the end.
# A path that already holds something else is a conflict: on a first install any
# different file, on an upgrade an untracked one (an earlier install's are tracked).
managed=() conflicts=() unstaged_note=()
tracked() { git -C "$REPO" ls-files --error-unmatch -- "$1" >/dev/null 2>&1; }
claim_path() {  # <template file or ""> <path in REPO>
  local src="$1" rel="$2" dst="$REPO/$2"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if [ -z "$src" ] || ! cmp -s "$src" "$dst"; then
      if [ "$upgrade" = 0 ] || ! tracked "$rel"; then conflicts+=("$rel"); fi
    fi
  fi
  managed+=("$rel")
}
hook_files=()
for f in "$TPL"/.claude/hooks/*; do
  # The tests stay in the issuelooper repository.
  case "$(basename "$f")" in __pycache__|.ruff_cache|*_test.py) continue ;; esac
  hook_files+=("$f")
  claim_path "$f" ".claude/hooks/$(basename "$f")"
done
for d in "$TPL"/.claude/skills/issuelooper-*; do
  name="$(basename "$d")"
  while IFS= read -r f; do
    claim_path "$f" ".claude/skills/$name/${f#"$d"/}"
  done < <(find "$d" -type f)
  # The directory is replaced whole: a file in it the template does not have is lost.
  if [ -d "$REPO/.claude/skills/$name" ]; then
    while IFS= read -r f; do
      rel=".claude/skills/$name/${f#"$REPO/.claude/skills/$name"/}"
      [ -e "$d/${f#"$REPO/.claude/skills/$name"/}" ] && continue
      if [ "$upgrade" = 0 ] || ! tracked "$rel"; then conflicts+=("$rel"); fi
      managed+=("$rel")  # an earlier install's file the template dropped: staged as deleted
    done < <(find "$REPO/.claude/skills/$name" -type f)
  fi
  link=".agents/skills/$name"
  if [ -e "$REPO/$link" ] || [ -L "$REPO/$link" ]; then
    if [ "$(readlink "$REPO/$link" || true)" != "../../.claude/skills/$name" ] \
      && { [ "$upgrade" = 0 ] || ! tracked "$link"; }; then conflicts+=("$link"); fi
  fi
  managed+=("$link")
done
claim_path "$TPL/.agents/rules/issuelooper.md" ".agents/rules/issuelooper.md"
claim_path "$TPL/docs/agent-framework.md" "docs/agent-framework.md"
if [ "${#conflicts[@]}" -gt 0 ] && [ "$force" = 0 ]; then
  die "these paths already hold files that are not issuelooper's; nothing was written:
$(printf '    %s\n' "${conflicts[@]}")
  Move them away, or rerun with --force to overwrite them."
fi

# --- framework files (replaced on upgrade) -----------------------------------------
mkdir -p "$REPO/.claude/hooks" "$REPO/.claude/skills" "$REPO/.agents/rules" "$REPO/.agents/skills" "$REPO/docs"
for f in "${hook_files[@]}"; do
  cp -a "$f" "$REPO/.claude/hooks/"
done
for d in "$TPL"/.claude/skills/issuelooper-*; do
  rm -rf "$REPO/.claude/skills/$(basename "$d")"
  cp -a "$d" "$REPO/.claude/skills/"
  ln -sfn "../../.claude/skills/$(basename "$d")" "$REPO/.agents/skills/$(basename "$d")"
done
cp "$TPL/.agents/rules/issuelooper.md" "$REPO/.agents/rules/issuelooper.md"
cp "$TPL/docs/agent-framework.md" "$REPO/docs/agent-framework.md"

# Hook registrations: merged into whatever the project already has.
python3 - "$TPL" "$REPO" <<'PY'
import json, pathlib, sys
tpl, repo = map(pathlib.Path, sys.argv[1:3])

def merge_hooks(dst, src):
    """Add each hook entry of src to dst unless an entry with the same command exists."""
    for event, entries in src.items():
        have = dst.setdefault(event, [])
        commands = {h.get("command") for e in have for h in e.get("hooks", [])}
        for e in entries:
            if not any(h.get("command") in commands for h in e.get("hooks", [])):
                have.append(e)

def load(p):
    return json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}

# Claude Code
p = repo / ".claude/settings.json"
cur = load(p)
merge_hooks(cur.setdefault("hooks", {}), load(tpl / ".claude/settings.json")["hooks"])
p.write_text(json.dumps(cur, indent=2) + "\n")

# agy: hooks.json holds named hook sets; skills.json lists skill directories.
p = repo / ".agents/hooks.json"
cur = load(p)
for name, body in load(tpl / ".agents/hooks.json").items():
    merge_hooks(cur.setdefault(name, {}), body)
p.write_text(json.dumps(cur, indent=2) + "\n")

p = repo / ".agents/skills.json"
cur = load(p)
entries = cur.setdefault("entries", [])
for e in load(tpl / ".agents/skills.json")["entries"]:
    if e not in entries:
        entries.append(e)
p.write_text(json.dumps(cur, indent=2) + "\n")
PY
managed+=(".claude/settings.json" ".agents/hooks.json" ".agents/skills.json")

# --- project files (never overwritten) ---------------------------------------------
mkdir -p "$REPO/.issuelooper"
for f in config.toml project.md review_rules.md; do
  if [ ! -e "$REPO/.issuelooper/$f" ]; then
    cp "$TPL/.issuelooper/$f" "$REPO/.issuelooper/$f"
    managed+=(".issuelooper/$f")
  fi
done
mkdir -p "$REPO/.githooks"
for f in pre-commit pre-push _chain; do
  if [ ! -e "$REPO/.githooks/$f" ]; then
    cp -a "$TPL/.githooks/$f" "$REPO/.githooks/$f"
    managed+=(".githooks/$f")
  fi
done
# ccrole.sh points core.hooksPath at .githooks for every session unless this says not to.
if [ "$hooks_mode" = keep ]; then
  git -C "$REPO" config issuelooper.keepHooks true
else
  git -C "$REPO" config --unset issuelooper.keepHooks || true
fi
if [ "$hooks_mode" = keep ]; then
  say "core.hooksPath left at ${cur_hooks:-the default (.git/hooks)}: run .githooks/pre-commit and .githooks/pre-push from your own hooks"
elif [ "$cur_hooks" != ".githooks" ]; then
  if [ "$hooks_mode" = chain ] && [ "${#prev_hooks[@]}" -gt 0 ]; then
    git -C "$REPO" config issuelooper.chainedHooksPath "$prev_dir"
    # A hook .githooks has no file for would stop running: give it one that only chains.
    for h in "${prev_hooks[@]}"; do
      if [ ! -e "$REPO/.githooks/$h" ]; then
        printf '#!/bin/sh\n# Runs the repository'"'"'s own %s hook (install.sh --chain-hooks).\nexec "$(dirname "$0")/_chain" %s "$@"\n' "$h" "$h" > "$REPO/.githooks/$h"
        chmod 755 "$REPO/.githooks/$h"
        managed+=(".githooks/$h")
      fi
    done
    say "core.hooksPath is now .githooks; it runs the hooks in $prev_dir after its own (${prev_hooks[*]})"
  fi
  git -C "$REPO" config core.hooksPath .githooks
fi

# .gitignore and CLAUDE.md get lines appended. One that was untracked before holds
# the project's own text, so it is left unstaged for the owner to look at.
for f in .gitignore CLAUDE.md; do
  if [ ! -e "$REPO/$f" ] || tracked "$f"; then managed+=("$f"); else unstaged_note+=("$f"); fi
done
touch "$REPO/.gitignore"
for line in ".issuelooper/run/" ".ux/" "__pycache__/"; do
  grep -qxF "$line" "$REPO/.gitignore" || printf '%s\n' "$line" >> "$REPO/.gitignore"
done

marker="<!-- issuelooper -->"
if ! grep -qF "$marker" "$REPO/CLAUDE.md" 2>/dev/null; then
  [ -s "$REPO/CLAUDE.md" ] && printf '\n' >> "$REPO/CLAUDE.md"
  { printf '%s\n' "$marker"; tail -n +3 "$TPL/.agents/rules/issuelooper.md"; } >> "$REPO/CLAUDE.md"
fi

# --- labels ------------------------------------------------------------------------
if [ "$labels" = 1 ]; then
  ( cd "$REPO" && gh repo view --json nameWithOwner -q .nameWithOwner >/dev/null ) \
    || die "gh cannot see this repository on GitHub (rerun with --no-labels to skip labels)"
  while IFS='|' read -r name color desc; do
    [ -n "$name" ] || continue
    ( cd "$REPO" && gh label create "$name" --color "$color" --description "$desc" --force >/dev/null )
  done <<'EOF'
stage:triage|fbca04|Claimable by a worker
stage:ready|0e8a16|Claimable by a worker
stage:doing|1d76db|A worker holds the claim
stage:pr|5319e7|Its pull request is open and green
stage:live|c5def5|Merged; the live check is owed
stage:owner|d93f0b|Waits for the owner's decision
needs:live|bfdadc|Acceptance needs the live environment
P0|b60205|Broken now, or the minimum viable product needs it
P1|d93f0b|Next
P2|fef2c0|Default priority
hold:monitor|e4e669|The monitor stopped this change from landing
on hold|ededed|The owner parked it
owner-request|0052cc|The owner asked for it
ux:proposed|c2e0c6|A proposal waiting for the owner's decision
type:bug|d73a4a|Something is broken
type:feature-request|a2eeef|New behavior
type:documentation|0075ca|Documentation only
type:spike|cfd3d7|Investigation
type:chore|ededed|Maintenance
EOF
  say "labels created or updated"
fi

# --- commit ------------------------------------------------------------------------
# Exactly the paths written above (-A: a skill file the template dropped is staged as
# deleted); nothing else under .claude/, .agents/, .githooks/ or .issuelooper/.
stage=()
for f in "${managed[@]}"; do
  if [ -e "$REPO/$f" ] || [ -L "$REPO/$f" ] || tracked "$f"; then stage+=("$f"); fi
done
git -C "$REPO" add -A -- "${stage[@]}"
for f in "${unstaged_note[@]+"${unstaged_note[@]}"}"; do
  say "$f was untracked; issuelooper's lines were appended to it but it is not staged: review and commit it yourself"
done
if [ "$commit" = 1 ]; then
  msg="Install issuelooper"; [ "$upgrade" = 1 ] && msg="Upgrade issuelooper"
  if git -C "$REPO" diff --cached --quiet; then
    say "nothing to commit: the framework files are already up to date"
  else
    ISSUELOOPER_CONFIG_OK=1 git -C "$REPO" commit -q -m "$msg"
    say "committed: $(git -C "$REPO" log -1 --format='%h %s') (not pushed)"
  fi
fi

cat <<EOF

Next:
  1. Edit .issuelooper/config.toml (agent CLI, sweep command, deploy, live, UX)
     and .issuelooper/project.md (how to set up, test and run the app).
  2. Commit with ISSUELOOPER_CONFIG_OK=1 (the pre-commit hook protects these files) and push.
  3. File issues, then start the loop:   .claude/hooks/issuelooper start
EOF
