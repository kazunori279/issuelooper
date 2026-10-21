#!/usr/bin/env bash
# Deploy $DEPLOY_SHA for the owner to try: the supervisor runs this from the shared
# checkout whenever main moves ([deploy] command in config.toml).
#
#   http://localhost:8000/   scripted mode (spec/fixtures/script.json), no credentials
#   http://localhost:8001/   live mode against the real model, when the checkout has .env
#
# Each commit is extracted to its own directory under $DEPLOY_DIR; the previous one
# keeps serving until the new one is installed. Ports: DEPLOY_PORT, DEPLOY_LIVE_PORT.
set -euo pipefail
sha="${DEPLOY_SHA:?DEPLOY_SHA is not set}"
root="$(pwd)"
base="${DEPLOY_DIR:-$HOME/.local/share/issuelooper-deploy/$(basename "$root")}"
port="${DEPLOY_PORT:-8000}" live_port="${DEPLOY_LIVE_PORT:-8001}"
dest="$base/${sha:0:12}"

rm -rf "$dest.tmp" && mkdir -p "$dest.tmp"
git archive "$sha" | tar -x -C "$dest.tmp"
(cd "$dest.tmp" && uv sync --frozen -q)
rm -rf "$dest" && mv "$dest.tmp" "$dest"
[ -f "$root/.env" ] && ln -sf "$root/.env" "$dest/.env"

serve() {  # serve PORT NAME [ENV=VALUE ...]
  local p="$1" name="$2"; shift 2
  lsof -tiTCP:"$p" -sTCP:LISTEN | xargs kill 2>/dev/null || true
  sleep 1
  # The whole background job writes to the log: a shell left holding the supervisor's
  # output pipe would keep the deploy from ever finishing.
  (cd "$dest" && exec env SESSIONS_DB="$base/$name.db" "$@" nohup uv run concierge --port "$p") \
    > "$base/$name.log" 2>&1 < /dev/null &
  for _ in $(seq 60); do curl -sf "localhost:$p/" > /dev/null && return 0; sleep 1; done
  echo "the $name server did not answer on :$p; see $base/$name.log" >&2
  tail -20 "$base/$name.log" >&2
  return 1
}

serve "$port" scripted CONCIERGE_SCRIPT=spec/fixtures/script.json
if [ -f "$dest/.env" ]; then
  serve "$live_port" live
fi
echo "${sha:0:12} on :$port$([ -f "$dest/.env" ] && echo " and :$live_port (live)")"

# Keep the last three deployments.
ls -1dt "$base"/*/ 2>/dev/null | tail -n +4 | xargs -r rm -rf
