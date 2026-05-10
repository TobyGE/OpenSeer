import AppKit
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
        // Install the ask_user round-trip handler. The agentd
        // daemon emits `ask_user` whenever the agent decides it
        // needs a human in the loop (uncertain target, hard-to-
        // reverse action, ambiguous instructions). We surface it
        // as an NSAlert and post the reply back over ws.
        AgentdClient.shared.askUserHandler = { [weak self] req in
            await self?.respondToAskUser(req) ?? nil
        }
    }

    /// Show a modal alert for an ask_user request and return the
    /// user's reply (or nil if dismissed). Runs on the main thread.
    private func respondToAskUser(_ req: AgentdClient.AskUserRequest)
        async -> String?
    {
        let alert = NSAlert()
        alert.messageText = "OpenSeer 想确认一下"
        alert.informativeText = req.question
        alert.alertStyle = .informational

        var optionTitles: [String] = []
        var textField: NSTextField? = nil

        switch req.kind {
        case "confirm":
            optionTitles = ["Yes", "No"]
        case "choose":
            // NSAlert allows up to 3 buttons cleanly; truncate
            // longer option lists. For lots-of-options we should
            // build a custom sheet later — out of scope here.
            optionTitles = Array(req.options.prefix(3))
            if optionTitles.isEmpty { optionTitles = ["OK"] }
        default:
            // "text" — accept free-form input via accessory view.
            optionTitles = ["Send", "Cancel"]
            let tf = NSTextField(
                frame: NSRect(x: 0, y: 0, width: 320, height: 24))
            tf.placeholderString = "Your reply…"
            alert.accessoryView = tf
            textField = tf
        }
        for title in optionTitles {
            alert.addButton(withTitle: title)
        }
        // NSWindow.makeKeyAndOrderFront so the alert can grab
        // focus on top of the floating voice orb panel.
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        // NSAlert maps the first 3 buttons to
        // .alertFirstButtonReturn / .alertSecondButtonReturn /
        // .alertThirdButtonReturn (1000/1001/1002).
        let index = response.rawValue - NSApplication.ModalResponse
            .alertFirstButtonReturn.rawValue
        guard index >= 0, index < optionTitles.count else {
            return nil
        }
        let chosen = optionTitles[index]
        // text-kind Cancel → return nil (treated as timeout).
        if req.kind == "text" {
            if chosen == "Cancel" { return nil }
            let entry = textField?.stringValue.trimmingCharacters(
                in: .whitespacesAndNewlines) ?? ""
            return entry.isEmpty ? nil : entry
        }
        return chosen
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
