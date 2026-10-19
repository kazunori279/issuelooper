#!/usr/bin/env bash
# Install issuelooper into a project's repository, or upgrade an earlier install.
#
#   ./install.sh <repo-dir>              install, create the labels, commit (no push)
#   ./install.sh <repo-dir> --upgrade    replace the framework files, keep .issuelooper/
#   options: --no-labels  skip creating the GitHub labels
#            --no-commit  leave the changes staged instead of committing
#
# What it writes into <repo-dir>:
#   .claude/hooks/                 the supervisor, guard, sweep, review gate and launcher
#   .claude/skills/issuelooper-*   what each role's agent reads
#   .claude/settings.json          the role_guard hook for Claude Code (merged)
#   .agents/                       the same hook, skills and rules for agy (merged)
#   .githooks/                     protect .claude/ and main (only if absent)
#   .issuelooper/                  config.toml, project.md, review_rules.md (only if absent)
#   docs/agent-framework.md, a section in CLAUDE.md, .gitignore entries
set -euo pipefail

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TPL="$HERE/template"
upgrade=0 labels=1 commit=1 repo=""
for a in "$@"; do
  case "$a" in
    --upgrade) upgrade=1 ;;
    --no-labels) labels=0 ;;
    --no-commit) commit=0 ;;
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; exit 0 ;;
    -*) die "unknown option $a" ;;
    *) [ -z "$repo" ] || die "one repository at a time"; repo="$a" ;;
  esac
done
[ -n "$repo" ] || die "usage: install.sh <repo-dir> [--upgrade] [--no-labels] [--no-commit]"
REPO="$(cd "$repo" && pwd)" || die "no such directory: $repo"

# --- prerequisites ---------------------------------------------------------------
git -C "$REPO" rev-parse --show-toplevel >/dev/null 2>&1 || die "$REPO is not a git repository"
[ "$(git -C "$REPO" rev-parse --show-toplevel)" = "$REPO" ] || die "$REPO is not the repository root"
git -C "$REPO" diff --quiet && git -C "$REPO" diff --cached --quiet || die "$REPO has uncommitted changes"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 3.11 or later is required"
command -v gh >/dev/null || die "the GitHub CLI (gh) is required"
if [ -e "$REPO/.claude/hooks/ccpool.py" ] && [ "$upgrade" = 0 ]; then
  die "$REPO already has issuelooper; run with --upgrade to replace the framework files"
fi

# --- framework files (replaced on upgrade) -----------------------------------------
mkdir -p "$REPO/.claude/hooks" "$REPO/.claude/skills" "$REPO/.agents/rules" "$REPO/.agents/skills" "$REPO/docs"
for f in "$TPL"/.claude/hooks/*; do
  # The tests stay in the issuelooper repository.
  case "$(basename "$f")" in __pycache__|.ruff_cache|*_test.py) continue ;; esac
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

# --- project files (never overwritten) ---------------------------------------------
mkdir -p "$REPO/.issuelooper"
for f in config.toml project.md review_rules.md; do
  [ -e "$REPO/.issuelooper/$f" ] || cp "$TPL/.issuelooper/$f" "$REPO/.issuelooper/$f"
done
if [ -d "$REPO/.githooks" ]; then
  for f in pre-commit pre-push; do
    [ -e "$REPO/.githooks/$f" ] || cp "$TPL/.githooks/$f" "$REPO/.githooks/$f"
  done
else
  cp -a "$TPL/.githooks" "$REPO/.githooks"
fi
git -C "$REPO" config core.hooksPath .githooks

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
git -C "$REPO" add .claude .agents .githooks .issuelooper docs/agent-framework.md CLAUDE.md .gitignore
if [ "$commit" = 1 ]; then
  msg="Install issuelooper"; [ "$upgrade" = 1 ] && msg="Upgrade issuelooper"
  ISSUELOOPER_CONFIG_OK=1 git -C "$REPO" commit -q -m "$msg"
  say "committed: $(git -C "$REPO" log -1 --format='%h %s') (not pushed)"
fi

cat <<EOF

Next:
  1. Edit .issuelooper/config.toml (agent CLI, sweep command, deploy, live, UX)
     and .issuelooper/project.md (how to set up, test and run the app).
  2. Commit with ISSUELOOPER_CONFIG_OK=1 (the pre-commit hook protects these files) and push.
  3. File issues, then start the loop:   .claude/hooks/issuelooper start
EOF
