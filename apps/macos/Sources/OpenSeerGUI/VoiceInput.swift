import AVFoundation
import Foundation
import Speech

@MainActor
final class VoiceInput: NSObject, ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var isStarting = false
    @Published private(set) var transcript = ""
    @Published private(set) var error: String? = nil
    /// Bumps every time the recognizer delivers a partial (or final)
    /// result, even when the transcript string is unchanged. The orb
    /// watches this rather than `transcript` for autoCommit gating —
    /// `transcript` is silent during a pause-to-think since SFSpeech
    /// keeps emitting same-string partials, and SwiftUI's onChange
    /// doesn't fire for "no-op" updates, which made the 4.5s timer
    /// fire mid-utterance.
    @Published private(set) var partialTick: Int = 0

    /// Bumps when the recognition session ends WITHOUT a caller
    /// waiting on `stopAndAwaitFinal` — i.e. SFSpeech aborted the
    /// task on its own (cooldown error, audio dropout, system
    /// reclaim, …). The orb watches this to know it needs to
    /// re-arm: `.onChange(of: isTaskRunning)` already fired when the
    /// task transitioned to .done/.fail, so it won't fire again on
    /// this internal SFSpeech blip.
    @Published private(set) var unexpectedStopTick: Int = 0

    var isAvailable: Bool {
        guard Bundle.main.bundleIdentifier != nil else { return false }
        return recognizer?.isAvailable == true
    }

    private var recognizer: SFSpeechRecognizer?
    private var allowServerRecognition: Bool
    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    /// True when start() was invoked but the recognizer was busy /
    /// unavailable. The SFSpeechRecognizerDelegate callback retries
    /// when availability flips back to true. Without this flag, the
    /// "voice only works once" symptom appears: after a session ends
    /// the recognizer is unavailable for a few seconds, the orb's
    /// auto-restart fires within that window, start() returned with
    /// an error and never retried.
    private var pendingStart = false

    /// Continuation handed back from `stopAndAwaitFinal`. Resumed
    /// either by the recognizer's isFinal callback (preferred) or by
    /// the timeout safety net. Resumed exactly once thanks to the
    /// nil-out pattern.
    private var pendingFinal: CheckedContinuation<String, Never>?
    /// Transcript captured when stopAndAwaitFinal was called, used as
    /// fallback if the timeout fires before isFinal lands.
    private var pendingFinalSnapshot = ""

    init(locale: Locale = Locale(identifier: "zh-CN"),
         allowServerRecognition: Bool = true) {
        self.allowServerRecognition = allowServerRecognition
        super.init()
        recognizer = SFSpeechRecognizer(locale: locale) ?? SFSpeechRecognizer()
        recognizer?.delegate = self
    }

    func configure(localeID: String, allowServerRecognition: Bool = true) {
        guard !isRecording, !isStarting else { return }
        self.allowServerRecognition = allowServerRecognition
        if localeID == "system" {
            recognizer = SFSpeechRecognizer(locale: Locale.current)
                ?? SFSpeechRecognizer()
        } else {
            recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID))
                ?? SFSpeechRecognizer()
        }
        recognizer?.delegate = self
        error = nil
    }

    func start() async {
        NSLog("[voice] start() entry isStarting=%d isRecording=%d task=%d",
              isStarting ? 1 : 0, isRecording ? 1 : 0, task != nil ? 1 : 0)
        if isStarting || isRecording || task != nil {
            NSLog("[voice] start() bailed at entry guard")
            return
        }
        isStarting = true
        defer { isStarting = false }
        error = nil
        transcript = ""

        guard Bundle.main.bundleIdentifier != nil else {
            error = "Voice requires the bundled app."
            NSLog("[voice] start() no bundle id")
            return
        }
        guard let recognizer else {
            error = "Speech recognition is unavailable."
            NSLog("[voice] start() no recognizer")
            return
        }
        // SFSpeechRecognizer.isAvailable flips to false for several
        // seconds after a session ends. Actively poll instead of
        // relying solely on the delegate — `availabilityDidChange`
        // doesn't always fire reliably across rapid stop→start
        // cycles (the delegate appears to skip transient flips
        // during the SFSpeech cooldown), and a missed event leaves
        // the orb permanently silent. 8s covers the worst observed
        // cooldown; we still fall back to delegate-driven retry if
        // we hit the timeout.
        if !recognizer.isAvailable {
            NSLog("[voice] start() recognizer unavailable; polling for up to 8s")
            for i in 0..<16 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if recognizer.isAvailable {
                    NSLog("[voice] start() recognizer became available after %dms",
                          (i + 1) * 500)
                    break
                }
            }
            if !recognizer.isAvailable {
                NSLog("[voice] start() still unavailable after 8s; queued pendingStart")
                pendingStart = true
                error = nil
                return
            }
        }
        guard allowServerRecognition
                || recognizer.supportsOnDeviceRecognition == true else {
            error = "On-device speech recognition is unavailable for this locale."
            return
        }

        let speechAuth = await Self.requestSpeechAuth()
        guard speechAuth == .authorized else {
            error = "Speech Recognition permission is denied."
            return
        }
        let micAuth = await AVCaptureDevice.requestAccess(for: .audio)
        guard micAuth else {
            error = "Microphone permission is denied."
            return
        }

        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = !allowServerRecognition
        request = req

        // Reset before reuse. Without this, the second-and-after
        // recognition session on the same VoiceInput sometimes
        // produces no audio buffers — installTap silently delivers
        // empty data because the engine's render graph is in a
        // half-stopped state from the previous run. Resetting forces
        // a fresh graph. Manual orb-restart "worked" because the
        // longer human delay let the engine settle on its own; auto-
        // restart fired immediately and lost the race.
        audioEngine.reset()

        let inputNode = audioEngine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) {
            [weak req] buffer, _ in
            req?.append(buffer)
        }

        do {
            audioEngine.prepare()
            try audioEngine.start()
        } catch {
            self.error = "Couldn't start microphone: \(error)"
            NSLog("[voice] audioEngine.start() threw: %@", "\(error)")
            audioEngine.inputNode.removeTap(onBus: 0)
            request = nil
            return
        }
        NSLog("[voice] start() success, recording")

        task = recognizer.recognitionTask(with: req) { [weak self] result, err in
            guard let self else { return }
            if let result {
                let text = result.bestTranscription.formattedString
                Task { @MainActor in
                    self.transcript = text
                    // Tick on every partial so the orb's autoCommit
                    // timer treats "same partial again" as "still
                    // talking", not "user stopped".
                    self.partialTick &+= 1
                }
            }
            if err != nil || result?.isFinal == true {
                let final = result?.bestTranscription.formattedString ?? ""
                let wasError = err != nil
                if let err = err {
                    NSLog("[voice] recognitionTask err: %@", "\(err)")
                }
                Task { @MainActor in
                    self.stopAudioCapture()
                    // Promote the final text into the published
                    // transcript so callers reading it directly (not
                    // via stopAndAwaitFinal) see the post-roll
                    // completion that partials usually miss.
                    if !final.isEmpty { self.transcript = final }
                    let wasUserStop = self.pendingFinal != nil
                    if wasUserStop {
                        self.deliverFinalIfPending(text: final)
                    } else {
                        self.clearRecognitionState()
                    }
                    // Signal an unexpected termination so the orb can
                    // re-arm. We only count error paths — if SFSpeech
                    // delivers isFinal without a caller waiting, the
                    // session ended cleanly on its own and the orb is
                    // about to restart via `isTaskRunning` change
                    // anyway.
                    if !wasUserStop && wasError {
                        self.unexpectedStopTick &+= 1
                        NSLog("[voice] unexpectedStopTick=%d",
                              self.unexpectedStopTick)
                    }
                }
            }
        }

        isRecording = true
    }

    func stop() {
        guard isRecording else { return }
        stopAudioCapture()
        request?.endAudio()
        let snapshotTask = task
        Task.detached { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            await MainActor.run {
                guard let self, self.task === snapshotTask else { return }
                snapshotTask?.cancel()
                self.clearRecognitionState()
            }
        }
    }

    /// Stop recording and wait for the recognizer's actual `isFinal`
    /// callback before returning, with a `timeout` safety net. This
    /// is the right entry point for "user pressed Send, get me the
    /// definitive transcript" — partial transcripts routinely miss
    /// the last word(s) until SFSpeech's post-roll lands ~1–3s after
    /// `endAudio()`. Caller may use the returned string directly.
    /// Falls back to the last-seen partial if the final never arrives.
    func stopAndAwaitFinal(timeout: Double = 3.0) async -> String {
        if !isRecording && task == nil && pendingFinal == nil {
            return transcript
        }
        if isRecording {
            request?.endAudio()
            stopAudioCapture()
        }
        let snapshot = transcript
        return await withCheckedContinuation { (c: CheckedContinuation<String, Never>) in
            // Hand off any orphaned waiter so we never leak two on
            // the same instance.
            if let stale = pendingFinal {
                pendingFinal = nil
                stale.resume(returning: snapshot)
            }
            pendingFinal = c
            pendingFinalSnapshot = snapshot
            // Safety net: cancelled tasks / dropped audio sometimes
            // never deliver `isFinal`. After `timeout` seconds we
            // resume the continuation with whatever transcript we
            // have so the orb's commit pipeline keeps moving.
            Task { [weak self] in
                let ns = UInt64(max(0, timeout) * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
                await MainActor.run {
                    self?.deliverFinalIfPending(text: nil)
                }
            }
        }
    }

    /// Resume the pendingFinal continuation, exactly once. `text` is
    /// the recognizer-reported final string (preferred); when nil we
    /// fall back to the latest published transcript, then to the
    /// snapshot taken at stop time.
    private func deliverFinalIfPending(text: String?) {
        guard let c = pendingFinal else { return }
        pendingFinal = nil
        let snapshot = pendingFinalSnapshot
        pendingFinalSnapshot = ""
        let final: String
        if let text, !text.isEmpty {
            final = text
        } else if !transcript.isEmpty {
            final = transcript
        } else {
            final = snapshot
        }
        // The recognition task may still be running if we got here
        // via the timeout path; clean up so the next start() is
        // unblocked by the `task != nil` guard.
        task?.cancel()
        clearRecognitionState()
        c.resume(returning: final)
    }

    func resetTranscript() {
        transcript = ""
    }

    private func stopAudioCapture() {
        if isRecording { isRecording = false }
        if audioEngine.isRunning { audioEngine.stop() }
        audioEngine.inputNode.removeTap(onBus: 0)
    }

    private func clearRecognitionState() {
        request = nil
        task = nil
    }

    private static func requestSpeechAuth() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in
                cont.resume(returning: status)
            }
        }
    }
}

// SFSpeechRecognizerDelegate fires when the recognizer transitions
// between available / unavailable. After a session ends the recognizer
// goes unavailable for a few seconds; without this hook the orb's
// auto-restart silently failed, producing the "voice only works once"
// symptom. With it, queued starts get a second chance the moment the
// recognizer is ready again.
extension VoiceInput: SFSpeechRecognizerDelegate {
    nonisolated func speechRecognizer(
        _ speechRecognizer: SFSpeechRecognizer,
        availabilityDidChange available: Bool
    ) {
        Task { @MainActor [weak self] in
            guard let self, available, self.pendingStart else { return }
            self.pendingStart = false
            await self.start()
        }
    }
}
