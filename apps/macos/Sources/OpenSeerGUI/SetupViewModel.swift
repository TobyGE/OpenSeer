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

    /// `openseer auth login --provider anthropic` — claude CLI's
    /// browser-based OAuth (`claude auth login` under the hood).
    /// If `claude` isn't installed the call returns non-zero and
    /// the next probe surfaces it via `anthropic.error`.
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
