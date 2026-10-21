#!/usr/bin/env bash
# Run by examples/create.sh in the new repository: the environment and a first check.
set -euo pipefail
uv sync -q
make verify
