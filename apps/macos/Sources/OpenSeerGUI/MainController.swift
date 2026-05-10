import Foundation
import SwiftUI

@MainActor
final class MainController: ObservableObject {
    @Published var selectedThreadID: String? = nil
    @Published var dryRun: Bool = false
    @Published var voiceAnswer: String? = nil

    let daemon: DaemonController
    private let binary: String

    init(binary: String) {
        self.binary = binary
        self.daemon = DaemonController(binary: binary)
    }

    var selectedThread: ChatThread? {
        daemon.threads.first { $0.id == selectedThreadID }
    }

    var selectedRunningRun: RunSession? {
        selectedThread?.sortedRuns.last { $0.status == .running }
    }

    func selectNewestThreadIfNeeded() {
        if selectedThreadID == nil,
           let newest = daemon.threads
                .max(by: { $0.lastActivity < $1.lastActivity }) {
            selectedThreadID = newest.id
        }
    }

    func submitTextPrompt(_ text: String) {
        // Text submits go through agentd alongside voice (WS Phase 2).
        // The legacy `.subprocess` path stays in code as an emergency
        // fallback path — flip this back if agentd development
        // regresses badly — and is removed wholesale in Phase 4 once
        // the telegram + CLI consolidation has settled.
        submitPrompt(text, transport: .agentd)
    }

    func submitVoicePrompt(_ text: String) {
        // Voice prompts go through agentd: the orb needs a control
        // channel for barge-in / ask-user / cancel, and a shared
        // long-running Python process. Phase 1 wires that here; the
        // subprocess fallback stays available if agentd can't start.
        submitPrompt(text, transport: .agentd) { [weak self] answer in
            self?.voiceAnswer = answer
        }
    }

    enum Transport {
        case subprocess
        case agentd
    }

    private func submitPrompt(_ text: String,
                              transport: Transport,
                              onFinalAnswer: ((String) -> Void)? = nil) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let continueThread: ChatThread? = {
            guard let cur = selectedThread, cur.kind == .local
            else { return nil }
            return cur
        }()
        // Barge-in: if a previous run on this thread is still
        // executing, cancel it. The agent writes the CANCEL sentinel
        // and emits a synthetic terminate step, so the run's status
        // settles to `.interrupted` and its partial progress flows
        // into the next call via renderSessionContext() — the LLM
        // sees "prior task interrupted: <last result>" and the new
        // prompt together, which is exactly the "stop and re-call
        // with the new message" behavior the user expects when they
        // talk over the agent.
        if let cur = continueThread {
            for run in cur.runs where run.status == .running {
                run.cancel()
            }
        }
        let sessionCtx = continueThread?.renderSessionContext()

        let s = RunSession(source: .localPrompt(text), binary: binary)
        s.onFinalAnswer = onFinalAnswer
        daemon.claimLocalPrompt(text)
        let onTraceFound: (String) -> Void = {
            [weak daemon, prompt = text] traceId in
            daemon?.reserveLocalTrace(traceId, prompt: prompt)
        }
        switch transport {
        case .subprocess:
            s.startLocal(prompt: text, dryRun: dryRun,
                         sessionContext: sessionCtx,
                         onTraceFound: onTraceFound)
        case .agentd:
            s.startViaAgentd(prompt: text, dryRun: dryRun,
                             binary: binary,
                             sessionContext: sessionCtx,
                             onTraceFound: onTraceFound)
        }
        let thread = daemon.addLocalRun(
            s, continueThread: continueThread?.id)
        selectedThreadID = thread.id
    }
}
