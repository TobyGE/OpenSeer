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
        submitPrompt(text)
    }

    func submitVoicePrompt(_ text: String) {
        submitPrompt(text) { [weak self] answer in
            self?.voiceAnswer = answer
        }
    }

    private func submitPrompt(_ text: String,
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
        s.startLocal(prompt: text, dryRun: dryRun,
                     sessionContext: sessionCtx) {
            [weak daemon, prompt = text] traceId in
            daemon?.reserveLocalTrace(traceId, prompt: prompt)
        }
        let thread = daemon.addLocalRun(
            s, continueThread: continueThread?.id)
        selectedThreadID = thread.id
    }
}
