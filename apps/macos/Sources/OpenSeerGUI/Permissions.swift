import Foundation
import ApplicationServices
import CoreGraphics
import AppKit

/// Permission probes that run INSIDE the OpenSeer.app process.
///
/// We can't trust `openseer check --json` for these in the bundled
/// release: the CLI is a child process (bash shim → bundled
/// python3) whose TCC identity is the python binary, not OpenSeer.app.
/// Granting Accessibility to OpenSeer in System Settings doesn't
/// flip the child's `AXIsProcessTrusted()` to true. Probing from
/// the Swift side fixes that — the Swift binary IS the app whose
/// bundle identifier the user toggled.
enum Permissions {

    /// True if the process has Accessibility access (AX read +
    /// synthesised events). Non-prompting.
    static func accessibilityGranted() -> Bool {
        AXIsProcessTrusted()
    }

    /// Open the Accessibility prompt (and add OpenSeer to the list
    /// if it wasn't there yet). Returns the immediately-known state
    /// (almost always false right after the call — the user has to
    /// flip the toggle in System Settings; we'll see true on the
    /// next refresh).
    @discardableResult
    static func requestAccessibility() -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue()
        let opts: CFDictionary = [key: true] as CFDictionary
        return AXIsProcessTrustedWithOptions(opts)
    }

    /// True if Screen Recording is granted. Non-prompting.
    static func screenRecordingGranted() -> Bool {
        CGPreflightScreenCaptureAccess()
    }

    /// Trigger the Screen Recording prompt AND register OpenSeer in
    /// the list. macOS only adds an app to "Screen & System Audio
    /// Recording" once it's actually called a capture API; calling
    /// this once ensures the row exists for the user to toggle on.
    @discardableResult
    static func requestScreenRecording() -> Bool {
        CGRequestScreenCaptureAccess()
    }

    /// Open System Settings directly to a TCC pane. Saves the user
    /// from clicking through Privacy & Security manually.
    static func openSystemSettings(pane: TCCPane) {
        let url = URL(string: pane.urlString)!
        NSWorkspace.shared.open(url)
    }

    enum TCCPane {
        case accessibility
        case screenRecording

        var urlString: String {
            switch self {
            case .accessibility:
                return "x-apple.systempreferences:"
                    + "com.apple.preference.security"
                    + "?Privacy_Accessibility"
            case .screenRecording:
                return "x-apple.systempreferences:"
                    + "com.apple.preference.security"
                    + "?Privacy_ScreenCapture"
            }
        }
    }
}
