import Foundation
import ServiceManagement

@MainActor
enum LaunchAtLoginManager {
    static func ensureEnabled() {
        installLaunchAgent()

        let service = SMAppService.mainApp

        switch service.status {
        case .enabled, .requiresApproval:
            return
        case .notRegistered, .notFound:
            try? service.register()
        @unknown default:
            return
        }
    }

    nonisolated static func launchAgentConfiguration(
        bundleIdentifier: String,
        executableURL: URL,
        homeDirectoryURL: URL
    ) -> (plistURL: URL, contents: [String: Any]) {
        let plistURL = homeDirectoryURL
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            .appendingPathComponent("com.nickita.centerword.plist")

        let contents: [String: Any] = [
            "Label": "com.nickita.centerword",
            "LimitLoadToSessionType": ["Aqua"],
            "ProcessType": "Interactive",
            "ProgramArguments": [executableURL.path],
            "RunAtLoad": true,
            "KeepAlive": [
                "SuccessfulExit": false,
            ],
            "AssociatedBundleIdentifiers": [bundleIdentifier],
        ]

        return (plistURL, contents)
    }

    private static func installLaunchAgent(
        bundle: Bundle = .main,
        fileManager: FileManager = .default
    ) {
        guard bundle.bundleURL.pathExtension == "app",
              let executableURL = bundle.executableURL,
              let bundleIdentifier = bundle.bundleIdentifier else {
            return
        }

        let configuration = launchAgentConfiguration(
            bundleIdentifier: bundleIdentifier,
            executableURL: executableURL.standardizedFileURL,
            homeDirectoryURL: fileManager.homeDirectoryForCurrentUser.standardizedFileURL
        )

        guard let plistData = try? PropertyListSerialization.data(
            fromPropertyList: configuration.contents,
            format: .xml,
            options: 0
        ) else {
            return
        }

        let launchAgentsDirectory = configuration.plistURL.deletingLastPathComponent()
        try? fileManager.createDirectory(at: launchAgentsDirectory, withIntermediateDirectories: true)

        if let existingData = try? Data(contentsOf: configuration.plistURL),
           existingData == plistData {
            return
        }

        try? plistData.write(to: configuration.plistURL, options: .atomic)
    }
}
