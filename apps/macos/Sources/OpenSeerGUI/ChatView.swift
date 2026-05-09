import SwiftUI

/// Two-pane layout: left sidebar with status/daemon controls, right
/// area with chat history + composer. The sessions array merges
/// locally-spawned tasks (user typed in the composer) and remote
/// sessions (daemon-spawned, surfaced via DirectoryWatcher).
struct ChatView: View {
    @EnvironmentObject var env: OpenSeerEnv
    @StateObject private var daemon: DaemonController
    @State private var showSettings: Bool = false
    @State private var statusBlob: SystemStatus? = nil
    @State private var composerText: String = ""
    @State private var dryRun: Bool = false

    init() {
        let bin = OpenSeerEnv.shared.binaryPath ?? "/usr/local/bin/openseer"
        _daemon = StateObject(wrappedValue: DaemonController(binary: bin))
    }

    var body: some View {
        HStack(spacing: 0) {
            Sidebar(daemon: daemon,
                    showSettings: $showSettings,
                    statusBlob: $statusBlob,
                    refreshStatus: refreshStatus)
            Divider()
            mainArea
        }
        .task { await refreshStatusAsync() }
        .sheet(isPresented: $showSettings) {
            SettingsSheet(statusBlob: $statusBlob,
                          onClose: { showSettings = false; refreshStatus() })
        }
    }

    private var mainArea: some View {
        VStack(spacing: 0) {
            transcript
            Divider()
            composer
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if daemon.sessions.isEmpty {
                    emptyState
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .padding(.vertical, 60)
                }
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(daemon.sessions) { session in
                        SessionBlock(session: session)
                            .id(session.id)
                    }
                }
                .padding(16)
            }
            .onChange(of: daemon.sessions.count) { _, _ in
                if let last = daemon.sessions.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("Type a task below to start.")
                .foregroundStyle(.secondary)
            Text("Examples: \"open Calculator and compute 999×123\", \"summarise top podcast posts on X\".")
                .multilineTextAlignment(.center)
                .foregroundStyle(.tertiary)
                .frame(maxWidth: 380)
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .bottom) {
                TextField("Ask OpenSeer to do something…",
                          text: $composerText, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { submit() }
                Button {
                    submit()
                } label: {
                    Image(systemName: "paperplane.fill")
                        .padding(.horizontal, 4)
                }
                .keyboardShortcut(.return, modifiers: [.command])
                .buttonStyle(.borderedProminent)
                .disabled(composerText.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            HStack {
                Toggle("Dry run (preview only)", isOn: $dryRun)
                    .controlSize(.small)
                    .toggleStyle(.switch)
                Spacer()
                Text("⌘↩ to send")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(12)
    }

    private func submit() {
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let s = RunSession(source: .localPrompt(text),
                           binary: env.binaryPath ?? "/usr/local/bin/openseer")
        // Claim the prompt with the daemon BEFORE spawning the
        // subprocess. The directory watcher reads task.json from
        // each new dir; if the prompt matches a claim, it skips
        // (no double-spawn). Once the subprocess prints its
        // out_dir, we also reserve the trace id as a backup.
        // Codex P2: pipe readability + FS-watch callbacks have
        // no ordering guarantee, so claim-by-prompt protects the
        // race window.
        daemon.claimLocalPrompt(text)
        s.startLocal(prompt: text, dryRun: dryRun) { [weak daemon, prompt = text] traceId in
            daemon?.reserveLocalTrace(traceId, prompt: prompt)
        }
        daemon.addLocal(session: s)
        composerText = ""
    }

    private func refreshStatus() {
        Task { await refreshStatusAsync() }
    }
    private func refreshStatusAsync() async {
        guard let bin = env.binaryPath else { return }
        statusBlob = await StatusProbe.fetch(binary: bin)
    }
}

/// One session's full block: bubbles + footer.
private struct SessionBlock: View {
    @ObservedObject var session: RunSession
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(session.turns) { t in
                BubbleView(turn: t)
            }
            SessionFooter(session: session)
        }
        .padding(.bottom, 8)
    }
}
