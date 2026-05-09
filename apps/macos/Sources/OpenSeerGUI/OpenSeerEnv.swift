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

        // `openseer auth status` prints the current auth state and exits 0
        // if logged in / not expired, non-zero otherwise. We use exit code
        // as the gate; output goes into authSummary for display.
        let result = await CLI.run(path: path, args: ["auth", "status"])
        authSummary = result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        provider = readConfigProvider() ?? ""

        if result.exitCode == 0 {
            status = .ready
        } else {
            // exit 1 is usually "not logged in / expired" — treat as needsSetup.
            // exit 2+ might be missing python deps; surface the message.
            if result.exitCode == 1 {
                status = .needsSetup
            } else {
                status = .error(
                    "openseer auth status failed (exit \(result.exitCode)).\n"
                    + result.stdout + result.stderr
                )
            }
        }
    }

    /// Read the persisted provider from ~/.openseer/config.json. Best-effort.
    private func readConfigProvider() -> String? {
        let path = NSHomeDirectory() + "/.openseer/config.json"
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let p = obj["provider"] as? String else { return nil }
        return p
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
