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
            textField = NSTextField(
                frame: NSRect(x: 0, y: 0, width: 320, height: 24))
            textField?.placeholderString = "Your reply…"
        }

        // Build the accessory view: any image attachments stacked
        // vertically, then the text input (for kind=text). Without
        // surfacing attachments, the user is asked to approve
        // hard-to-reverse actions blind — a real bug codex flagged.
        alert.accessoryView = buildAskUserAccessory(
            attachments: req.attachments,
            textField: textField)

        for title in optionTitles {
            alert.addButton(withTitle: title)
        }
        // NSApp.activate so the alert grabs focus on top of the
        // floating voice-orb panel.
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

    /// Compose the NSAlert accessory view: load up to 3 attachments
    /// as scaled NSImageViews stacked vertically; if a text field
    /// was supplied (kind=text), append it below. Returns nil when
    /// neither is needed so NSAlert uses its default layout.
    private func buildAskUserAccessory(
        attachments: [String],
        textField: NSTextField?
    ) -> NSView? {
        let imagePaths = attachments
            .prefix(3)
            .filter { FileManager.default.fileExists(atPath: $0) }
        if imagePaths.isEmpty && textField == nil { return nil }

        let width: CGFloat = 360
        let imageHeight: CGFloat = 200
        let spacing: CGFloat = 8
        let tfHeight: CGFloat = 24

        var rows: [NSView] = []
        for path in imagePaths {
            guard let img = NSImage(contentsOfFile: path) else { continue }
            let iv = NSImageView(
                frame: NSRect(x: 0, y: 0, width: width, height: imageHeight))
            iv.image = img
            iv.imageScaling = .scaleProportionallyUpOrDown
            iv.imageAlignment = .alignCenter
            iv.wantsLayer = true
            iv.layer?.cornerRadius = 6
            iv.layer?.masksToBounds = true
            iv.layer?.borderColor = NSColor.separatorColor.cgColor
            iv.layer?.borderWidth = 1
            rows.append(iv)
        }
        if let tf = textField { rows.append(tf) }

        var totalHeight: CGFloat = 0
        for (i, row) in rows.enumerated() {
            let h = (row is NSTextField) ? tfHeight : imageHeight
            if i > 0 { totalHeight += spacing }
            totalHeight += h
        }
        let container = NSView(
            frame: NSRect(x: 0, y: 0, width: width, height: totalHeight))
        // Stack from top to bottom in AppKit-flipped coords (origin
        // at bottom-left): iterate rows from last to first so the
        // first attachment ends up at the top.
        var y: CGFloat = totalHeight
        for row in rows {
            let h = (row is NSTextField) ? tfHeight : imageHeight
            y -= h
            row.frame = NSRect(x: 0, y: y, width: width, height: h)
            container.addSubview(row)
            y -= spacing
        }
        return container
    }

    var selectedThread: ChatThread? {
        daemon.threads.first { $0.id == selectedThreadID }
    }

    var selectedRunningRun: RunSession? {
        selectedThread?.sortedRuns.last { $0.status == .running }
    }

    /// Active = running OR held (the user took over but the run is
    /// still alive). Used for hand-off button targeting.
    var selectedActiveRun: RunSession? {
        selectedThread?.sortedRuns.last {
            $0.status == .running || $0.status == .held
        }
    }

    /// Toggle hold/resume on whatever active run is selected. Called
    /// from the voice orb's Hold/Resume button.
    func toggleHoldOnSelectedRun() {
        guard let run = selectedActiveRun else { return }
        if run.status == .held { run.resume() }
        else if run.status == .running { run.hold() }
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
