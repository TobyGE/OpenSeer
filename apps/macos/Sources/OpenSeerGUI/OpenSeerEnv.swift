import Foundation
import SwiftUI

/// Discovers the `openseer` Python CLI on PATH, runs status probes,
/// and exposes the readiness state to SwiftUI.
///
/// The Swift app does NOT reimplement any agent logic — every operation
/// shells out to the Python CLI. This class is the single seam where
/// "find the binary + run a quick status check" lives so the rest of
/// the UI can `await env.refresh()` and switch on `env.status`.
@MainActor
final class OpenSeerEnv: ObservableObject {
    static let shared = OpenSeerEnv()

    enum Status: Equatable {
        case loading
        case needsSetup
        case ready
        case error(String)
    }

    @Published var status: Status = .loading
    @Published private(set) var binaryPath: String? = nil
    @Published private(set) var authSummary: String = ""
    @Published private(set) var provider: String = ""

    private init() {}

    func refresh() async {
        // CRITICAL: do NOT bounce status to .loading here on a
        // periodic refresh — the RootView's `switch env.status`
        // tears down MainView when status flips to .loading and
        // rebuilds it on .ready, which resets the @StateObject
        // MainController (and with it, selectedThreadID).
        // Consequence: voice prompts arriving across a refresh
        // boundary land in separate threads instead of continuing
        // the same conversation. Only set .loading on the very
        // first call, when we have no result yet to show.
        if status == .ready || status == .needsSetup {
            // keep current view alive; only update result fields
            // and the final status at the end.
        } else {
            status = .loading
        }
        guard let path = locateBinary() else {
            status = .error(
                "Couldn't find the `openseer` CLI on $PATH. Install it "
                + "(e.g. `pip install -e .` from the repo root) and restart "
                + "this app."
            )
            return
        }
        binaryPath = path

        // Use the new provider-aware `openseer check --json` instead
        // of `openseer auth status` (which only checks Codex /
        // OpenAI). Codex flagged that an Anthropic-only user
        // completing the wizard would still be bounced back to
        // setup because auth status exits non-zero for them. With
        // check --json we look at the SELECTED provider's login
        // state and gate readiness on that one.
        guard let blob = await StatusProbe.fetch(binary: path) else {
            status = .error(
                "Couldn't read system status (`openseer check --json` "
                + "failed). Make sure the `openseer` CLI is installed and "
                + "this `python -m openseer` import path works."
            )
            return
        }
        // Note: StatusProbe.fetch already replaces blob.permissions
        // with Swift-side probes. The python child's TCC identity
        // differs from the .app's, so the JSON's permission flags
        // are unreliable in the bundled release.
        provider = blob.selectedProvider ?? ""

        // Build a one-line summary for the chat header.
        let lines = [
            "provider: \(blob.selectedProvider ?? "?")",
            blob.providers.openai.loggedIn ? "openai: ✓" : "openai: ✗",
            blob.providers.anthropic.loggedIn ? "claude: ✓" : "claude: ✗",
        ]
        authSummary = lines.joined(separator: " · ")

        let providerOK: Bool
        switch blob.selectedProvider {
        case "anthropic": providerOK = blob.providers.anthropic.loggedIn
        case "openai":    providerOK = blob.providers.openai.loggedIn
        default:
            providerOK = false
        }
        let permsOK = blob.permissions.accessibility
            && blob.permissions.screenRecording
        let target: Status = (providerOK && permsOK) ? .ready : .needsSetup
        // Skip the assignment when nothing changed — @Published
        // emits objectWillChange even on equal values, which would
        // re-run RootView.body and risk an identity churn that
        // resets @StateObject.
        if status != target { status = target }
    }

    /// Find `openseer`. We try in order:
    ///   1. Bundled shim at <App>.app/Contents/MacOS/openseer (DMG release)
    ///   2. ./.venv/bin/openseer  (dev workflow)
    ///   3. /usr/local/bin/openseer
    ///   4. /opt/homebrew/bin/openseer
    ///   5. plain $PATH lookup via `which`
    private func locateBinary() -> String? {
        // The release .app bundles its own Python + openseer. The
        // shim lives in Resources/ (not MacOS/ — extra scripts in
        // MacOS confuse codesign). Prefer it over PATH so a stale
        // system openseer doesn't hijack a user who installed the
        // .app and never `pip install`'d anything.
        let bundled = Bundle.main.bundlePath
            + "/Contents/Resources/openseer"
        if FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        // Dev workflow puts `openseer` in the repo-root .venv; from
        // `swift run` cwd that's two levels up (apps/macos → repo).
        // Codex P2: a single ./venv check missed this case and made
        // first-launch from the documented `cd apps/macos && swift
        // run` flow report "CLI missing" unless the user added it to
        // PATH.
        let cwd = FileManager.default.currentDirectoryPath
        let candidates: [String] = [
            cwd + "/.venv/bin/openseer",
            cwd + "/../.venv/bin/openseer",
            cwd + "/../../.venv/bin/openseer",
            "/usr/local/bin/openseer",
            "/opt/homebrew/bin/openseer",
        ]
        for c in candidates {
            if FileManager.default.isExecutableFile(atPath: c) { return c }
        }
        // Fall back to `which`. Some users have it via pyenv/conda shims.
        // GUI processes launched by Finder inherit a sparse PATH
        // (/usr/bin:/bin) — prepend the usual locations so `which`
        // can find pyenv/conda/npm-global installs.
        let task = Process()
        task.launchPath = "/usr/bin/env"
        task.arguments = ["which", "openseer"]
        var env = ProcessInfo.processInfo.environment
        let extra = "/opt/homebrew/bin:/usr/local/bin:"
            + (NSHomeDirectory() + "/.local/bin:")
            + (NSHomeDirectory() + "/.npm-global/bin")
        env["PATH"] = extra + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        task.environment = env
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do { try task.run() } catch { return nil }
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let s = String(data: data, encoding: .utf8) ?? ""
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
