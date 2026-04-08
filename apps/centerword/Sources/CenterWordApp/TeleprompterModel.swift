import Foundation
import AppKit
import ApplicationServices

enum TeleprompterLogic {
    static let fallbackWordsPerMinute = 860
    static let defaultWordsPerMinuteKey = "defaultWordsPerMinute"

    static func clampWordsPerMinute(_ value: Int) -> Int {
        min(max(value, 1), 2_000)
    }

    static func parseWordsPerMinute(from text: String) -> Int? {
        let digits = text.filter(\.isNumber)
        guard let value = Int(digits), !digits.isEmpty else {
            return nil
        }

        return clampWordsPerMinute(value)
    }

    static func storedDefaultWordsPerMinute(userDefaults: UserDefaults = .standard) -> Int {
        let storedValue = userDefaults.integer(forKey: defaultWordsPerMinuteKey)
        if storedValue == 0 {
            return fallbackWordsPerMinute
        }

        return clampWordsPerMinute(storedValue)
    }

    static func wordsToSeek(forSeconds seconds: TimeInterval, wordsPerMinute: Int) -> Int {
        guard seconds > 0 else {
            return 0
        }

        let words = seconds * Double(clampWordsPerMinute(wordsPerMinute)) / 60.0
        return max(1, Int(words.rounded()))
    }

    static func seekIndex(
        currentIndex: Int,
        bySeconds seconds: TimeInterval,
        wordsPerMinute: Int,
        totalWords: Int
    ) -> Int {
        guard totalWords > 0 else {
            return 0
        }

        let step = wordsToSeek(forSeconds: abs(seconds), wordsPerMinute: wordsPerMinute)
        let signedStep = seconds < 0 ? -step : step
        return min(max(currentIndex + signedStep, 0), totalWords - 1)
    }

    static func elapsedSeconds(currentIndex: Int, wordsPerMinute: Int) -> TimeInterval {
        let clampedIndex = max(currentIndex, 0)
        return Double(clampedIndex) * 60.0 / Double(clampWordsPerMinute(wordsPerMinute))
    }

    static func remainingSeconds(currentIndex: Int, totalWords: Int, wordsPerMinute: Int) -> TimeInterval {
        guard totalWords > 0 else {
            return 0
        }

        let remainingWords = max(totalWords - currentIndex - 1, 0)
        return Double(remainingWords) * 60.0 / Double(clampWordsPerMinute(wordsPerMinute))
    }
}

enum CenterWordActivation {
    @MainActor
    static func revealApplication() {
        NSApp.unhide(nil)
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        NSApp.activate(ignoringOtherApps: true)
        forceFrontmostAccessibility()
    }

    private static func forceFrontmostAccessibility() {
        let appElement = AXUIElementCreateApplication(ProcessInfo.processInfo.processIdentifier)
        AXUIElementSetAttributeValue(appElement, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    }
}

struct TeleprompterLaunchRequest: Identifiable {
    let id = UUID()
    let text: String
    let wordsPerMinute: Int
}

@MainActor
final class CenterWordSession: ObservableObject {
    @Published private(set) var launchRequest: TeleprompterLaunchRequest?
    @Published var errorMessage: String?

    var openMainWindow: (() -> Void)?
    private let revealCollectionBehavior: NSWindow.CollectionBehavior = [
        .moveToActiveSpace,
        .canJoinAllSpaces,
        .fullScreenAuxiliary,
    ]

    func presentCapturedText(_ text: String, wordsPerMinute: Int) {
        errorMessage = nil
        launchRequest = TeleprompterLaunchRequest(
            text: text,
            wordsPerMinute: TeleprompterLogic.clampWordsPerMinute(wordsPerMinute)
        )
        revealWindow()
    }

    func presentError(_ message: String) {
        errorMessage = message
        revealWindow()
    }

    func clearError() {
        errorMessage = nil
    }

    private func revealWindow() {
        let hasVisibleWindow = NSApp.windows.contains { window in
            window.isVisible && !window.isMiniaturized
        }

        if NSApp.windows.isEmpty || !hasVisibleWindow {
            openMainWindow?()
        }

        revealExistingWindows()

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
            self.revealExistingWindows()
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            self.revealExistingWindows()
        }
    }

    private func revealExistingWindows() {
        activateApplication()
        for window in NSApp.windows {
            if window.isMiniaturized {
                window.deminiaturize(nil)
            }
            window.collectionBehavior.formUnion(revealCollectionBehavior)
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
        }
    }

    private func activateApplication() {
        CenterWordActivation.revealApplication()
    }
}

@MainActor
final class TeleprompterEngine {
    private var playbackTask: Task<Void, Never>?

    func start(
        wordsPerMinute: Int,
        initialDelay: TimeInterval = 0,
        tick: @escaping @MainActor () -> Bool
    ) {
        stop()

        let clampedWordsPerMinute = TeleprompterLogic.clampWordsPerMinute(wordsPerMinute)
        playbackTask = Task {
            if initialDelay > 0 {
                try? await Task.sleep(for: .seconds(initialDelay))
            }

            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60.0 / Double(clampedWordsPerMinute)))

                if Task.isCancelled {
                    break
                }

                let shouldContinue = await MainActor.run(body: tick)
                if !shouldContinue {
                    break
                }
            }
        }
    }

    func stop() {
        playbackTask?.cancel()
        playbackTask = nil
    }

    deinit {
        playbackTask?.cancel()
    }
}
