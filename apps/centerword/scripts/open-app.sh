#!/bin/zsh
set -euo pipefail

APP_DIR="$HOME/Applications/CenterWord.app"

if [[ ! -d "$APP_DIR" ]]; then
  echo "CenterWord.app not found in ~/Applications." >&2
  exit 1
fi

open "$APP_DIR"
