import AppKit
import ApplicationServices
import Carbon
import Foundation

enum SelectedTextCaptureError: LocalizedError {
    case accessibilityPermissionRequired
    case noSelectedTextFound

    var errorDescription: String? {
        switch self {
        case .accessibilityPermissionRequired:
            return "CenterWord needs Accessibility permission to read highlighted text. Enable it, then press Cmd+Option+S again."
        case .noSelectedTextFound:
            return "No highlighted text was found in the frontmost app."
        }
    }
}

struct SelectedTextCaptureService {
    static func accessibilityPermissionGranted() -> Bool {
        let options = [
            "AXTrustedCheckOptionPrompt": false,
        ] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    @discardableResult
    static func promptForAccessibilityPermission() -> Bool {
        let options = [
            "AXTrustedCheckOptionPrompt": true,
        ] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    func captureSelectedText(promptIfNeeded: Bool) -> Result<String, SelectedTextCaptureError> {
        guard ensureAccessibilityPermission(promptIfNeeded: promptIfNeeded) else {
            return .failure(.accessibilityPermissionRequired)
        }

        if let text = selectedTextFromAccessibility(), !text.isEmpty {
            return .success(text)
        }

        if let text = selectedTextFromCopyFallback(), !text.isEmpty {
            return .success(text)
        }

        return .failure(.noSelectedTextFound)
    }

    private func ensureAccessibilityPermission(promptIfNeeded: Bool) -> Bool {
        let options = [
            "AXTrustedCheckOptionPrompt": promptIfNeeded,
        ] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    private func selectedTextFromAccessibility() -> String? {
        let systemWide = AXUIElementCreateSystemWide()
        guard let focusedElement = copyElementAttribute(kAXFocusedUIElementAttribute as CFString, from: systemWide) else {
            return nil
        }

        if let text = copyStringAttribute(kAXSelectedTextAttribute as CFString, from: focusedElement)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !text.isEmpty {
            return text
        }

        return nil
    }

    private func selectedTextFromCopyFallback() -> String? {
        let pasteboard = NSPasteboard.general
        let originalItems: [[(NSPasteboard.PasteboardType, Data)]]? = pasteboard.pasteboardItems?.map { item in
            item.types.compactMap { type -> (NSPasteboard.PasteboardType, Data)? in
                guard let data = item.data(forType: type) else {
                    return nil
                }
                return (type, data)
            }
        }
        let originalChangeCount = pasteboard.changeCount

        postCommandC()
        let copyResult = waitForCopiedText(
            in: pasteboard,
            originalChangeCount: originalChangeCount
        )

        restorePasteboard(originalItems)

        guard copyResult.changeCount != originalChangeCount else {
            return nil
        }

        return copyResult.text?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func waitForCopiedText(
        in pasteboard: NSPasteboard,
        originalChangeCount: Int
    ) -> (text: String?, changeCount: Int) {
        let pollIntervals: [useconds_t] = [60_000, 60_000, 80_000, 120_000, 160_000, 220_000]
        var latestChangeCount = pasteboard.changeCount
        var latestText = pasteboard.string(forType: .string)

        for interval in pollIntervals {
            usleep(interval)
            latestChangeCount = pasteboard.changeCount
            if latestChangeCount != originalChangeCount {
                latestText = pasteboard.string(forType: .string)
                break
            }
        }

        return (latestText, latestChangeCount)
    }

    private func restorePasteboard(_ originalItems: [[(NSPasteboard.PasteboardType, Data)]]?) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()

        guard let originalItems else {
            return
        }

        for itemEntries in originalItems {
            let item = NSPasteboardItem()
            for (type, data) in itemEntries {
                item.setData(data, forType: type)
            }
            pasteboard.writeObjects([item])
        }
    }

    private func postCommandC() {
        guard let keyDown = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(kVK_ANSI_C), keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(kVK_ANSI_C), keyDown: false) else {
            return
        }

        keyDown.flags = CGEventFlags.maskCommand
        keyUp.flags = CGEventFlags.maskCommand
        keyDown.post(tap: CGEventTapLocation.cghidEventTap)
        keyUp.post(tap: CGEventTapLocation.cghidEventTap)
    }

    private func copyElementAttribute(_ attribute: CFString, from element: AXUIElement) -> AXUIElement? {
        var rawValue: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute, &rawValue)
        guard status == .success, let rawValue else {
            return nil
        }

        return (rawValue as! AXUIElement)
    }

    private func copyStringAttribute(_ attribute: CFString, from element: AXUIElement) -> String? {
        var rawValue: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute, &rawValue)
        guard status == .success, let rawValue else {
            return nil
        }

        return rawValue as? String
    }
}
