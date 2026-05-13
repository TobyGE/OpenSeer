import AppKit
import SwiftUI

/// Multi-step setup wizard. Each step body now reads from
/// `SetupViewModel.status` (populated by `openseer check --json`)
/// and offers actions to fix what's missing — provider login,
/// Accessibility / Screen Recording grants, Telegram bot config.
struct SetupView: View {
    @EnvironmentObject var env: OpenSeerEnv
    @StateObject private var model: SetupViewModel
    @State private var step: SetupStep = .provider

    init() {
        // Bind the wizard model to the resolved binary path. Done
        // once at view construction so SwiftUI's StateObject
        // identity doesn't churn on every redraw.
        let bin = OpenSeerEnv.shared.binaryPath
            ?? "/usr/local/bin/openseer"
        _model = StateObject(wrappedValue: SetupViewModel(binary: bin))
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Setup")
                    .font(.title3.bold())
                    .padding(.bottom, 8)
                ForEach(SetupStep.allCases, id: \.self) { s in
                    StepRailItem(step: s,
                                 isCurrent: s == step,
                                 isDone: stepIsDone(s))
                        .contentShape(Rectangle())
                        .onTapGesture { step = s }
                }
                Spacer()
                if let err = model.probeError {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
                Button {
                    Task { await model.probe() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(model.isProbing)
            }
            .padding()
            .frame(width: 220)
            .background(.background.secondary)

            Divider()

            VStack(alignment: .leading, spacing: 16) {
                stepBody
                Spacer()
                HStack {
                    Button("Back") { step = step.prev() ?? step }
                        .disabled(step == .provider)
                    Spacer()
                    if step == .done {
                        Button("Open OpenSeer") {
                            Task { await env.refresh() }
                        }
                        .keyboardShortcut(.defaultAction)
                    } else {
                        Button("Next") { step = step.next() ?? step }
                            .keyboardShortcut(.defaultAction)
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .task { await model.probe() }
    }

    @ViewBuilder
    private var stepBody: some View {
        switch step {
        case .provider:    ProviderStepView().environmentObject(model)
        case .auth:        AuthStepView().environmentObject(model)
        case .permissions: PermissionsStepView().environmentObject(model)
        case .telegram:    TelegramStepView().environmentObject(model)
        case .done:        DoneStepView().environmentObject(model)
        }
    }

    private func stepIsDone(_ s: SetupStep) -> Bool {
        guard let st = model.status else { return false }
        switch s {
        case .provider:
            return model.chosenProvider != nil
        case .auth:
            switch model.chosenProvider {
            case "openai":    return st.providers.openai.loggedIn
            case "anthropic": return st.providers.anthropic.loggedIn
            default:          return false
            }
        case .permissions:
            return st.permissions.accessibility && st.permissions.screenRecording
        case .telegram:
            // Optional step — count it "done" when configured OR
            // explicitly skipped (we treat any visit past the form
            // as acknowledged; tracking a real skip flag is overkill).
            return st.telegram.configured
        case .done:
            return false
        }
    }
}

enum SetupStep: Int, CaseIterable {
    case provider = 0
    case auth
    case permissions
    case telegram
    case done

    var title: String {
        switch self {
        case .provider:    return "Model provider"
        case .auth:        return "Sign in"
        case .permissions: return "macOS permissions"
        case .telegram:    return "Telegram (optional)"
        case .done:        return "Ready"
        }
    }

    func next() -> SetupStep? { SetupStep(rawValue: rawValue + 1) }
    func prev() -> SetupStep? { SetupStep(rawValue: rawValue - 1) }
}

private struct StepRailItem: View {
    let step: SetupStep
    let isCurrent: Bool
    let isDone: Bool
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: isDone
                ? "checkmark.circle.fill"
                : (isCurrent ? "circle.dotted" : "circle"))
                .foregroundStyle(isDone ? .green : (isCurrent ? .accentColor : .secondary))
            Text(step.title)
                .foregroundStyle(isCurrent ? .primary : .secondary)
                .fontWeight(isCurrent ? .semibold : .regular)
        }
        .padding(.vertical, 4)
    }
}

// ─── step bodies ───────────────────────────────────────────────────

private struct ProviderStepView: View {
    @EnvironmentObject var model: SetupViewModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Model provider").font(.title2.bold())
            Text("OpenSeer drives your Mac with either GPT-5.5 (Codex CLI OAuth) or Claude Haiku 4.5 (Claude Code OAuth). Pick whichever subscription you have.")
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                ProviderCard(
                    name: "OpenAI · GPT-5.5",
                    auth: "Codex CLI",
                    status: model.status?.providers.openai,
                    binaryFound: model.status?.binaryPaths.codex != nil,
                    isSelected: model.chosenProvider == "openai",
                    select: {
                        model.chosenProvider = "openai"
                        Task { await model.saveProvider() }
                    }
                )
                ProviderCard(
                    name: "Anthropic · Haiku 4.5",
                    auth: "Claude Code",
                    status: model.status?.providers.anthropic,
                    binaryFound: model.status?.binaryPaths.claude != nil,
                    isSelected: model.chosenProvider == "anthropic",
                    select: {
                        model.chosenProvider = "anthropic"
                        Task { await model.saveProvider() }
                    }
                )
            }
            if let sel = model.chosenProvider {
                Text("Selected: \(sel == "openai" ? "OpenAI · GPT-5.5" : "Anthropic · Haiku 4.5"). The next step finalizes the OAuth login if it isn't already complete.")
                    .font(.callout)
                    .foregroundStyle(.tertiary)
            }
        }
    }
}

private struct ProviderCard: View {
    let name: String
    let auth: String
    let status: SystemStatus.ProviderStatus?
    let binaryFound: Bool
    let isSelected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(name).font(.headline)
                    Spacer()
                    statusBadge
                }
                Text("Auth: \(auth) · CLI \(binaryFound ? "found" : "not on PATH")")
                    .font(.caption).foregroundStyle(.secondary)
                if let err = status?.error, !(status?.loggedIn ?? false) {
                    Text(err)
                        .font(.caption2).foregroundStyle(.orange)
                        .lineLimit(2)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(isSelected ? Color.accentColor.opacity(0.15)
                        : Color(nsColor: .controlBackgroundColor))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(isSelected ? Color.accentColor : Color.secondary.opacity(0.2),
                            lineWidth: isSelected ? 2 : 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var statusBadge: some View {
        if status?.loggedIn == true {
            Label("logged in", systemImage: "checkmark.seal.fill")
                .labelStyle(.titleAndIcon)
                .font(.caption2)
                .foregroundStyle(.green)
        } else {
            Label("needs login", systemImage: "exclamationmark.triangle.fill")
                .labelStyle(.titleAndIcon)
                .font(.caption2)
                .foregroundStyle(.orange)
        }
    }
}

private struct AuthStepView: View {
    @EnvironmentObject var model: SetupViewModel
    @State private var isLoggingIn = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Sign in").font(.title2.bold())
            if let provider = model.chosenProvider {
                if provider == "openai" {
                    OpenAILoginView(isLoggingIn: $isLoggingIn)
                        .environmentObject(model)
                } else if provider == "anthropic" {
                    AnthropicLoginView(isLoggingIn: $isLoggingIn)
                        .environmentObject(model)
                }
            } else {
                Text("Pick a provider on the previous step first.")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct OpenAILoginView: View {
    @EnvironmentObject var model: SetupViewModel
    @Binding var isLoggingIn: Bool
    var body: some View {
        let st = model.status?.providers.openai
        VStack(alignment: .leading, spacing: 8) {
            if st?.loggedIn == true {
                Label("Codex CLI: signed in", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                if let plan = st?.plan {
                    Text("Plan: \(plan)").font(.caption).foregroundStyle(.secondary)
                }
                Text("Token expires in \((st?.expiresInS ?? 0) / 3600)h.")
                    .font(.caption).foregroundStyle(.tertiary)
            } else {
                Label("Codex CLI not signed in", systemImage: "person.crop.circle.badge.questionmark")
                    .foregroundStyle(.orange)
                Text("Click below; the existing Codex OAuth flow opens in your browser. The Swift app does not capture credentials directly.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Sign in via Codex CLI") {
                    Task {
                        isLoggingIn = true
                        await model.runOpenAILogin()
                        isLoggingIn = false
                    }
                }
                .disabled(isLoggingIn)
                .buttonStyle(.borderedProminent)
                if isLoggingIn {
                    ProgressView("Waiting for browser flow…")
                        .progressViewStyle(.linear)
                }
                if let err = st?.error {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
            }
        }
    }
}

private struct AnthropicLoginView: View {
    @EnvironmentObject var model: SetupViewModel
    @Binding var isLoggingIn: Bool
    @State private var pastedCode: String = ""

    var body: some View {
        let st = model.status?.providers.anthropic
        VStack(alignment: .leading, spacing: 10) {
            if st?.loggedIn == true {
                Label("Claude Code: signed in", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                if let sub = st?.subscription {
                    Text("Subscription: \(sub)").font(.caption).foregroundStyle(.secondary)
                }
                Text("Token expires in \((st?.expiresInS ?? 0) / 60)m.")
                    .font(.caption).foregroundStyle(.tertiary)
            } else if model.anthropicVerifier == nil {
                phaseOne
            } else {
                phaseTwo
            }
            if let err = model.anthropicError {
                Text(err).font(.caption).foregroundStyle(.red)
            } else if let err = st?.error {
                Text(err).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private var phaseOne: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Claude Code not signed in",
                  systemImage: "person.crop.circle.badge.questionmark")
                .foregroundStyle(.orange)
            Text("Click Sign in — your browser will open to claude.com for OAuth. After approving, the page shows a code; paste it here. No `claude` CLI required.")
                .font(.callout).foregroundStyle(.secondary)
            Button("Sign in with Claude") {
                Task {
                    isLoggingIn = true
                    _ = await model.startAnthropicLogin()
                    isLoggingIn = false
                }
            }
            .disabled(isLoggingIn)
            .buttonStyle(.borderedProminent)
        }
    }

    private var phaseTwo: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.anthropicBrowserOpened {
                Label("Browser opened — paste the code from the success page",
                      systemImage: "arrow.right.circle")
                    .foregroundStyle(.blue)
            } else {
                Label("Browser couldn't open automatically",
                      systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
            Text("After signing in at claude.com you'll land on a page that displays a long code (often formatted `abc…#xyz…`). Copy the WHOLE thing and paste here.")
                .font(.caption).foregroundStyle(.secondary)
            if let url = model.anthropicAuthURL {
                HStack(spacing: 4) {
                    Text(model.anthropicBrowserOpened
                         ? "Browser didn't open?"
                         : "Open this URL manually:")
                        .font(.caption2).foregroundStyle(.tertiary)
                    Button("Copy URL") {
                        let pb = NSPasteboard.general
                        pb.clearContents()
                        pb.setString(url, forType: .string)
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.small)
                    Button("Open in browser") {
                        if let u = URL(string: url) {
                            NSWorkspace.shared.open(u)
                        }
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.small)
                }
            }
            HStack {
                TextField("Paste code", text: $pastedCode)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isLoggingIn)
                Button("Submit") {
                    Task {
                        isLoggingIn = true
                        let ok = await model.finishAnthropicLogin(code: pastedCode)
                        if ok { pastedCode = "" }
                        isLoggingIn = false
                    }
                }
                .keyboardShortcut(.return, modifiers: [])
                .disabled(isLoggingIn || pastedCode.trimmingCharacters(in: .whitespaces).isEmpty)
                .buttonStyle(.borderedProminent)
            }
            Button("Cancel — start over") {
                model.anthropicVerifier = nil
                model.anthropicState = nil
                model.anthropicError = nil
                pastedCode = ""
            }
            .controlSize(.small)
        }
    }
}

private struct PermissionsStepView: View {
    @EnvironmentObject var model: SetupViewModel
    @State private var preflightFired = false
    var body: some View {
        let p = model.status?.permissions
        VStack(alignment: .leading, spacing: 12) {
            Text("macOS permissions").font(.title2.bold())
            Text("OpenSeer needs Accessibility (to inject mouse/keyboard) and Screen Recording (to capture screenshots). Click each \"Request\" button — macOS will prompt and add OpenSeer to the relevant Privacy list. After flipping the toggle in System Settings, hit Refresh below.")
                .foregroundStyle(.secondary)

            permissionRow(
                title: "Accessibility",
                granted: p?.accessibility ?? false,
                onRequest: {
                    // Trigger BOTH the Swift-side and python-side
                    // requests so OpenSeer.app itself AND the python
                    // child both end up in the AX list. Without the
                    // Swift request the user would have to click `+`
                    // and browse to /Applications/OpenSeer.app to
                    // add it manually.
                    //
                    // We DON'T open System Settings ourselves here:
                    // `AXIsProcessTrustedWithOptions(prompt: true)`
                    // already shows the native macOS dialog (which
                    // itself offers an "Open System Settings" button),
                    // so opening Settings preemptively just slammed
                    // the panel up before the user clicked Request.
                    // The separate "Open Settings" button in
                    // permissionRow remains as the explicit fallback.
                    Permissions.requestAccessibility()
                    Task { await model.runPermissionRequest() }
                }
            )
            permissionRow(
                title: "Screen Recording",
                granted: p?.screenRecording ?? false,
                onRequest: {
                    // Same pattern as Accessibility above —
                    // `CGRequestScreenCaptureAccess()` already
                    // surfaces the native prompt; the explicit
                    // "Open Settings" button stays as the fallback.
                    Permissions.requestScreenRecording()
                    Task { await model.runPermissionRequest() }
                }
            )

            if (p?.accessibility ?? false) && (p?.screenRecording ?? false) {
                Label("All permissions granted.", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                    .padding(.top, 4)
            } else {
                Divider().padding(.top, 6)
                fallbackHelp
            }
        }
        .onAppear {
            if !preflightFired {
                preflightFired = true
                Task { @MainActor in
                    await model.probe()
                }
            }
        }
    }

    private var fallbackHelp: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Don't see OpenSeer in the Privacy list?")
                .font(.caption.bold())
            Text("Ad-hoc-signed builds (the default `build_app.sh` "
                 + "output) get a fresh code-signing identity on every "
                 + "rebuild. macOS' TCC database keys grants by that "
                 + "identity, so a stale entry can block a fresh app "
                 + "from showing up. Two ways out:")
                .font(.caption).foregroundStyle(.secondary)

            // Option 1 — manual `+` add.
            VStack(alignment: .leading, spacing: 6) {
                Text("1. Manually add OpenSeer.app")
                    .font(.caption.bold())
                if let path = Self.bundleAppPath() {
                    pathBox(path)
                    Button {
                        NSWorkspace.shared.activateFileViewerSelecting(
                            [URL(fileURLWithPath: path)])
                    } label: {
                        Label("Reveal OpenSeer.app in Finder",
                              systemImage: "folder")
                    }
                    .controlSize(.small)
                } else {
                    Text("(running unbundled — relaunch via OpenSeer.app, "
                         + "Accessibility grants don't stick to "
                         + "`swift run` builds.)")
                        .font(.caption2).foregroundStyle(.orange)
                }
                Text("In Privacy → Accessibility / Screen Recording, click + and pick OpenSeer.app at the path above.")
                    .font(.caption2).foregroundStyle(.secondary)
            }

            // Option 2 — wipe stale TCC.
            VStack(alignment: .leading, spacing: 6) {
                Text("2. Reset stale TCC entries")
                    .font(.caption.bold())
                Text("If a stale OpenSeer entry already exists with the same bundle id, macOS won't add a fresh one. Run these in Terminal, then click Request again:")
                    .font(.caption2).foregroundStyle(.secondary)
                ResetCommandRow(
                    command: "tccutil reset Accessibility \(Self.bundleId)")
                ResetCommandRow(
                    command: "tccutil reset ScreenCapture \(Self.bundleId)")
            }
        }
    }

    private func pathBox(_ path: String) -> some View {
        HStack(spacing: 6) {
            Text(path)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.background.tertiary)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            CopyButton(value: path)
        }
    }

    private static func bundleAppPath() -> String? {
        let path = Bundle.main.bundlePath
        return path.hasSuffix(".app") ? path : nil
    }

    private static var bundleId: String {
        Bundle.main.bundleIdentifier ?? "com.openseer.OpenSeer"
    }
}

private struct ResetCommandRow: View {
    let command: String
    var body: some View {
        HStack(spacing: 6) {
            Text(command)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.background.tertiary)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            CopyButton(value: command)
        }
    }
}

private struct CopyButton: View {
    let value: String
    @State private var copied = false
    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
            copied = true
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await MainActor.run { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
        }
        .buttonStyle(.borderless)
        .help(copied ? "Copied" : "Copy")
    }
}

private func permissionRow(title: String, granted: Bool,
                           onRequest: @escaping () -> Void) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: granted ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(granted ? .green : .orange)
                .font(.title3)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                if granted {
                    Text("Granted.").font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("Click Request — macOS will prompt and "
                         + "add OpenSeer to the Privacy list. Then "
                         + "flip the toggle and tap Refresh.\n\n"
                         + "If OpenSeer is already in the list and "
                         + "the toggle looks on, click Refresh.")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack(spacing: 8) {
                        Button("Request") { onRequest() }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        Button("Open Settings") {
                            if title == "Accessibility" {
                                Permissions.openSystemSettings(pane: .accessibility)
                            } else {
                                Permissions.openSystemSettings(pane: .screenRecording)
                            }
                        }
                        .controlSize(.small)
                    }
                }
            }
            Spacer()
        }
        .padding(10)
        .background(.background.secondary)
        .clipShape(RoundedRectangle(cornerRadius: 8))
}

private struct TelegramStepView: View {
    @EnvironmentObject var model: SetupViewModel
    @State private var isSaving = false

    var body: some View {
        let tg = model.status?.telegram
        VStack(alignment: .leading, spacing: 12) {
            Text("Telegram bot (optional)").font(.title2.bold())
            Text("Lets you control this Mac from your phone. Get a token from @BotFather, then add your Telegram chat id to the allowlist. Skip if you don't want remote control.")
                .foregroundStyle(.secondary)

            Form {
                Toggle("Enable Telegram daemon", isOn: Binding(
                    get: { model.telegramEnabled },
                    set: { model.telegramEnabled = $0; model.telegramFormDirty = true }
                ))

                if let exists = tg?.tokenPresent, exists {
                    Text("Existing token is preserved unless you type a new one below.")
                        .font(.caption).foregroundStyle(.tertiary)
                }
                SecureField("Bot token (paste from @BotFather)", text: Binding(
                    get: { model.telegramToken },
                    set: { model.telegramToken = $0; model.telegramFormDirty = true }
                ))

                TextField("Allowed chat IDs (comma-separated)", text: Binding(
                    get: { model.telegramAllowedChatIds },
                    set: { model.telegramAllowedChatIds = $0; model.telegramFormDirty = true }
                ))

                TextField("Trigger prefix (optional, e.g. \"openseer:\")",
                          text: Binding(
                    get: { model.telegramTriggerPrefix },
                    set: { model.telegramTriggerPrefix = $0; model.telegramFormDirty = true }
                ))

                HStack {
                    TextField("Max steps per task", text: Binding(
                        get: { model.telegramMaxSteps },
                        set: { model.telegramMaxSteps = $0; model.telegramFormDirty = true }
                    ))
                    TextField("Step check interval", text: Binding(
                        get: { model.telegramStepCheckInterval },
                        set: { model.telegramStepCheckInterval = $0; model.telegramFormDirty = true }
                    ))
                }
            }
            .formStyle(.grouped)

            HStack {
                Button("Save Telegram config") {
                    Task {
                        isSaving = true
                        await model.saveTelegram()
                        isSaving = false
                    }
                }
                .disabled(!model.telegramFormDirty || isSaving)
                .buttonStyle(.borderedProminent)
                if isSaving { ProgressView().controlSize(.small) }
                if let err = model.telegramSaveError {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
            }
        }
    }
}

private struct DoneStepView: View {
    @EnvironmentObject var model: SetupViewModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("All set.").font(.title2.bold())
            Text("Click below to switch into the chat window. You can return to this wizard later from Settings.")
                .foregroundStyle(.secondary)
            if let st = model.status {
                summaryRow("Provider", model.chosenProvider ?? "?")
                summaryRow("Provider login",
                           providerLoggedIn(st) ? "✓ ready" : "✗ needs sign-in")
                summaryRow("Permissions",
                           st.permissions.accessibility && st.permissions.screenRecording
                           ? "✓ granted" : "✗ missing")
                summaryRow("Telegram",
                           st.telegram.configured ? "✓ configured" : "skipped")
            }
        }
    }

    private func providerLoggedIn(_ st: SystemStatus) -> Bool {
        switch model.chosenProvider {
        case "openai":    return st.providers.openai.loggedIn
        case "anthropic": return st.providers.anthropic.loggedIn
        default:          return false
        }
    }

    private func summaryRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary).frame(width: 120, alignment: .leading)
            Text(value)
        }
        .font(.callout)
    }
}
