import AppKit
import SwiftUI

/// Snapshot of the current turn that the orb displays as a live step
/// bubble while a task is running. Driven from `RunSession.turns.last`
/// by FloatingVoiceOrbWindow's observer; flat value-type so SwiftUI
/// diffing doesn't need to chase nested ObservableObjects.
struct LiveStepInfo: Equatable {
    let step: Int
    let reflection: String     // [SUCCESS] / [INEFFECTIVE] / [REGRESSED] / [THINKING] / ""
    let thought: String
    let action: String?        // last executed action's summary
    let isFailed: Bool          // task_failed → render as red
}

struct VoiceOrbView: View {
    let isTaskRunning: Bool
    /// Subset of `isTaskRunning` that says the run is parked
    /// waiting for the user to release it (HOLD sentinel set on
    /// disk). True only when isTaskRunning is also true.
    let isTaskHeld: Bool
    let spokenAnswer: String?
    let liveStep: LiveStepInfo?
    let onSubmit: (String) -> Void
    let onAnswerConsumed: () -> Void
    /// Toggle the hand-off: if running → hold; if held → resume.
    let onHoldToggle: () -> Void
    /// Stop the running task entirely (CANCEL sentinel + asyncio
    /// task cancel). Visible only while a task is in progress.
    let onStop: () -> Void
    @Binding var isWindowExpanded: Bool

    @AppStorage("voiceLocale") private var voiceLocale = "zh-CN"
    // Voice input is opt-in. Default off so a fresh install doesn't
    // auto-fail on missing microphone / Speech Recognition perms when
    // the user just wants to type. Settings → General → Voice flips
    // this on; the orb's hotkey then summons with the mic listening.
    @AppStorage("voiceEnabled") private var voiceEnabled = false
    @StateObject private var input = VoiceInput()
    @State private var isOpen = false
    @State private var autoListen = false
    @State private var utteranceTask: Task<Void, Never>? = nil
    @State private var commitTask: Task<Void, Never>? = nil
    @State private var lastSubmitted = ""
    @State private var lastSubmittedAt = Date.distantPast
    @State private var displayedAnswer: String? = nil
    @State private var unexpectedRestartAt: Date = .distantPast
    @State private var unexpectedRestartBurst: Int = 0
    /// What the user sees and edits in the transcript area. Sourced
    /// from `input.transcript` while the user hasn't typed anything
    /// different yet; once they edit, we stop overwriting and the
    /// edited string is what Send / autoCommit submits.
    @State private var draftText: String = ""
    /// Last transcript value we copied into `draftText`. We treat
    /// the buffer as "user-edited" iff `draftText != lastSeenTranscript`
    /// — that's how we detect whether to keep overwriting from
    /// SFSpeech (no) or leave the user's edits alone (yes).
    @State private var lastSeenTranscript: String = ""
    /// True while the user is focused on the editable transcript
    /// field; autoCommit is paused so a 4.5s pause-to-think doesn't
    /// auto-submit mid-edit.
    @FocusState private var isEditingDraft: Bool
    private let autoCommitDelayNs: UInt64 = 4_500_000_000
    private let finalTranscriptTimeoutS: Double = 3.0
    /// Anti-storm gate for orb-driven restarts after SFSpeech aborts
    /// the session on its own. If the recognizer is permanently in a
    /// bad state we don't want to flap forever; cap at 4 quick
    /// retries within 30 seconds, then back off.
    private let unexpectedRestartMaxBurst: Int = 4
    private let unexpectedRestartBurstWindowS: Double = 30
    private let unexpectedRestartDelayNs: UInt64 = 1_200_000_000

    var body: some View {
        VStack(alignment: .trailing, spacing: 10) {
            // All non-orb chrome (panel, step bubble, answer bubble)
            // is gated on `isOpen` so a single tap on the crystal
            // ball collapses everything back to just-the-orb. Tap
            // again to bring the panel + any pending bubbles back.
            if isOpen {
                panel
                if isTaskRunning, let liveStep {
                    stepBubble(liveStep)
                }
                if let displayedAnswer, !displayedAnswer.isEmpty {
                    answerBubble(displayedAnswer)
                }
                // Skill chip lives in the main chat thread now
                // (LessonBubble in BubbleView.swift), not on the
                // orb — the user expected to see "Learned…" inline
                // under the answer in the conversation, not as a
                // separate floating affordance.
            }
            Button {
                withAnimation(.spring(response: 0.26, dampingFraction: 0.86)) {
                    isOpen.toggle()
                    isWindowExpanded = isOpen
                }
                // Tapping the crystal ball toggles the panel.
                // Starting the listen loop on every open ignored the
                // voiceEnabled toggle (codex P2 on 414526d); now we
                // only kick the loop when voice is opted-in.
                // Otherwise, focus the draft TextEditor so the user
                // can start typing immediately — type-only mode is
                // the default path.
                if isOpen {
                    if voiceEnabled {
                        startLoop()
                    } else {
                        isEditingDraft = true
                    }
                } else {
                    stopLoop()
                }
            } label: {
                CrystalOrb(active: input.isRecording,
                           isOpen: isOpen)
            }
            .buttonStyle(.plain)
            .help(isOpen ? "Close voice mode" : "Open voice mode")
        }
        .onAppear {
            input.configure(localeID: voiceLocale)
            isWindowExpanded = isOpen
        }
        .onChange(of: liveStep) { _, _ in
            isWindowExpanded = isOpen
        }
        .onChange(of: voiceLocale) { _, newValue in
            input.configure(localeID: newValue)
        }
        .onChange(of: voiceEnabled) { _, enabled in
            // Toggling voice off in Settings while the mic is
            // already recording must stop the loop — otherwise the
            // Pause button greys out (its `.disabled` now includes
            // `!voiceEnabled`) and the user has no UI control to
            // end the in-flight SFSpeech session. Codex P2 caught
            // this on the v0.1.9 tag push. Toggling back on does
            // NOT auto-start; the user explicitly Listens or
            // re-summons via the hotkey.
            if !enabled && autoListen {
                stopLoop()
            }
        }
        .onChange(of: input.partialTick) { _, _ in
            // Reset the autoCommit timer on every partial — including
            // partials that repeat the same string. Watching transcript
            // here would miss those, and a pause-to-think would commit
            // mid-utterance (codex P1).
            //
            // Pause auto-commit while the user is editing the text
            // field — otherwise a slow 4.5s edit window would
            // auto-submit before the user clicks Send.
            guard autoListen, !input.transcript.isEmpty,
                  !isEditingDraft else { return }
            scheduleCommit()
        }
        .onChange(of: input.transcript) { _, new in
            // Track the recognizer's transcript into the editable
            // buffer UNLESS the user has already taken over by
            // typing/editing. Once draftText diverges from what we
            // last copied (lastSeenTranscript), SFSpeech doesn't
            // get to clobber the user's edits.
            if draftText == lastSeenTranscript {
                draftText = new
            }
            lastSeenTranscript = new
        }
        .onChange(of: input.unexpectedStopTick) { _, _ in
            // SFSpeech aborted its session on its own (cooldown err,
            // audio dropout, …). `isTaskRunning` already fired its
            // post-task restart, so we have to re-arm here. Burst-
            // limited so a permanently broken recognizer can't pin
            // the CPU.
            guard autoListen, !isTaskRunning else { return }
            let now = Date()
            if now.timeIntervalSince(unexpectedRestartAt)
                > unexpectedRestartBurstWindowS {
                unexpectedRestartBurst = 0
            }
            guard unexpectedRestartBurst < unexpectedRestartMaxBurst else {
                return
            }
            unexpectedRestartBurst += 1
            unexpectedRestartAt = now
            // Delay slightly — restarting in the same runloop tick as
            // the abort error often re-aborts immediately.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: unexpectedRestartDelayNs)
                if autoListen && !isTaskRunning {
                    startListeningIfIdle()
                }
            }
        }
        .onChange(of: isTaskRunning) { _, running in
            // When a task starts executing the mic goes silent. The
            // user must press Listen to re-engage if they want to
            // interrupt; pressing it and speaking triggers the same
            // barge-in path that's always existed
            // (MainController.submitPrompt cancels the in-flight run
            // and re-calls the LLM with the new message + the
            // interrupted run's state in the session context). The
            // old "stay listening through the run" behavior caused
            // accidental commits — coughing, the user's own thinking-
            // out-loud, hands-free chatter — to silently kill running
            // tasks.
            //
            // Stay off after the task finishes too; only the explicit
            // Listen button re-arms the loop.
            if running {
                stopLoop()
            }
            isWindowExpanded = isOpen
        }
        .onReceive(NotificationCenter.default.publisher(
            for: .openVoiceOrb)) { _ in
            // Global control+S fired (or any other code path
            // wants the orb visible + listening). Always open the
            // panel; only kick the listen loop when voice is
            // enabled in Settings — otherwise the orb summons in
            // type-only mode and the user can focus the draft
            // field directly.
            if !isOpen {
                withAnimation(.spring(response: 0.26,
                                       dampingFraction: 0.86)) {
                    isOpen = true
                    isWindowExpanded = true
                }
            }
            if voiceEnabled && !autoListen {
                startLoop()
            } else if !voiceEnabled {
                // Type-only mode: drop the user straight into the
                // TextEditor so the very next keystroke lands in the
                // draft buffer (codex P2 on 414526d). Without this,
                // pressing ⌃S in default-off mode opened the panel
                // but left focus elsewhere, and the user had to
                // click into the editor before typing.
                isEditingDraft = true
            }
        }
        .onReceive(NotificationCenter.default.publisher(
            for: .dismissVoiceOrb)) { _ in
            // Hotkey-toggle dismissal: stop listening + collapse
            // the open-state so SFSpeech doesn't keep recording
            // after the panel is hidden. Match the same teardown
            // path the X button uses (stopLoop + isOpen=false).
            stopLoop()
            if isOpen {
                withAnimation(.easeOut(duration: 0.16)) {
                    isOpen = false
                    isWindowExpanded = false
                }
            }
        }
        .onChange(of: spokenAnswer) { _, answer in
            guard let answer, !answer.isEmpty else { return }
            // Auto-open the panel so the new answer is immediately
            // visible — otherwise a collapsed orb would silently
            // accumulate answers the user never notices.
            withAnimation(.spring(response: 0.24, dampingFraction: 0.9)) {
                displayedAnswer = answer
                if !isOpen { isOpen = true }
                isWindowExpanded = true
            }
            if input.isRecording { input.stop() }
            onAnswerConsumed()
            if autoListen { startListeningIfIdle() }
        }
    }

    private var panel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                // Header glyph + title reflect the active mode.
                // Voice off → keyboard / "Prompt" so the panel
                // doesn't pretend it's still a voice surface; voice
                // on → waveform / "Voice" with the red recording
                // tint when the mic is actually live.
                Image(systemName: voiceEnabled
                      ? "waveform.circle.fill"
                      : "keyboard")
                    .foregroundStyle(voiceEnabled
                                      ? (input.isRecording ? .red : .accentColor)
                                      : .secondary)
                Text(voiceEnabled ? "Voice" : "Prompt")
                    .font(.headline)
                Spacer()
                Button {
                    stopLoop()
                    withAnimation(.spring(response: 0.24, dampingFraction: 0.9)) {
                        isOpen = false
                        // All non-orb chrome is gated on isOpen now,
                        // so the window must collapse with it —
                        // leaving expanded:true would keep a 360x640
                        // invisible NSPanel intercepting clicks.
                        isWindowExpanded = false
                    }
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("Close")
            }

            Text(statusText)
                .font(.caption)
                .foregroundStyle(statusColor)
                .lineLimit(2)

            // Editable transcript area. SFSpeech writes into draftText
            // via .onChange(of: input.transcript); the user can fix
            // recognition errors before hitting Send. Once they edit,
            // auto-overwrite stops (see the onChange handler).
            //
            // IME (Pinyin / Kotoeri / etc.) gotcha: the orb's panel
            // is a `.nonactivatingPanel`, so a click into TextEditor
            // makes the panel key without activating our app, and
            // macOS's input method server attaches its candidate
            // window to the *active* app — which is whatever was
            // foreground before. Visible symptom: Chinese typing
            // works, but the candidate popup is invisible (you
            // can't pick characters). Activating the app when the
            // editor gains focus pulls the IME panel onto our text
            // view; when the user dismisses focus, the foreground
            // app gets its IME back naturally.
            // TextField with `axis: .vertical` grows for multi-line
            // drafts. Plain Return would normally insert a newline
            // (codex P2: `.onSubmit` doesn't fire on multiline
            // TextField), so we intercept Return ourselves via
            // `.onKeyPress`. CJK IME composition is unaffected —
            // the input method consumes candidate-confirm Returns
            // before SwiftUI's key-press pipeline sees them, so
            // commitNow only fires after composition ends. Shift+
            // Return falls through to insert a newline as usual.
            TextField(transcriptText, text: $draftText,
                      axis: .vertical)
                .textFieldStyle(.plain)
                .font(.callout)
                .focused($isEditingDraft)
                .lineLimit(2...5)
                .padding(8)
                .background(.background.secondary)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .onChange(of: isEditingDraft) { _, focused in
                    if focused {
                        NSApp.activate(ignoringOtherApps: true)
                    }
                }
                .onKeyPress(phases: .down) { press in
                    // Plain Return → send. Only CHORD modifiers
                    // (Shift / Option / Command / Control) block
                    // the send so the user can still get a newline
                    // via Shift+Return and the other chords stay
                    // available for system-level use. Caps Lock is
                    // explicitly stripped because macOS reports it
                    // in the modifier set whenever it's engaged,
                    // and a user typing in ALL CAPS still expects
                    // Return to send (codex P2 on c5ec59a).
                    let chord = press.modifiers
                        .subtracting(.capsLock)
                    guard press.key == .return,
                          chord.isEmpty else {
                        return .ignored
                    }
                    commitNow()
                    return .handled
                }

            HStack(spacing: 8) {
                // Listen / Pause is only shown when voice is enabled
                // in Settings. With voice off the button has no role
                // — it could never start a session and just took up
                // visual space next to Undo / Send, contradicting
                // the "Prompt" header + type-mode copy.
                if voiceEnabled {
                    Button {
                        autoListen ? stopLoop() : startLoop()
                    } label: {
                        Label(autoListen ? "Pause" : "Listen",
                              systemImage: autoListen ? "pause.fill" : "mic.fill")
                    }
                    .controlSize(.small)
                    .disabled(!input.isAvailable || input.isStarting)
                    .help("Press to start / stop listening.")
                }

                // Hand-off: pause/resume the agent so the user can
                // drive the mouse + keyboard themselves for a few
                // steps. Only relevant while there's an active task.
                if isTaskRunning {
                    Button {
                        onHoldToggle()
                    } label: {
                        Label(isTaskHeld ? "Resume" : "Hand off",
                              systemImage: isTaskHeld
                                ? "play.fill" : "hand.raised.fill")
                    }
                    .controlSize(.small)
                    .help(isTaskHeld
                          ? "Resume the agent; it'll re-read state on the next step."
                          : "Pause the agent so you can use the mouse/keyboard yourself.")

                    // Stop: ends the current task entirely. Different
                    // from Hand off (which only pauses) — Stop is
                    // when the agent's going off the rails and we
                    // want it to give up.
                    Button(role: .destructive) {
                        onStop()
                    } label: {
                        Label("Stop", systemImage: "stop.fill")
                    }
                    .controlSize(.small)
                    .help("End this task. The agent writes a terminate(fail) and exits cleanly.")
                }

                // Undo: one-tap "撤销刚才那一步". Submits a fixed
                // prompt; the agent looks at the most recent
                // producing step in session context and emits the
                // inverse (cmd+z for type, cmd+[ for click-that-
                // navigated, ask_user when ambiguous — see the
                // "Undoing the previous step" block in the system
                // prompt).
                Button {
                    onSubmit("撤销刚才那一步")
                } label: {
                    Label("Undo", systemImage: "arrow.uturn.backward")
                }
                .controlSize(.small)
                .help("Ask the agent to revert the last action it took.")

                Button {
                    commitNow()
                } label: {
                    Label("Send", systemImage: "paperplane.fill")
                }
                .controlSize(.small)
                // No keyboard shortcut on the orb's Send button —
                // the TextField above already submits on plain ↩
                // via `.onSubmit`. Keeping a redundant ⌃↩ shortcut
                // would just leak through when the editor isn't
                // focused and cause stray sends.
                .disabled(draftText
                    .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(12)
        .frame(width: 320)
        .background(.regularMaterial)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(.white.opacity(0.22), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.18), radius: 18, y: 8)
    }

    private func stepBubble(_ info: LiveStepInfo) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.7)
                Text("step \(info.step)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if !info.reflection.isEmpty {
                    Text(info.reflection)
                        .font(.caption2.monospaced())
                        .foregroundStyle(reflectionColor(info.reflection))
                }
                Spacer(minLength: 0)
            }
            if !info.thought.isEmpty {
                ScrollView(.vertical, showsIndicators: true) {
                    Text(info.thought)
                        .font(.callout)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 90)
            }
            if let action = info.action, !action.isEmpty {
                Text("→ \(action)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
                    .textSelection(.enabled)
            }
        }
        .padding(11)
        .frame(width: 320, alignment: .leading)
        .background(.regularMaterial)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(
                    info.isFailed
                        ? Color.red.opacity(0.35)
                        : Color.accentColor.opacity(0.20),
                    lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.16), radius: 14, y: 7)
    }

    private func reflectionColor(_ r: String) -> Color {
        if r.contains("SUCCESS") { return .green }
        if r.contains("REGRESSED") { return .red }
        if r.contains("INEFFECTIVE") { return .orange }
        return .secondary
    }

    private func answerBubble(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text("OpenSeer")
                    .font(.caption.weight(.semibold))
                Spacer()
                Button {
                    withAnimation(.easeOut(duration: 0.16)) {
                        displayedAnswer = nil
                        isWindowExpanded = isOpen
                    }
                } label: {
                    Image(systemName: "xmark")
                        .font(.caption2.weight(.bold))
                }
                .buttonStyle(.borderless)
                .help("Dismiss")
            }
            ScrollView(.vertical, showsIndicators: true) {
                Text(text)
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 160)
        }
        .padding(11)
        .frame(width: 320, alignment: .leading)
        .background(.regularMaterial)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.accentColor.opacity(0.25), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.16), radius: 14, y: 7)
    }

    private var transcriptText: String {
        if !input.transcript.isEmpty { return input.transcript }
        if isTaskHeld {
            return "Paused — you have the mouse. Press Resume when done."
        }
        if isTaskRunning {
            return input.isRecording
                ? "Listening… speak to interrupt."
                : "OpenSeer is working…"
        }
        // Type-only mode: voice opt-in is off, so the user is going
        // to type. Plain Return now submits via the TextField's
        // .onSubmit; the placeholder just nudges them to start.
        if !voiceEnabled {
            return "Type your prompt."
        }
        if input.isAvailable { return "Listening starts when voice mode opens." }
        return "Voice input is unavailable in this runtime."
    }

    private var statusText: String {
        if isTaskHeld {
            return "Hand-off — you drive. Resume when ready."
        }
        if isTaskRunning {
            return input.isRecording
                ? "Working — speak to interrupt and re-call."
                : "Working. Voice will resume when the task ends."
        }
        if input.isRecording { return "Listening. Pause briefly to send." }
        if input.isStarting { return "Starting microphone..." }
        if let err = input.error { return err }
        // Same type-only mode override — without it the header
        // promises voice behavior the orb won't actually perform.
        if !voiceEnabled {
            return "Type a prompt. Enable voice in Settings → General → Voice."
        }
        return "Speak naturally. OpenSeer sends after a short pause."
    }

    private var statusColor: Color {
        if input.error != nil { return .orange }
        if isTaskHeld { return .yellow }
        if input.isRecording { return .red }
        if isTaskRunning { return .accentColor }
        // Type-only mode reads in a quieter tone — no red mic, no
        // "voice ready" accent.
        if !voiceEnabled { return .secondary }
        return .secondary
    }

    private func startLoop() {
        guard input.isAvailable else { return }
        autoListen = true
        startListeningIfIdle()
    }

    private func stopLoop() {
        autoListen = false
        utteranceTask?.cancel()
        commitTask?.cancel()
        utteranceTask = nil
        commitTask = nil
        if input.isRecording { input.stop() }
    }

    private func startListeningIfIdle() {
        // No `!isTaskRunning` guard: barge-in keeps the mic open while
        // a task is running so the next utterance can interrupt it
        // and re-call the LLM with the new message. The committed
        // commit path will cancel the in-flight run via
        // MainController.submitPrompt.
        guard autoListen else { return }
        guard !input.isRecording, !input.isStarting else { return }
        input.configure(localeID: voiceLocale)
        Task { await input.start() }
    }

    private func scheduleCommit() {
        utteranceTask?.cancel()
        utteranceTask = Task {
            // CRITICAL: don't use `try?` here. `try?` swallows the
            // CancellationError that `Task.sleep` throws when the
            // task is cancelled, then commitNow() runs anyway —
            // every reschedule (and we reschedule on every partial,
            // ~10 Hz) immediately commits the in-flight one. That's
            // the "speak one syllable, instantly sends" bug.
            do {
                try await Task.sleep(nanoseconds: autoCommitDelayNs)
            } catch {
                return
            }
            await MainActor.run { commitNow() }
        }
    }

    private func commitNow() {
        utteranceTask?.cancel()
        utteranceTask = nil
        commitTask?.cancel()
        // Snapshot the user's draft right now. We still call
        // stopAndAwaitFinal to drain the SFSpeech post-roll; if the
        // user hasn't manually edited (draftText was tracking
        // input.transcript), the post-roll lands into draftText via
        // .onChange(of: input.transcript). If they HAVE edited, we
        // keep their version.
        // Remember whether the user had already diverged from
        // SFSpeech's transcript at submit time. Need this AFTER
        // the await — by then draftText may have been updated by
        // onChange (if no divergence) or might still hold the
        // user's edit (if there was divergence).
        let userEdited = draftText != lastSeenTranscript
        let userDraft = draftText
        commitTask = Task { @MainActor in
            let finalFromSpeech = await input.stopAndAwaitFinal(
                timeout: finalTranscriptTimeoutS)
            // Picking the right text after the await:
            //   - if the user manually edited, their draft wins
            //     (SwiftUI's onChange will NOT overwrite a
            //     user-edited draftText, by design)
            //   - otherwise prefer SFSpeech's post-roll final
            //     directly (codex P2 fix: don't rely on the
            //     onChange having propagated before this
            //     continuation resumes — it isn't guaranteed in
            //     the same MainActor turn, so the final
            //     syllable would otherwise drop)
            let candidate: String
            if userEdited {
                candidate = userDraft
            } else if !finalFromSpeech.isEmpty {
                candidate = finalFromSpeech
            } else {
                candidate = draftText.isEmpty ? userDraft : draftText
            }
            let text = candidate
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let repeatedTooSoon = text == lastSubmitted
                && Date().timeIntervalSince(lastSubmittedAt) < 2.5
            guard !text.isEmpty, !repeatedTooSoon else {
                // Empty / repeat: don't re-arm. The user can press
                // Listen again if they meant to try once more.
                return
            }
            lastSubmitted = text
            lastSubmittedAt = Date()
            displayedAnswer = nil
            // Clear the editable buffer + the tracking marker so
            // the next utterance starts from scratch.
            draftText = ""
            lastSeenTranscript = ""
            input.resetTranscript()
            isEditingDraft = false
            onSubmit(text)
            // The submit either started a fresh task or triggered
            // barge-in on a running one. Either way a task is now
            // executing — match the "mic off during execution"
            // policy explicitly. If isTaskRunning was already true
            // (barge-in path), the onChange in body never sees a
            // false→true edge and wouldn't call stopLoop on its own,
            // so we do it here. The user must press Listen again to
            // re-engage. (codex P2 from 6c7c55e.)
            stopLoop()
        }
    }
}

private struct CrystalOrb: View {
    let active: Bool
    let isOpen: Bool

    var body: some View {
        ZStack {
            Circle()
                .fill(
                    LinearGradient(
                        colors: [
                            Color(red: 0.58, green: 0.50, blue: 0.99),
                            Color(red: 0.50, green: 0.57, blue: 0.98),
                            Color(red: 0.39, green: 0.77, blue: 0.86),
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
            Circle()
                .stroke(.white.opacity(0.72), lineWidth: 1.2)
            Ellipse()
                .fill(
                    LinearGradient(
                        colors: [
                            .white.opacity(0.58),
                            .white.opacity(0.20),
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: 30, height: 22)
                .rotationEffect(.degrees(-6))
                .offset(x: -14, y: -12)
            Circle()
                .fill(.white.opacity(0.88))
                .frame(width: 6, height: 6)
                .offset(x: 16, y: -16)
            Circle()
                .fill(.white.opacity(0.70))
                .frame(width: 3.5, height: 3.5)
                .offset(x: 23, y: -7)
            Circle()
                .fill(.white.opacity(0.56))
                .frame(width: 3.5, height: 3.5)
                .offset(x: -22, y: 19)
            micBadge
            if active {
                Circle()
                    .stroke(.white.opacity(0.82), lineWidth: 2.5)
                    .scaleEffect(1.10)
            }
        }
        .frame(width: 66, height: 66)
        .shadow(color: .black.opacity(0.22), radius: 12, y: 6)
    }

    private var micBadge: some View {
        ZStack {
            Circle()
                .fill(.black.opacity(0.24))
            Image(systemName: isOpen ? "waveform" : "mic.fill")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.white)
        }
        .frame(width: 20, height: 20)
        .offset(x: 22, y: 22)
    }
}
