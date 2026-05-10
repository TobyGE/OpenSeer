import Foundation
import SwiftUI

/// Mutable state for the setup wizard. Bound by every step view.
@MainActor
final class SetupViewModel: ObservableObject {
    @Published var status: SystemStatus? = nil
    @Published var isProbing: Bool = false
    @Published var probeError: String? = nil

    /// Currently chosen provider in the wizard. When nil, we infer
    /// from the resolved provider in status (or fall back to the
    /// one that's already logged in, or "openai" as final default).
    @Published var chosenProvider: String? = nil

    /// Telegram form fields, populated from status on first load. We
    /// keep an explicit dirty flag so "Save" only fires when the user
    /// changed something (avoids overwriting concurrent edits).
    @Published var telegramEnabled: Bool = false
    @Published var telegramToken: String = ""
    @Published var telegramAllowedChatIds: String = ""    // comma-separated for editing
    @Published var telegramTriggerPrefix: String = ""
    @Published var telegramMaxSteps: String = "200"
    @Published var telegramStepCheckInterval: String = "30"
    @Published var telegramFormDirty: Bool = false
    @Published var telegramSaveError: String? = nil

    private let binary: String

    init(binary: String) {
        self.binary = binary
    }

    func probe() async {
        isProbing = true
        probeError = nil
        if let s = await StatusProbe.fetch(binary: binary) {
            status = s
            // Default chosen provider from selected_provider if user
            // hasn't manually picked.
            if chosenProvider == nil {
                chosenProvider = s.selectedProvider
                    ?? (s.providers.openai.loggedIn ? "openai"
                        : (s.providers.anthropic.loggedIn ? "anthropic" : "openai"))
            }
            // Pre-fill telegram form on first probe (don't clobber
            // the user's in-progress edits afterward).
            if !telegramFormDirty {
                telegramEnabled = s.telegram.enabled
                telegramAllowedChatIds = s.telegram.allowedChatIds.map(String.init)
                    .joined(separator: ", ")
                telegramTriggerPrefix = s.telegram.triggerPrefix
                if let m = s.telegram.maxSteps { telegramMaxSteps = String(m) }
                if let v = s.telegram.stepCheckInterval { telegramStepCheckInterval = String(v) }
                // Token field stays empty: status hides the actual
                // token, and we only write back when the user types
                // a fresh value.
            }
        } else {
            probeError = "openseer check --json failed."
        }
        isProbing = false
    }

    /// `openseer auth login --provider openai` — codex CLI's
    /// browser-based OAuth.
    func runOpenAILogin() async {
        _ = await CLI.run(path: binary,
                          args: ["auth", "login", "--provider", "openai"])
        await probe()
    }

    /// Two-step Anthropic OAuth so the GUI can render the
    /// paste-back UI itself (claude.com only registers a hosted
    /// callback, no localhost). Returns nil on failure to start.
    @Published var anthropicVerifier: String? = nil
    @Published var anthropicState: String? = nil
    @Published var anthropicError: String? = nil
    /// The auth URL — shown in the GUI's phase-2 view as a "browser
    /// didn't open? click here" link. Surfaced even on success so
    /// the user can recover when Launch Services / default-browser
    /// is misconfigured (codex P2).
    @Published var anthropicAuthURL: String? = nil
    @Published var anthropicBrowserOpened: Bool = true

    /// Step 1: launch the browser via the Python module and capture
    /// state + verifier. Stash them on the model for step 2.
    func startAnthropicLogin() async -> Bool {
        anthropicError = nil
        let r = await CLI.run(path: binary,
                              args: ["auth", "login",
                                     "--provider", "anthropic",
                                     "--mode", "start"])
        guard r.exitCode == 0 else {
            anthropicError = "Couldn't start login: " +
                (r.stderr.isEmpty ? r.stdout : r.stderr)
            return false
        }
        // Last non-empty line of stdout is the JSON blob.
        let line = r.stdout
            .split(separator: "\n")
            .last { !$0.isEmpty }
            .map(String.init) ?? ""
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let v = obj["verifier"] as? String,
              let s = obj["state"] as? String else {
            anthropicError = "Couldn't parse login start payload."
            return false
        }
        anthropicVerifier = v
        anthropicState = s
        anthropicAuthURL = obj["url"] as? String
        anthropicBrowserOpened = (obj["opened"] as? Bool) ?? true
        return true
    }

    /// Step 2: hand the pasted code to Python for token exchange,
    /// then refresh status. Returns true on success.
    ///
    /// Secrets (auth code + PKCE verifier) are passed via stdin —
    /// not argv — so other local processes can't read them via
    /// `ps`/Activity Monitor.
    func finishAnthropicLogin(code: String) async -> Bool {
        guard let verifier = anthropicVerifier else {
            anthropicError = "No verifier stashed — start the login first."
            return false
        }
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        var payload: [String: String] = ["code": trimmed,
                                         "verifier": verifier]
        if let s = anthropicState { payload["state"] = s }
        guard let stdinData = try? JSONSerialization.data(
            withJSONObject: payload) else {
            anthropicError = "Couldn't serialise login payload."
            return false
        }
        let r = await CLI.run(
            path: binary,
            args: ["auth", "login", "--provider", "anthropic",
                   "--mode", "finish"],
            stdin: stdinData,
        )
        if r.exitCode != 0 {
            anthropicError = r.stdout.isEmpty ? r.stderr : r.stdout
            return false
        }
        anthropicVerifier = nil
        anthropicState = nil
        anthropicError = nil
        anthropicAuthURL = nil
        anthropicBrowserOpened = true
        await probe()
        return true
    }

    /// Compatibility alias kept so existing callers (and the
    /// non-interactive `openseer auth login --provider anthropic`
    /// CLI path used outside the GUI) still work. The GUI itself
    /// no longer uses this.
    func runAnthropicLogin() async {
        _ = await CLI.run(path: binary,
                          args: ["auth", "login", "--provider", "anthropic"])
        await probe()
    }

    /// `openseer permissions request` — calls AXIsProcessTrusted
    /// (with prompt) AND CGRequestScreenCaptureAccess from the
    /// PYTHON process so macOS adds it to the Privacy lists. The
    /// .app's TCC grants don't propagate to the python child;
    /// without this the user could never get python into those
    /// lists and runs would silently fail to capture/control.
    func runPermissionRequest() async {
        _ = await CLI.run(path: binary, args: ["permissions", "request"])
        await probe()
    }

    /// Persist the selected provider to ~/.openseer/config.json. Same
    /// shape the CLI setup wizard writes.
    func saveProvider() async {
        guard let p = chosenProvider else { return }
        await ConfigStore.setProvider(p)
        await probe()
    }

    /// Persist the telegram block. Token is only written when non-empty
    /// (so re-saving without re-typing keeps the existing token).
    func saveTelegram() async {
        let parsedIds: [Int] = telegramAllowedChatIds
            .split(whereSeparator: { c in c == "," || c == " " || c == "\n" })
            .compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
        let max = Int(telegramMaxSteps)
        let interval = Int(telegramStepCheckInterval)
        do {
            try ConfigStore.updateTelegram(
                enabled: telegramEnabled,
                token: telegramToken.isEmpty ? nil : telegramToken,
                allowedChatIds: parsedIds,
                triggerPrefix: telegramTriggerPrefix,
                maxSteps: max,
                stepCheckInterval: interval,
            )
            telegramSaveError = nil
            telegramFormDirty = false
            telegramToken = ""           // never keep secrets in memory longer than needed
            await probe()
        } catch {
            telegramSaveError = "Failed to save: \(error.localizedDescription)"
        }
    }
}
