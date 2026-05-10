import SwiftUI

/// Left panel: provider/login status, permissions, daemon controls,
/// settings entry. Refreshes from `OpenSeerEnv.refresh()` and from
/// any explicit user-triggered probe.
struct Sidebar: View {
    @EnvironmentObject var env: OpenSeerEnv
    @ObservedObject var daemon: DaemonController
    @Binding var showSettings: Bool
    @Binding var statusBlob: SystemStatus?
    var refreshStatus: () -> Void
    @State private var confirmReset: Bool = false
    @State private var resettingNow: Bool = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                Divider()
                providerSection
                permissionsSection
                Divider()
                daemonSection
                Divider()
                navSection
                Spacer(minLength: 12)
            }
            .padding(16)
        }
        .frame(maxWidth: .infinity)
        .background(.background.secondary)
    }

    private var header: some View {
        Text("OpenSeer").font(.title2.bold())
    }

    private var providerSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("Provider")
            if let s = statusBlob {
                providerRow(name: "OpenAI GPT",
                            status: s.providers.openai,
                            isSelected: s.selectedProvider == "openai")
                providerRow(name: "Anthropic Claude",
                            status: s.providers.anthropic,
                            isSelected: s.selectedProvider == "anthropic")
            } else {
                Text("…").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var permissionsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("Permissions")
            HStack(spacing: 12) {
                permRow("Accessibility",
                        ok: statusBlob?.permissions.accessibility ?? false)
                permRow("Screen Recording",
                        ok: statusBlob?.permissions.screenRecording ?? false)
            }
            Button {
                refreshStatus()
            } label: {
                Label("Refresh status", systemImage: "arrow.clockwise")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
        }
    }

    private var daemonSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("Telegram daemon")
            HStack {
                Circle().fill(daemon.isRunning ? .green : .secondary)
                    .frame(width: 8, height: 8)
                Text(daemon.isRunning ? "running" : "stopped")
                    .font(.caption)
                Spacer()
                if daemon.isRunning {
                    Button("Stop") { daemon.stop() }
                        .controlSize(.small)
                } else {
                    Button("Start") { daemon.start() }
                        .controlSize(.small)
                        .buttonStyle(.borderedProminent)
                }
            }
            if let tg = statusBlob?.telegram, tg.configured {
                let n = tg.allowedChatIds.count
                Text("\(n) allowed chat\(n == 1 ? "" : "s") · "
                     + (tg.tokenPresent ? "token set" : "token MISSING"))
                    .font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("not configured — open Settings to add a bot token")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
            if let err = daemon.startupError {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
    }

    private var navSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionHeader("Tools")
            navButton("Settings", icon: "gearshape") {
                showSettings = true
            }
            navButton("Re-run setup",
                      icon: resettingNow ? "hourglass" : "wand.and.stars") {
                confirmReset = true
            }
            .disabled(resettingNow)
        }
        .alert("Reset OpenSeer?", isPresented: $confirmReset) {
            Button("Cancel", role: .cancel) {}
            Button("Reset", role: .destructive) {
                Task { await runFactoryReset() }
            }
        } message: {
            Text("Signs you out of OpenAI and Claude, clears the "
                 + "Telegram bot token + per-chat session memory, "
                 + "and resets the OpenSeer entry in macOS Privacy "
                 + "(Accessibility / Screen Recording).\n\n"
                 + "Not touched: SOUL.md, MEMORY.md, your saved "
                 + "skills.\n\n"
                 + "If you previously granted permissions to "
                 + "\"Python\" in Privacy & Security, that entry "
                 + "is shared with other Python tools and is left "
                 + "intact — remove it manually in System Settings "
                 + "if you want a fully clean slate.\n\n"
                 + "The setup wizard opens afterward so you can "
                 + "re-authenticate.")
        }
    }

    @MainActor
    private func runFactoryReset() async {
        resettingNow = true
        defer { resettingNow = false }
        // Stop the daemon AND wait for it to exit BEFORE wiping
        // config — otherwise the running process keeps the loaded
        // bot token + chat sessions in memory and would happily
        // reply to Telegram even though config.json is gone.
        // SIGTERM unwind takes a moment; await it.
        if daemon.isRunning {
            await daemon.stopAndWait()
        }
        if let bin = env.binaryPath {
            _ = await CLI.run(path: bin, args: ["reset"])
        }
        await env.refresh()
        env.markNeedsSetup()
    }

    // ── helpers ────────────────────────────────────────────────────

    private func sectionHeader(_ s: String) -> some View {
        Text(s.uppercased())
            .font(.caption.bold())
            .foregroundStyle(.secondary)
    }

    private func providerRow(name: String,
                             status: SystemStatus.ProviderStatus,
                             isSelected: Bool) -> some View {
        HStack(spacing: 8) {
            Image(systemName: status.loggedIn ? "checkmark.seal.fill"
                  : "exclamationmark.triangle.fill")
                .foregroundStyle(status.loggedIn ? .green : .orange)
                .font(.callout)
            Text(name).font(.callout)
            Spacer()
            if isSelected {
                Image(systemName: "circle.fill")
                    .foregroundStyle(Color.accentColor)
                    .font(.caption2)
            }
        }
    }

    private func permRow(_ name: String, ok: Bool) -> some View {
        HStack(spacing: 4) {
            Image(systemName: ok ? "checkmark" : "xmark")
                .foregroundStyle(ok ? .green : .orange)
                .font(.caption2)
            Text(name).font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func navButton(_ label: String, icon: String,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .frame(width: 16)
                    .foregroundStyle(.secondary)
                Text(label)
                Spacer()
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

extension OpenSeerEnv {
    /// Set status back to needsSetup so RootView shows the wizard.
    /// Called when the user clicks "Re-run setup" in the sidebar.
    func markNeedsSetup() {
        status = .needsSetup
    }
}
