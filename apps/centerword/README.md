# CenterWord

CenterWord is a small macOS speed-reading app.

It has two modes:

- A normal app window where you can paste long text, change WPM, start, pause, restart, and jump backward or forward by 5 seconds.
- A global hotkey mode where you highlight text in any app, press `Cmd+Option+S`, and CenterWord opens a stripped-down fast-reader view with one centered word at a time.

## Features

- Paste or type long-form text directly into the app
- Set a reading speed in words per minute
- Start, pause, resume, and restart playback
- Jump backward or forward by 5 seconds based on the current WPM
- Show elapsed time, remaining time, and word progress
- Capture selected text from other apps with `Cmd+Option+S`
- First-run onboarding for Accessibility setup
- Installs as a regular Dock app

## Requirements

- macOS 14+
- Accessibility permission for cross-app text capture

## Local Development

Run the app from source:

```bash
cd apps/centerword
swift run CenterWord
```

Run tests:

```bash
cd apps/centerword
swift test
```

## Install

Build, sign, install, and launch the app:

```bash
cd apps/centerword
./scripts/install-app.sh
```

That installs the app to:

```text
/Users/$USER/Applications/CenterWord.app
```

## First-Run Setup

CenterWord cannot auto-grant Accessibility permission because macOS requires the user to approve that manually.

On first launch, the app shows a setup section with buttons to:

- prompt for Accessibility access
- open the correct macOS Accessibility settings pane
- reveal the installed app in Finder
- re-check whether permission has been granted

Recommended first-run flow:

1. Open `CenterWord`.
2. Click `Prompt for Access`.
3. Enable `CenterWord` in `System Settings > Privacy & Security > Accessibility`.
4. Return to the app and click `Finish Setup`.

## Using The Main App

1. Paste text into the large editor.
2. Enter a WPM value.
3. Press `Start`.
4. Use `Pause`, `Restart`, `Back 5s`, or `Forward 5s` as needed.

The 5-second jump controls are time-based, not word-based. CenterWord converts 5 seconds at the current WPM into an approximate word jump.

## Using The Global Hotkey

1. Make sure `CenterWord` is running.
2. Highlight text in another app.
3. Press `Cmd+Option+S`.

CenterWord will:

- capture the selected text
- bring its reader window forward
- open the stripped-down one-word view
- start playback at the saved default hotkey WPM

## Project Layout

```text
apps/centerword/
├── AppBundle/Info.plist
├── Package.swift
├── README.md
├── Sources/CenterWordApp/
│   ├── CenterWordApplication.swift
│   ├── CenterWordHotKeyMonitor.swift
│   ├── LaunchAtLoginManager.swift
│   ├── SelectedTextCaptureService.swift
│   ├── TeleprompterModel.swift
│   ├── TeleprompterText.swift
│   └── TeleprompterView.swift
├── Tests/CenterWordTests/
│   └── TeleprompterModelTests.swift
└── scripts/
    ├── install-app.sh
    └── open-app.sh
```
