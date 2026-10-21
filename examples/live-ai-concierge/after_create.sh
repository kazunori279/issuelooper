#!/usr/bin/env bash
# Run by examples/create.sh in the new repository: the environment, the browser for the
# end-to-end tests and the UX runs, a first check, and a .env to fill in.
set -euo pipefail
export UV_FROZEN=1   # use uv.lock as it is, whatever index this machine is set to
uv sync -q
uv run playwright install chromium
make verify
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Wrote .env from .env.example: set GOOGLE_CLOUD_PROJECT (and GOOGLE_MAPS_API_KEY if you have one)."
fi
