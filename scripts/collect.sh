#!/usr/bin/env bash
# Coleta diaria. Chamado pelo launchd; tambem roda a mao para testar.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "$(command -v uv)" run --quiet game-deals-collect
