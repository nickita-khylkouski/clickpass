#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="CenterWord"
APP_LABEL="com.nickita.centerword"
BUILD_CONFIGURATION="release"
OPEN_AFTER_INSTALL=1
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PATH="$LAUNCH_AGENT_DIR/${APP_LABEL}.plist"

resolve_signing_identity() {
  if [[ -n "${CENTERWORD_SIGN_IDENTITY:-}" ]]; then
    printf '%s\n' "$CENTERWORD_SIGN_IDENTITY"
    return 0
  fi

  local personal_identity
  personal_identity="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '\"' '/Developer ID Application: Nickita Khy \\(HSRWKMA3SL\\)/ { print $2; exit }')"
  if [[ -n "$personal_identity" ]]; then
    printf '%s\n' "$personal_identity"
    return 0
  fi

  local any_developer_id
  any_developer_id="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '\"' '/Developer ID Application:/ { print $2; exit }')"
  if [[ -n "$any_developer_id" ]]; then
    printf '%s\n' "$any_developer_id"
  fi
}

for arg in "$@"; do
  case "$arg" in
    debug|release)
      BUILD_CONFIGURATION="$arg"
      ;;
    --no-open)
      OPEN_AFTER_INSTALL=0
      ;;
    *)
      echo "Usage: ./scripts/install-app.sh [debug|release] [--no-open]" >&2
      exit 1
      ;;
  esac
done

ROOT_BUILD_DIR="$ROOT_DIR/.build/arm64-apple-macosx/${BUILD_CONFIGURATION}"
APP_DIR="$HOME/Applications/${APP_NAME}.app"
EXECUTABLE_PATH="$ROOT_BUILD_DIR/${APP_NAME}"

cd "$ROOT_DIR"
swift build -c "$BUILD_CONFIGURATION" --product "$APP_NAME"

mkdir -p "$HOME/Applications"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cp "$EXECUTABLE_PATH" "$APP_DIR/Contents/MacOS/${APP_NAME}"
chmod +x "$APP_DIR/Contents/MacOS/${APP_NAME}"
cp "$ROOT_DIR/AppBundle/Info.plist" "$APP_DIR/Contents/Info.plist"

mkdir -p "$LAUNCH_AGENT_DIR"
cat > "$LAUNCH_AGENT_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${APP_LABEL}</string>
  <key>AssociatedBundleIdentifiers</key>
  <array>
    <string>${APP_LABEL}</string>
  </array>
  <key>LimitLoadToSessionType</key>
  <array>
    <string>Aqua</string>
  </array>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>ProgramArguments</key>
  <array>
    <string>${APP_DIR}/Contents/MacOS/${APP_NAME}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
</dict>
</plist>
EOF

SIGNING_IDENTITY="$(resolve_signing_identity || true)"
if [[ -n "$SIGNING_IDENTITY" ]]; then
  codesign --force --deep --options runtime --timestamp --sign "$SIGNING_IDENTITY" "$APP_DIR"
  SIGNING_SUMMARY="Developer ID signed with: $SIGNING_IDENTITY"
else
  codesign --force --deep --sign - "$APP_DIR"
  SIGNING_SUMMARY="Ad hoc signed"
fi

if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
  pkill -x "$APP_NAME" >/dev/null 2>&1 || true
  sleep 0.2
fi

launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PATH" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/$(id -u)/${APP_LABEL}" >/dev/null 2>&1 || true

if [[ "$OPEN_AFTER_INSTALL" -eq 1 ]]; then
  osascript -e 'tell application "CenterWord" to activate' >/dev/null 2>&1 \
    || open "$APP_DIR" >/dev/null 2>&1 \
    || true
fi

cat <<EOF
Installed ${APP_DIR}

What to expect:
- Type or paste text
- Enter a WPM number
- Press Start

Signing:
- ${SIGNING_SUMMARY}
EOF
