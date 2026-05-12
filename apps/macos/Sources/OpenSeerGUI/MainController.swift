import AppKit
import Foundation
import SwiftUI

@MainActor
final class MainController: ObservableObject {
    @Published var selectedThreadID: String? = nil
    @Published var dryRun: Bool = false
    @Published var voiceAnswer: String? = nil
    /// Background mode — agent prefers AppleScript-based browser
    /// automation over pyautogui so it doesn't steal mouse / keyboard
    /// focus. User keeps using their Mac while the agent works. The
    /// browser-background skill carries the patterns; we just nudge
    /// the model via session context to prefer it.
    @Published var backgroundMode: Bool = false

    /// One-shot session-context snippet captured at hotkey-press
    /// time (frontmost app name, etc.). Consumed by the next
    /// submitPrompt and cleared. Lets the user say a follow-up
    /// like "翻译这段" without retyping which app they meant.
    @Published var pendingHotkeyContext: String? = nil

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
        // Global cmd+option+O — open the voice orb + listen, with
        // the currently frontmost app captured as session context.
        GlobalHotkey.shared.install { [weak self] in
            guard let self else { return }
            if let ctx = FrontmostCapture.capture() {
                self.recordHotkeyContext(ctx)
            }
            // Bring OpenSeer (and the orb's NSPanel) forward and
            // post the open notification — VoiceOrbView listens.
            NSApp.activate(ignoringOtherApps: true)
            NotificationCenter.default.post(
                name: .openVoiceOrb, object: nil)
        }
    }

    func recordHotkeyContext(_ ctx: FrontmostContext) {
        // Plain English so the agent's system prompt can pick it
        // up naturally without a dedicated parser. The "your next
        // instruction" framing nudges the model to apply the
        // captured context to whatever the user says next instead
        // of treating it as a separate task.
        let bundleSuffix = ctx.bundleId.map { " (\($0))" } ?? ""
        pendingHotkeyContext = "USER OPENED THE OPENSEER HOTKEY "
            + "WHILE LOOKING AT: \(ctx.appName)\(bundleSuffix). "
            + "Their next instruction is about whatever they were "
            + "doing in that app — if you need to operate on it, "
            + "switch focus there first (open_app or click)."
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

    /// Stop the active run (Cancel). Used by the voice orb's Stop
    /// button — different from Hand off, which only pauses.
    func stopSelectedRun() {
        selectedActiveRun?.cancel()
    }

    /// Walk the currently-selected thread for a run holding a
    /// `pendingLesson`. The proposal can land *after* task_finished
    /// (post-run reflection lives in `on_run_end`), so the run is no
    /// longer in `.running` state — but it's still the newest run
    /// on the thread. We pick the latest non-nil-lesson run.
    private func runWithPendingLesson() -> RunSession? {
        let runs = selectedThread?.sortedRuns ?? []
        return runs.reversed()
            .first { $0.pendingLesson != nil }
    }

    /// User accepted the post-run skill suggestion. The daemon owns
    /// the canonical body on disk; we just authorize the write over
    /// the agentd WS and trust the inbound `skill_applied` event to
    /// clear the chip on success.
    func applyPendingLesson() {
        guard let run = runWithPendingLesson(),
              let pending = run.pendingLesson else { return }
        Task { @MainActor in
            do {
                _ = try await AgentdClient.shared.applySkill(
                    runId: pending.runId)
                // The skill_applied event flips lastAppliedSkillName
                // and clears pendingLesson; auto-clear the toast after
                // a few seconds so the orb doesn't keep a stale chip.
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if run.lastAppliedSkillName == pending.skillName {
                    run.lastAppliedSkillName = nil
                    run.objectWillChange.send()
                }
            } catch {
                NSLog("[lesson] applySkill failed: %@", "\(error)")
                // Leave the chip up so the user can retry; surface the
                // error inline via the run's errorMessage so it's
                // visible somewhere debuggable.
                run.errorMessage = "Save skill failed: \(error)"
                run.objectWillChange.send()
            }
        }
    }

    /// User dismissed the suggestion. Clear locally first (snappier
    /// than waiting for the round-trip) and tell the daemon to drop
    /// the proposed_skill.md sidecar so the same suggestion can't
    /// re-appear on a stale reconnect.
    func discardPendingLesson() {
        guard let run = runWithPendingLesson(),
              let pending = run.pendingLesson else { return }
        run.pendingLesson = nil
        run.objectWillChange.send()
        Task { @MainActor in
            try? await AgentdClient.shared.discardSkill(
                runId: pending.runId)
        }
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
            // Cancel both .running AND .held runs. A held run still
            // has a worker thread (and possibly an in-flight LLM /
            // action cycle that hasn't reached the HOLD check yet)
            // — leaving it alone would let the previous agent
            // continue while the new one starts, defeating barge-in
            // (codex P2 on 55b6441).
            for run in cur.runs
                where run.status == .running || run.status == .held {
                run.cancel()
            }
        }
        // Merge hotkey-time frontmost-app context into the thread's
        // session context. One-shot — clear it after consuming so
        // subsequent follow-ups don't keep pretending we're still
        // looking at the same app.
        var sessionCtx = continueThread?.renderSessionContext()
        if let hk = pendingHotkeyContext, !hk.isEmpty {
            sessionCtx = ((sessionCtx ?? "") + (sessionCtx?.isEmpty == false
                ? "\n\n" : "") + hk)
            pendingHotkeyContext = nil
        }
        // Background mode hint — read the browser-background skill
        // first, prefer AppleScript browser automation over
        // pyautogui, NEVER call `activate` on the user's apps.
        if backgroundMode {
            let bg = "BACKGROUND MODE: the user wants this task done "
                + "without stealing their mouse / keyboard / "
                + "frontmost app. Clicks are AUTOMATICALLY routed to "
                + "the target app via CGEventPostToPid by the "
                + "executor — your normal `click` actions just work "
                + "and don't disturb the user's cursor. Typing and "
                + "keyboard focus are NOT routed yet though, so: do "
                + "NOT call `activate`, and for browser-heavy flows "
                + "consider `read_skill browser-background` to drive "
                + "via AppleScript + injected JS instead of click-"
                + "and-type. If you hit a captcha / vision-required "
                + "UI you can't handle without focus, "
                + "ask_user(kind=\"confirm\") with a screenshot and "
                + "let the user decide whether to escalate to "
                + "foreground."
            sessionCtx = ((sessionCtx ?? "") + (sessionCtx?.isEmpty == false
                ? "\n\n" : "") + bg)
        }

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
                             backgroundMode: backgroundMode,
                             onTraceFound: onTraceFound)
        }
        let thread = daemon.addLocalRun(
            s, continueThread: continueThread?.id)
        selectedThreadID = thread.id
    }
}
