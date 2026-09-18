#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export GAMEDEALS_DB=./demo.db
exec "$(command -v uv)" run --quiet game-deals-web
