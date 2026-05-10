import AppKit
import SwiftUI

/// Snapshot of the current turn that the orb displays as a live step
/// bubble while a task is running. Driven from `RunSession.turns.last`
/// by FloatingVoiceOrbWindow's observer; flat value-type so SwiftUI
/// diffing doesn't need to chase nested ObservableObjects.
struct LiveStepInfo: Equatable {
    let step: Int
    let reflection: String     // [SUCCESS] / [INEFFECTIVE] / [REGRESSED] / [N/A] / ""
    let thought: String
    let action: String?        // last executed action's summary
    let isFailed: Bool          // task_failed → render as red
}

struct VoiceOrbView: View {
    let isTaskRunning: Bool
    let spokenAnswer: String?
    let liveStep: LiveStepInfo?
    let onSubmit: (String) -> Void
    let onAnswerConsumed: () -> Void
    @Binding var isWindowExpanded: Bool

    @AppStorage("voiceLocale") private var voiceLocale = "zh-CN"
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
            if isOpen { panel }
            // While a task is running we show a live step bubble — the
            // model's current thought + last action + reflection state
            // — so the user gets the same "OpenSeer 正在做 X" feedback
            // they'd see in the main chat window. The bubble is hidden
            // once the task ends; the answerBubble takes over.
            if isTaskRunning, let liveStep {
                stepBubble(liveStep)
            }
            if let displayedAnswer, !displayedAnswer.isEmpty {
                answerBubble(displayedAnswer)
            }
            Button {
                withAnimation(.spring(response: 0.26, dampingFraction: 0.86)) {
                    isOpen.toggle()
                    isWindowExpanded = isOpen || displayedAnswer != nil
                }
                isOpen ? startLoop() : stopLoop()
            } label: {
                CrystalOrb(active: input.isRecording,
                           isOpen: isOpen)
            }
            .buttonStyle(.plain)
            .help(isOpen ? "Close voice mode" : "Open voice mode")
        }
        .onAppear {
            input.configure(localeID: voiceLocale)
            isWindowExpanded = isOpen || displayedAnswer != nil
                || (isTaskRunning && liveStep != nil)
        }
        .onChange(of: liveStep) { _, _ in
            isWindowExpanded = isOpen || displayedAnswer != nil
                || (isTaskRunning && liveStep != nil)
        }
        .onChange(of: voiceLocale) { _, newValue in
            input.configure(localeID: newValue)
        }
        .onChange(of: input.partialTick) { _, _ in
            // Reset the autoCommit timer on every partial — including
            // partials that repeat the same string. Watching transcript
            // here would miss those, and a pause-to-think would commit
            // mid-utterance (codex P1).
            guard autoListen, !input.transcript.isEmpty else { return }
            scheduleCommit()
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
            // Barge-in: keep the mic open while a task is running so
            // the user can interrupt. The next autoCommit (or Send
            // button) calls back into MainController.submitPrompt,
            // which cancels the in-flight run and re-calls the LLM
            // with the new message + the interrupted run's state in
            // the session context.
            //
            // When a task finishes (running -> false) and the mic
            // *isn't* recording (e.g. user paused it manually), the
            // existing auto-restart still kicks back in.
            if !running && autoListen && !input.isRecording {
                startListeningIfIdle()
            }
            isWindowExpanded = isOpen || displayedAnswer != nil
                || (isTaskRunning && liveStep != nil)
        }
        .onChange(of: spokenAnswer) { _, answer in
            guard let answer, !answer.isEmpty else { return }
            withAnimation(.spring(response: 0.24, dampingFraction: 0.9)) {
                displayedAnswer = answer
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
                Image(systemName: "waveform.circle.fill")
                    .foregroundStyle(input.isRecording ? .red : .accentColor)
                Text("Voice")
                    .font(.headline)
                Spacer()
                Button {
                    stopLoop()
                    withAnimation(.spring(response: 0.24, dampingFraction: 0.9)) {
                        isOpen = false
                        isWindowExpanded = displayedAnswer != nil
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

            ScrollView(.vertical, showsIndicators: true) {
                Text(transcriptText)
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            // Fixed height: each partial would otherwise reflow the
            // outer panel and the orb would visibly hop. Internal
            // ScrollView handles overflow.
            .frame(height: 70)
            .padding(10)
            .background(.background.secondary)
            .clipShape(RoundedRectangle(cornerRadius: 8))

            HStack(spacing: 8) {
                Button {
                    autoListen ? stopLoop() : startLoop()
                } label: {
                    Label(autoListen ? "Pause" : "Listen",
                          systemImage: autoListen ? "pause.fill" : "mic.fill")
                }
                .controlSize(.small)
                .disabled(!input.isAvailable || input.isStarting)

                Button {
                    commitNow()
                } label: {
                    Label("Send", systemImage: "paperplane.fill")
                }
                .controlSize(.small)
                .disabled(input.transcript
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
        if isTaskRunning {
            return input.isRecording
                ? "Listening… speak to interrupt."
                : "OpenSeer is working…"
        }
        if input.isAvailable { return "Listening starts when voice mode opens." }
        return "Voice input is unavailable in this runtime."
    }

    private var statusText: String {
        if isTaskRunning {
            return input.isRecording
                ? "Working — speak to interrupt and re-call."
                : "Working. Voice will resume when the task ends."
        }
        if input.isRecording { return "Listening. Pause briefly to send." }
        if input.isStarting { return "Starting microphone..." }
        if let err = input.error { return err }
        return "Speak naturally. OpenSeer sends after a short pause."
    }

    private var statusColor: Color {
        if input.error != nil { return .orange }
        if input.isRecording { return .red }
        if isTaskRunning { return .accentColor }
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
        let snapshot = input.transcript
        // Wait for SFSpeech's actual `isFinal` callback rather than a
        // fixed sleep — partial transcripts routinely miss the last
        // word(s) until the post-roll lands ~1–3s after endAudio.
        // 3s safety net; fall back to the partial snapshot if the
        // final never arrives.
        commitTask = Task { @MainActor in
            let finalText = await input.stopAndAwaitFinal(
                timeout: finalTranscriptTimeoutS)
            let text = (finalText.isEmpty ? snapshot : finalText)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let repeatedTooSoon = text == lastSubmitted
                && Date().timeIntervalSince(lastSubmittedAt) < 2.5
            guard !text.isEmpty, !repeatedTooSoon else {
                if autoListen { startListeningIfIdle() }
                return
            }
            lastSubmitted = text
            lastSubmittedAt = Date()
            displayedAnswer = nil
            input.resetTranscript()
            autoListen = true
            onSubmit(text)
            // Re-arm the mic immediately after submit so a follow-up
            // utterance can interrupt the new task. Previously this
            // only happened on isTaskRunning → false; with barge-in
            // we want it now.
            startListeningIfIdle()
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
