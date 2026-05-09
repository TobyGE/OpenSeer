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

    @Published private(set) var status: Status = .loading
    @Published private(set) var binaryPath: String? = nil
    @Published private(set) var authSummary: String = ""
    @Published private(set) var provider: String = ""

    private init() {}

    func refresh() async {
        status = .loading
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
            // No selected provider yet (first run) — needs setup.
            providerOK = false
        }
        // Permissions are also a hard requirement: without
        // Accessibility / Screen Recording the agent loop can't
        // actually drive the Mac. Surface that as needsSetup too.
        let permsOK = blob.permissions.accessibility
            && blob.permissions.screenRecording
        if providerOK && permsOK {
            status = .ready
        } else {
            status = .needsSetup
        }
    }

    /// Find `openseer` on $PATH. We try in order:
    ///   1. ./.venv/bin/openseer  (the dev workflow most contributors use)
    ///   2. /usr/local/bin/openseer
    ///   3. /opt/homebrew/bin/openseer
    ///   4. plain $PATH lookup via `which`
    private func locateBinary() -> String? {
        let candidates: [String] = [
            FileManager.default.currentDirectoryPath + "/.venv/bin/openseer",
            "/usr/local/bin/openseer",
            "/opt/homebrew/bin/openseer",
        ]
        for c in candidates {
            if FileManager.default.isExecutableFile(atPath: c) { return c }
        }
        // Fall back to `which`. Some users have it via pyenv/conda shims.
        let task = Process()
        task.launchPath = "/usr/bin/env"
        task.arguments = ["which", "openseer"]
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
