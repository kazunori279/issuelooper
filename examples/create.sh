#!/usr/bin/env bash
# Create a GitHub repository from a sample, with issuelooper installed, configured for
# the sample, and the sample's seed backlog filed as issues.
#
#   examples/create.sh <sample> <owner>/<name> [--dir DIR] [--public]
#
#   <sample>   a directory under examples/ with app/, issuelooper/ and backlog.md:
#              hello-world, live-ai-concierge
#   --dir      where to put the local clone (default: ./<name>)
#   --public   create a public repository (default: private)
#
# Steps: copy the sample's app/ into DIR and commit it; create the GitHub repository
# and push; run install.sh (framework files, labels); copy the sample's issuelooper/
# files into .issuelooper/ and push; run the sample's after_create.sh, if any; file
# backlog.md with seed_issues.py.
set -euo pipefail

die() { printf 'create.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
sample="" slug="" dir="" visibility="--private"
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) dir="${2:?--dir needs a path}"; shift ;;
    --public) visibility="--public" ;;
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) if [ -z "$sample" ]; then sample="$1"; elif [ -z "$slug" ]; then slug="$1"; else die "too many arguments"; fi ;;
  esac
  shift
done
[ -n "$slug" ] || die "usage: create.sh <sample> <owner>/<name> [--dir DIR] [--public]"
S="$HERE/$sample"
[ -d "$S/app" ] && [ -d "$S/issuelooper" ] && [ -f "$S/backlog.md" ] || die "no such sample: $sample"
case "$slug" in */*) ;; *) die "name the repository as <owner>/<name>" ;; esac
dir="${dir:-$PWD/${slug#*/}}"
[ -e "$dir" ] && die "$dir already exists"

for c in git gh uv python3; do command -v "$c" >/dev/null || die "$c is required"; done
gh auth status >/dev/null 2>&1 || die "gh is not signed in (gh auth login)"
gh repo view "$slug" >/dev/null 2>&1 && die "$slug already exists on GitHub"

say "app -> $dir"
mkdir -p "$dir"
cp -a "$S/app/." "$dir/"
git -C "$dir" init -q -b main
git -C "$dir" add -A
git -C "$dir" commit -q -m "Initial app"

say "GitHub repository $slug"
gh repo create "$slug" "$visibility" --disable-wiki --source "$dir" --remote origin --push

say "install issuelooper"
"$ROOT/install.sh" "$dir" | sed '/^Next:/,$d'   # this script does the next steps

say "configure issuelooper for the $sample sample"
cp "$S"/issuelooper/* "$dir/.issuelooper/"
git -C "$dir" add .issuelooper
ISSUELOOPER_CONFIG_OK=1 git -C "$dir" commit -q -m "Configure issuelooper for the $sample sample"
git -C "$dir" push -q origin main

if [ -x "$S/after_create.sh" ]; then
  say "prepare the app"
  (cd "$dir" && "$S/after_create.sh")
fi

say "seed backlog"
python3 "$HERE/seed_issues.py" "$S/backlog.md" --repo "$slug"

cat <<EOF

Ready: https://github.com/$slug ($dir)
Next, as examples/$sample/README.md describes:
  cd $dir
  herdr --session ${slug#*/}          # then, in a herdr pane:
  .claude/hooks/issuelooper start
EOF
