import SwiftUI

final class CenterWordApplicationDelegate: NSObject, NSApplicationDelegate {
    var session: CenterWordSession?

    private let hotKeyMonitor = CenterWordHotKeyMonitor()
    private let selectedTextCaptureService = SelectedTextCaptureService()
    private var lastHotKeyPressAt = Date.distantPast
    private var allowsTermination = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        if terminateIfDuplicateInstanceExists() {
            return
        }

        NSApplication.shared.setActivationPolicy(.regular)
        LaunchAtLoginManager.ensureEnabled()

        hotKeyMonitor.onPress = { [weak self] in
            Task { @MainActor in
                self?.handleHotKeyPress()
            }
        }
        hotKeyMonitor.install()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard allowsTermination else {
            for window in sender.windows {
                window.orderOut(nil)
            }
            sender.hide(nil)
            return .terminateCancel
        }

        return .terminateNow
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        session?.openMainWindow?()
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotKeyMonitor.uninstall()
    }

    @MainActor
    private func handleHotKeyPress() {
        let now = Date()
        guard now.timeIntervalSince(lastHotKeyPressAt) > 0.5 else {
            return
        }
        lastHotKeyPressAt = now

        let result = selectedTextCaptureService.captureSelectedText(promptIfNeeded: true)

        switch result {
        case let .success(text):
            session?.presentCapturedText(text, wordsPerMinute: TeleprompterLogic.storedDefaultWordsPerMinute())
        case let .failure(error):
            session?.presentError(error.localizedDescription)
        }
    }

    @MainActor
    private func terminateIfDuplicateInstanceExists() -> Bool {
        guard let bundleIdentifier = Bundle.main.bundleIdentifier else {
            return false
        }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        let duplicates = NSRunningApplication
            .runningApplications(withBundleIdentifier: bundleIdentifier)
            .filter { $0.processIdentifier != currentPID }

        guard let existing = duplicates.first else {
            return false
        }

        existing.activate()
        allowsTermination = true
        NSApp.terminate(nil)
        return true
    }
}

@main
struct CenterWordApplication: App {
    @NSApplicationDelegateAdaptor(CenterWordApplicationDelegate.self) private var appDelegate
    @StateObject private var session = CenterWordSession()

    var body: some Scene {
        WindowGroup("CenterWord", id: "main") {
            TeleprompterView()
                .environmentObject(session)
                .background(
                    OpenWindowRegistrar { openMainWindow in
                        appDelegate.session = session
                        session.openMainWindow = openMainWindow
                    }
                )
        }
        .defaultSize(width: 900, height: 680)
    }
}

private struct OpenWindowRegistrar: View {
    @Environment(\.openWindow) private var openWindow

    let register: (@escaping () -> Void) -> Void

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .onAppear {
                register {
                    openWindow(id: "main")
                }
            }
    }
}
