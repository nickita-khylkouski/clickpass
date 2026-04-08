import XCTest
@testable import CenterWord

final class TeleprompterModelTests: XCTestCase {
    func testTokenizerSplitsWhitespace() {
        XCTAssertEqual(
            TeleprompterText.words(in: "one   two\nthree\tfour"),
            ["one", "two", "three", "four"]
        )
    }

    func testParseWordsPerMinuteFiltersAndClamps() {
        XCTAssertEqual(TeleprompterLogic.parseWordsPerMinute(from: "320"), 320)
        XCTAssertEqual(TeleprompterLogic.parseWordsPerMinute(from: "0"), 1)
        XCTAssertEqual(TeleprompterLogic.parseWordsPerMinute(from: "999999"), 2_000)
        XCTAssertEqual(TeleprompterLogic.parseWordsPerMinute(from: "2a5b0"), 250)
        XCTAssertNil(TeleprompterLogic.parseWordsPerMinute(from: ""))
    }

    func testStoredDefaultWordsPerMinuteFallsBackWhenUnset() {
        let defaults = UserDefaults(suiteName: #function)!
        defaults.removePersistentDomain(forName: #function)

        XCTAssertEqual(
            TeleprompterLogic.storedDefaultWordsPerMinute(userDefaults: defaults),
            TeleprompterLogic.fallbackWordsPerMinute
        )
    }

    func testStoredDefaultWordsPerMinuteClampsSavedValue() {
        let defaults = UserDefaults(suiteName: #function)!
        defaults.removePersistentDomain(forName: #function)
        defaults.set(999999, forKey: TeleprompterLogic.defaultWordsPerMinuteKey)

        XCTAssertEqual(
            TeleprompterLogic.storedDefaultWordsPerMinute(userDefaults: defaults),
            2_000
        )
    }

    func testSeekIndexUsesSecondsAtCurrentSpeed() {
        XCTAssertEqual(
            TeleprompterLogic.seekIndex(currentIndex: 100, bySeconds: 5, wordsPerMinute: 240, totalWords: 500),
            120
        )
        XCTAssertEqual(
            TeleprompterLogic.seekIndex(currentIndex: 100, bySeconds: -5, wordsPerMinute: 240, totalWords: 500),
            80
        )
    }

    func testSeekIndexClampsWithinBounds() {
        XCTAssertEqual(
            TeleprompterLogic.seekIndex(currentIndex: 2, bySeconds: -5, wordsPerMinute: 300, totalWords: 40),
            0
        )
        XCTAssertEqual(
            TeleprompterLogic.seekIndex(currentIndex: 38, bySeconds: 5, wordsPerMinute: 300, totalWords: 40),
            39
        )
    }

    func testDurationHelpersUseCurrentSpeed() {
        XCTAssertEqual(TeleprompterLogic.wordsToSeek(forSeconds: 5, wordsPerMinute: 240), 20)
        XCTAssertEqual(TeleprompterLogic.elapsedSeconds(currentIndex: 30, wordsPerMinute: 120), 15, accuracy: 0.001)
        XCTAssertEqual(
            TeleprompterLogic.remainingSeconds(currentIndex: 30, totalWords: 61, wordsPerMinute: 120),
            15,
            accuracy: 0.001
        )
    }

    func testLaunchAgentConfigurationTargetsInstalledApp() {
        let configuration = LaunchAtLoginManager.launchAgentConfiguration(
            bundleIdentifier: "com.nickita.centerword",
            executableURL: URL(fileURLWithPath: "/Users/test/Applications/CenterWord.app/Contents/MacOS/CenterWord"),
            homeDirectoryURL: URL(fileURLWithPath: "/Users/test", isDirectory: true)
        )

        XCTAssertEqual(
            configuration.plistURL.path,
            "/Users/test/Library/LaunchAgents/com.nickita.centerword.plist"
        )
        XCTAssertEqual(configuration.contents["Label"] as? String, "com.nickita.centerword")
        XCTAssertEqual(
            configuration.contents["ProgramArguments"] as? [String],
            ["/Users/test/Applications/CenterWord.app/Contents/MacOS/CenterWord"]
        )
        XCTAssertEqual(
            configuration.contents["AssociatedBundleIdentifiers"] as? [String],
            ["com.nickita.centerword"]
        )
        XCTAssertEqual(configuration.contents["LimitLoadToSessionType"] as? [String], ["Aqua"])
        XCTAssertEqual(configuration.contents["ProcessType"] as? String, "Interactive")
        XCTAssertEqual(
            (configuration.contents["KeepAlive"] as? [String: Bool])?["SuccessfulExit"],
            false
        )
    }
}
