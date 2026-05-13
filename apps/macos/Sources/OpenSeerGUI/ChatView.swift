import SwiftUI

/// Three-pane layout: status sidebar | session list | active chat.
/// Each session is its own conversation thread; the session list
/// lets the user jump between past local runs and daemon-spawned
/// (Telegram) traces. Submitting a prompt always creates a NEW
/// session and selects it (one OpenSeer task = one session).
struct ChatView: View {
    @EnvironmentObject var env: OpenSeerEnv
    @ObservedObject var controller: MainController
    @State private var showSettings: Bool = false
    @State private var statusBlob: SystemStatus? = nil
    @State private var composerText: String = ""

    var body: some View {
        // HSplitView is AppKit-backed: each child gets a real
        // draggable divider, and the layout respects min widths
        // even when the user resizes the window aggressively.
        HSplitView {
            Sidebar(daemon: controller.daemon,
                    showSettings: $showSettings,
                    statusBlob: $statusBlob,
                    refreshStatus: refreshStatus)
                .frame(minWidth: 200, idealWidth: 240, maxWidth: 320)
            SessionListView(daemon: controller.daemon,
                            selectedID: $controller.selectedThreadID,
                            onNew: { controller.selectedThreadID = nil })
                .frame(minWidth: 200, idealWidth: 260, maxWidth: 420)
            detailPane
                .frame(minWidth: 360)
        }
        .task { await refreshStatusAsync() }
        .onChange(of: controller.daemon.threads.count) { _, _ in
            // Auto-select the most-recently-active thread on first
            // launch / right after a new run lands.
            controller.selectNewestThreadIfNeeded()
        }
        .sheet(isPresented: $showSettings) {
            SettingsSheet(statusBlob: $statusBlob,
                          onClose: { showSettings = false; refreshStatus() })
        }
    }

    private var detailPane: some View {
        VStack(spacing: 0) {
            transcript
            Divider()
            composer
        }
    }

    private var selectedThread: ChatThread? {
        controller.selectedThread
    }
    private var selectedRunningRun: RunSession? {
        controller.selectedRunningRun
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if let t = selectedThread {
                    ThreadBlock(thread: t, controller: controller)
                        .padding(16)
                        .id(t.id)
                } else {
                    emptyState
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .padding(.vertical, 60)
                }
            }
            .onChange(of: totalTurnCount) { _, _ in
                if let t = selectedThread {
                    withAnimation { proxy.scrollTo(t.id, anchor: .bottom) }
                }
            }
        }
    }
    private var totalTurnCount: Int {
        selectedThread?.runs.reduce(0) { $0 + $1.turns.count } ?? 0
    }

    /// Composer caption text — adapts to whether the next Send will
    /// continue the selected local thread or spawn a fresh one.
    private var composerHint: String {
        if let t = selectedThread, t.kind == .local {
            return "⌘↩ to send · continuing this conversation · "
                + "Compose ✏️ to start fresh"
        }
        return "⌘↩ to send · starts a new conversation"
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("Type a task below to start a new session.")
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
                if let s = selectedRunningRun {
                    PressFeedbackButton(systemImage: "stop.fill",
                                        tint: .red,
                                        help: "Stop the active task") {
                        s.cancel()
                    }
                }
                PressFeedbackButton(systemImage: "paperplane.fill",
                                    tint: .accentColor,
                                    help: "Send",
                                    disabled: composerText
                                        .trimmingCharacters(in: .whitespaces)
                                        .isEmpty) {
                    submit()
                }
                .keyboardShortcut(.return, modifiers: [.command])
            }
            HStack {
                Toggle("Dry run (preview only)", isOn: $controller.dryRun)
                    .controlSize(.small)
                    .toggleStyle(.switch)
                Toggle("Background (no focus)",
                       isOn: $controller.backgroundMode)
                    .controlSize(.small)
                    .toggleStyle(.switch)
                    .help("Prefer AppleScript browser automation; the agent won't move your mouse or steal focus. Works for browser-only tasks.")
                Spacer()
                Text(composerHint)
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(12)
    }

    private func submit() {
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        controller.submitTextPrompt(text)
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

/// Bordered-prominent style icon button with a uniform 28×28 square
/// hit area so Send + Stop visually match. Custom press feedback:
/// 0.25 opacity dip on press + a brief pulse on release so the user
/// always sees the click registered, even when the underlying
/// action is async (subprocess kill, sentinel write, etc.).
private struct PressFeedbackButton: View {
    let systemImage: String
    let tint: Color
    var help: String = ""
    var disabled: Bool = false
    let action: () -> Void
    @State private var pulsing: Bool = false

    var body: some View {
        Button {
            action()
            // Quick pulse so the click feels acknowledged.
            pulsing = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
                pulsing = false
            }
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 14, weight: .semibold))
                .frame(width: 28, height: 28)
        }
        .buttonStyle(PressDimStyle(tint: tint, pulsing: pulsing))
        .disabled(disabled)
        .help(help)
    }
}

private struct PressDimStyle: ButtonStyle {
    let tint: Color
    let pulsing: Bool
    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed || pulsing
        return configuration.label
            .foregroundStyle(.white)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(tint.opacity(pressed ? 0.55 : 1.0))
            )
            .scaleEffect(pressed ? 0.94 : 1.0)
            .animation(.easeOut(duration: 0.12), value: pressed)
    }
}

/// Render a thread as a continuous chat: every run's turns are
/// rendered inline, separated by a small footer chip noting the
/// run's status. The most recent run sits at the bottom; the
/// composer below feeds the SAME thread (Telegram side) — for
/// local runs each Send still spawns a fresh thread.
private struct ThreadBlock: View {
    @ObservedObject var thread: ChatThread
    @ObservedObject var controller: MainController

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(thread.sortedRuns) { run in
                RunBlock(run: run,
                         showFooter: thread.runs.count > 1
                            || run.status != .done,
                         controller: controller)
            }
        }
    }
}

/// One run's bubbles + (optional) footer. Renders the lesson chip
/// after the last turn so the post-run skill proposal lands directly
/// under the answer bubble in the conversation flow, not as a
/// separate UI surface on the orb. (The orb used to show a chip too;
/// chat-bubble placement makes the suggestion discoverable when the
/// user is looking at the conversation rather than the floating
/// orb.)
private struct RunBlock: View {
    @ObservedObject var run: RunSession
    let showFooter: Bool
    @ObservedObject var controller: MainController

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(run.turns) { t in
                BubbleView(turn: t)
            }
            LessonBubble(
                run: run,
                onApply: { controller.applyPendingLesson(on: run) },
                onDiscard: { controller.discardPendingLesson(on: run) })
            if showFooter {
                SessionFooter(session: run)
            }
        }
    }
}
