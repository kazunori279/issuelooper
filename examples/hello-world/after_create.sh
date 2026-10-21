#!/usr/bin/env bash
# Run by examples/create.sh in the new repository: the environment and a first check.
set -euo pipefail
export UV_FROZEN=1   # use uv.lock as it is, whatever index this machine is set to
uv sync -q
make verify
