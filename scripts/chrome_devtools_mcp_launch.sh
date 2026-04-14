#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/daytona
export SHELL=/bin/bash
export PATH="$HOME/tools/web-audit/node-runtime/bin:$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export npm_config_cache="$HOME/.npm"
export MCP_BIN="$HOME/tools/web-audit/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp"

mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$npm_config_cache"

if [ ! -x "$MCP_BIN" ]; then
  echo "chrome-devtools-mcp binary missing at $MCP_BIN" >&2
  exit 1
fi

exec "$MCP_BIN" "$@"
