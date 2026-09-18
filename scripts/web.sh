#!/usr/bin/env bash
# Sobe o dashboard. GAMEDEALS_DB=./demo.db para ver com dados de demonstracao.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "$(command -v uv)" run --quiet game-deals-web
