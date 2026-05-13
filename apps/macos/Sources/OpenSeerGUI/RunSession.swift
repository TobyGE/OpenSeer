import Foundation
import SwiftUI

/// One running task: spawns `openseer task` (or watches a daemon
/// trace dir), tails events.jsonl, folds events into Turn objects,
/// republishes for the chat view.
@MainActor
final class RunSession: ObservableObject, Identifiable {
    let id = UUID()

    /// Where this session originated. UI uses this to label bubbles
    /// ("local task", "remote — Telegram").
    enum Source: Equatable {
        case localPrompt(String)
        case daemonTrace(traceId: String)
    }
    let source: Source

    /// Whether this run was launched in dry-run mode (the agent
    /// planned every action but the executor skipped the actual
    /// pyautogui call). Used by ChatThread.lastProducingAction to
    /// avoid telling follow-up runs "you typed X" when no typing
    /// actually happened. Defaults to false; replayed traces don't
    /// carry the flag, but historically those were executed runs.
    var dryRun: Bool = false

    @Published var turns: [Turn] = []
    @Published var status: Status = .running
    @Published var traceId: String? = nil
    @Published var errorMessage: String? = nil
    @Published private(set) var finalAnswer: String? = nil
    var onFinalAnswer: ((String) -> Void)? = nil

    /// Post-run skill proposal awaiting the user's decision. Set when
    /// the daemon emits `skill_proposed` (reflection identified a
    /// durable lesson and wrote `proposed_skill.md` to the run dir);
    /// cleared on `skill_applied` / `skill_discarded` or when the
    /// session is replaced. The orb surfaces this as a "学到了 → 存为
    /// skill?" chip — the read-only payload preview is shown in a
    /// sheet on tap, and Save / Discard send back `apply_skill` /
    /// `discard_skill` over the agentd WS.
    @Published var pendingLesson: ProposedLesson? = nil
    /// Name of the skill we most recently applied, kept around for ~5
    /// seconds so the orb can flash a "saved: foo-com-web" toast
    /// before the chip vanishes entirely.
    @Published var lastAppliedSkillName: String? = nil
    /// When this session was created in the GUI. Used to order the
    /// session list (newest first). For historical daemon traces
    /// loaded at startup we override this from the run dir's mtime.
    var createdAt: Date = Date()

    /// Title for the session list row. Falls back to the prompt
    /// captured in the source enum for local runs, or the first
    /// user-prompt turn for daemon-tailed runs, or the trace id.
    var title: String {
        if case .localPrompt(let p) = source, !p.isEmpty { return p }
        if let prompt = turns.first(where: { $0.isUserPrompt })?.promptText,
           !prompt.isEmpty { return prompt }
        if let tid = traceId { return "trace \(tid.prefix(8))…" }
        return "New session"
    }

    enum Status: Equatable {
        case running, done, fail, cap, interrupted
        /// User pressed "换我 / Hand off" — agent is parked between
        /// steps, waiting for the user to release it via resume().
        /// Behaves like running for "is the task still alive"
        /// purposes (the run will resume from where it left off).
        case held
    }

    /// One pending skill suggestion. `body` is the full SKILL.md the
    /// model produced (frontmatter + body, validated server-side).
    struct ProposedLesson: Equatable {
        let runId: String
        let skillName: String
        let isNew: Bool        // create new vs update existing
        let lesson: String     // bullet-form "Lesson learned" text
        let body: String       // full SKILL.md preview
    }

    private var stream: CLI.StreamHandle?
    private var watcher: FileTail?
    private let binary: String
    private var didNotifyFinalAnswer = false

    init(source: Source, binary: String) {
        self.source = source
        self.binary = binary
    }

    /// Launch a local `openseer task <text>` subprocess. The CLI
    /// writes structured events to ``~/.openseer/runs/<id>/events.jsonl``
    /// (TrajectoryCallback) and prints human-oriented progress to
    /// stdout — the JSON event stream and stdout are two SEPARATE
    /// channels. The chat needs the structured one. We:
    ///
    ///   1. snapshot the current ``runs/latest`` target,
    ///   2. spawn the subprocess,
    ///   3. poll ``runs/latest`` until it points at a NEW dir,
    ///   4. attach FileTail to that dir's events.jsonl.
    ///
    /// We do NOT pre-seed a user-prompt bubble: the agent's
    /// ``task_started`` event carries the prompt and TurnFolder
    /// renders it. Seeding here would create a duplicate bubble.
    func startLocal(prompt: String, dryRun: Bool,
                    sessionContext: String? = nil,
                    onTraceFound: ((String) -> Void)? = nil) {
        guard case .localPrompt = source else { return }
        self.dryRun = dryRun
        let runsDir = NSHomeDirectory() + "/.openseer/runs"

        var args = ["task", prompt]
        if !dryRun { args.append("--execute") }
        // If the GUI is continuing an existing thread, write its
        // prior-conversation summary to a tmp file and pass via
        // --session-context-file. Args alone would put the (often
        // long) summary in `ps`-visible argv. We use the system
        // tmp dir (NOT runs/) so a crash mid-flight can't leave
        // a non-directory entry under runs/ where loadRecentRuns
        // would mis-classify it as a trace and tail forever
        // (codex P2).
        var contextFile: String? = nil
        if let ctx = sessionContext, !ctx.isEmpty {
            let tmp = NSTemporaryDirectory()
                + "openseer-ctx-" + UUID().uuidString
            do {
                try ctx.write(toFile: tmp, atomically: true,
                              encoding: .utf8)
                contextFile = tmp
                args.append(contentsOf: ["--session-context-file", tmp])
            } catch {
                NSLog("openseer: failed to write session context: %@",
                      "\(error)")
            }
        }
        // Capture trace_id from the subprocess's own stdout so we
        // bind to OUR run dir, not "whichever symlink updated next"
        // (codex P2: in a daemon-running scenario, the global
        // `runs/latest` could flip to a daemon-spawned trace and
        // we'd tail the wrong events.jsonl).
        //
        // The CLI prints `[agent] dry_run=…  out_dir=/Users/.../runs/<id>`
        // as the very first line. Parse that for the trace id.
        let onLine: @Sendable (String) -> Void = { [weak self] line in
            guard let self else { return }
            if line.contains("[agent]"),
               let r = line.range(of: "out_dir=") {
                let path = line[r.upperBound...]
                    .split(separator: " ").first
                    .map(String.init) ?? String(line[r.upperBound...])
                let traceId = (path as NSString).lastPathComponent
                Task { @MainActor in
                    self.attachLocalTrace(traceId, runsDir: runsDir)
                    onTraceFound?(traceId)
                }
            }
        }
        let h = CLI.stream(path: binary, args: args, onLine: onLine)
        do {
            try h.start()
            self.stream = h
        } catch {
            errorMessage = "couldn't spawn openseer task: \(error)"
            status = .fail
            // Spawn failed — the wait task that normally cleans up
            // the tmp ctx file will never run. Clean up now so we
            // don't leak a file per failed Send (codex P3).
            if let p = contextFile {
                try? FileManager.default.removeItem(atPath: p)
            }
            return
        }

        // 15-second deadline to see the trace_id line. CLI is run
        // with PYTHONUNBUFFERED=1 (CLI.swift) so the line should
        // arrive within milliseconds, but the agent may already be
        // mid-screenshot/inference before we attach. If we still
        // don't see it after 15s assume the subprocess died early
        // and surface that — without a watcher we'd never get
        // task_finished events.
        Task.detached { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            await MainActor.run {
                guard let self else { return }
                if self.watcher == nil {
                    self.errorMessage = "Couldn't locate the run trace "
                        + "under \(runsDir). The subprocess didn't print "
                        + "[agent] out_dir=… within 5s."
                    if self.status == .running { self.status = .fail }
                }
            }
        }

        // Track exit so the wait block can flag spawn-level failures.
        // We DON'T flip status to .done here on exit==0 — the
        // task_finished event is authoritative and may say
        // fail/cap/interrupted.
        let ctxPath = contextFile
        Task { [weak self] in
            guard let stream = self?.stream else {
                if let p = ctxPath { try? FileManager.default.removeItem(atPath: p) }
                return
            }
            let exit = await stream.wait()
            await MainActor.run {
                guard let self else { return }
                if exit != 0 && self.status == .running {
                    self.status = .fail
                    if self.errorMessage == nil {
                        self.errorMessage = "openseer task exited with code \(exit)"
                    }
                }
            }
            // Drop the tmp context file once the agent is done with
            // it. Leaving it would leak a small file per continued
            // turn into ~/.openseer/runs/.
            if let p = ctxPath {
                try? FileManager.default.removeItem(atPath: p)
            }
        }
    }

    /// Attach FileTail to the local run's events.jsonl. Does NOT
    /// seed a user-prompt bubble; task_started will produce one
    /// via TurnFolder. Idempotent — second call with the same id
    /// is a no-op (defends against races where parsing produces
    /// the trace twice).
    private func attachLocalTrace(_ traceId: String, runsDir: String) {
        guard watcher == nil else { return }
        self.traceId = traceId
        let path = runsDir + "/" + traceId + "/events.jsonl"
        watcher = FileTail(path: path) { [weak self] line in
            Task { @MainActor in self?.ingestEventLine(line) }
        }
        watcher?.start()
    }

    /// Start a local task via the WebSocket daemon instead of a
    /// per-task subprocess. The daemon stays warm across tasks, can
    /// be talked to by multiple clients (voice orb + main window +
    /// telegram), and exposes a control channel for cancel /
    /// ask-user / barge-in. Events arrive over ws and are folded
    /// into the same Turn pipeline as the subprocess path.
    func startViaAgentd(prompt: String, dryRun: Bool,
                        binary: String,
                        sessionContext: String? = nil,
                        backgroundMode: Bool = false,
                        onTraceFound: ((String) -> Void)? = nil) {
        guard case .localPrompt = source else { return }
        self.dryRun = dryRun
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                try await AgentdClient.shared.ensureRunning(binary: binary)
                let runId = try await AgentdClient.shared.startTask(
                    prompt: prompt,
                    dryRun: dryRun,
                    sessionContext: sessionContext,
                    backgroundMode: backgroundMode
                ) { [weak self] msg in
                    Task { @MainActor in
                        self?.ingestAgentdMessage(msg)
                    }
                }
                self.traceId = runId
                onTraceFound?(runId)
            } catch {
                self.errorMessage = "agentd: \(error)"
                if self.status == .running { self.status = .fail }
                NSLog("[run] startViaAgentd failed: %@", "\(error)")
            }
        }
    }

    /// Adapt the agentd ws message shape
    /// `{type:"event", run_id, event:{type, step, data, timestamp}}`
    /// into the same RunEvent the file-tail path consumes via
    /// `ingestEventLine`.
    private func ingestAgentdMessage(_ msg: [String: Any]) {
        guard let evObj = msg["event"] as? [String: Any] else { return }
        // RunEvent decodes from JSON; re-serialize the inner event.
        guard let data = try? JSONSerialization.data(
            withJSONObject: evObj),
              let ev = try? JSONDecoder().decode(RunEvent.self, from: data)
        else {
            NSLog("[run] bad agentd event: %@", "\(evObj)")
            return
        }
        ingestEvent(ev)
    }

    /// Attach to an existing trace dir (daemon-spawned runs, or
    /// historical replays). We don't have stdout for those — tail
    /// events.jsonl directly. The first event in the file is
    /// task_started which TurnFolder renders as the user-prompt
    /// bubble, so we don't seed one here (doing so would create
    /// a duplicate).
    func attachToTrace(_ traceId: String) {
        self.traceId = traceId
        let dir = NSHomeDirectory() + "/.openseer/runs/\(traceId)"
        let path = dir + "/events.jsonl"
        watcher = FileTail(path: path) { [weak self] line in
            Task { @MainActor in self?.ingestEventLine(line) }
        }
        watcher?.start()
    }

    /// Cooperative stop. For local runs we both write the CANCEL
    /// sentinel (so the agent loop exits cleanly with a synthetic
    /// terminate step) AND terminate the subprocess as a hard
    /// fallback if the loop is wedged in a long capture/inference
    /// call. For daemon-spawned runs we can't kill the process
    /// (it's the daemon's worker thread, killing it would take down
    /// every other chat) — the sentinel is the only knob we have.
    /// Hand-off: tell agentd to drop a HOLD sentinel. Optimistically
    /// flip status to .held immediately so the user sees the Resume
    /// button right away — the agent's actual pause happens at the
    /// top of its next step (could be 10-30s away while the current
    /// step's capture/LLM/execute cycle finishes). The agent_held
    /// event is then a no-op confirmation; agent_resumed / task_*
    /// events still authoritatively override status afterward.
    func hold() {
        guard let tid = traceId, status == .running else { return }
        status = .held
        Task { @MainActor in
            await AgentdClient.shared.holdTask(runId: tid)
        }
    }

    /// Resume from hand-off: remove the HOLD sentinel. Optimistically
    /// flip back to .running so the user gets immediate feedback;
    /// the agent's actual resume happens within ~500ms (its outer
    /// loop polls the HOLD sentinel every 500ms).
    func resume() {
        guard let tid = traceId, status == .held else { return }
        status = .running
        Task { @MainActor in
            await AgentdClient.shared.resumeTask(runId: tid)
        }
    }

    func cancel() {
        if let tid = traceId {
            let cancelPath = NSHomeDirectory()
                + "/.openseer/runs/\(tid)/CANCEL"
            FileManager.default.createFile(
                atPath: cancelPath,
                contents: "requested by GUI Stop button\n".data(using: .utf8))
            // Also signal agentd directly — if this run is going
            // through ws (no `stream` to terminate), the sentinel
            // alone might not fire until the next outer-loop tick.
            // cancel_task additionally cancels the asyncio worker
            // so an in-flight LLM stream stops immediately.
            Task { @MainActor in
                await AgentdClient.shared.cancelTask(runId: tid)
            }
        }
        if case .localPrompt = source {
            // Give the agent a moment to see the sentinel and emit
            // its synthetic terminate event, then hard-kill if it
            // hasn't exited.
            let s = stream
            Task.detached {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                s?.terminate()
            }
        }
        if status == .running { status = .interrupted }
    }

    deinit {
        // SAFE: deinit must be Sendable-friendly; no @MainActor work.
        stream?.terminate()
        watcher?.stop()
    }

    // ── ingest helpers ─────────────────────────────────────────────

    private func ingestStdoutLine(_ line: String) {
        // The Python CLI's stdout has both human-friendly status lines
        // (e.g. `[ax] pid=… → 19 elements`) AND the JSON event blobs
        // it prints alongside. Try-decode each line; only feed the
        // ones that parse as RunEvent.
        guard let data = line.data(using: .utf8) else { return }
        guard let ev = try? JSONDecoder().decode(RunEvent.self, from: data)
        else { return }
        Task { @MainActor in self.ingestEvent(ev) }
    }

    private func ingestEventLine(_ line: String) {
        guard let data = line.data(using: .utf8) else { return }
        guard let ev = try? JSONDecoder().decode(RunEvent.self, from: data)
        else { return }
        ingestEvent(ev)
    }

    private func ingestEvent(_ ev: RunEvent) {
        // Capture trace_id + dry_run from task_started (first event
        // of the run). For attached / replayed traces this is how
        // we recover the dry-run flag — startLocal/startViaAgentd
        // set it directly, but DaemonController.loadRecentRuns and
        // scanForNewRuns construct RunSessions that only attach via
        // events.jsonl, so the event payload is the authority.
        if ev.type == "task_started" {
            if let tid = ev.data["trace_id"]?.string { traceId = tid }
            if let dr = ev.data["dry_run"]?.bool   { dryRun  = dr  }
        }
        TurnFolder.apply(ev, to: &turns)
        if ev.type == "task_finished" {
            let st = ev.data["status"]?.string ?? "done"
            status = (st == "done" ? .done
                      : st == "cap" ? .cap : .fail)
            captureFinalAnswer()
        } else if ev.type == "task_failed" {
            status = .fail
            errorMessage = ev.data["error"]?.string
        } else if ev.type == "agent_held" {
            // The user (or another client) toggled hand-off. Agent
            // is parked between steps until we resume.
            if status == .running { status = .held }
        } else if ev.type == "agent_resumed" {
            if status == .held { status = .running }
        } else if ev.type == "skill_proposed" {
            // Reflection identified a durable lesson and dropped a
            // proposed SKILL.md on disk. Surface a chip the user can
            // tap to Save / Discard; AgentdClient.applySkill picks it
            // up from disk later, so the body in the event is just
            // for the preview sheet.
            let runId = ev.data["run_id"]?.string ?? traceId ?? ""
            let name = ev.data["skill_name"]?.string ?? ""
            if !runId.isEmpty && !name.isEmpty {
                pendingLesson = ProposedLesson(
                    runId: runId,
                    skillName: name,
                    isNew: ev.data["is_new"]?.bool ?? true,
                    lesson: ev.data["lesson"]?.string ?? "",
                    body: ev.data["body"]?.string ?? "")
            }
        } else if ev.type == "skill_applied" {
            // pendingLesson clears unconditionally — the skill IS on
            // disk now whether the event arrived live or as part of
            // a historical trace replay. The "just saved" toast on
            // top of that is momentary by design (5s).
            //
            // The clear used to be scheduled by
            // MainController.applyPendingLesson, but that path only
            // runs on a live click. Reattaching to a recent trace
            // replays the skill_applied event without ever calling
            // applyPendingLesson, so the toast had nothing to turn
            // it off and the orb stayed bound to the finished run
            // indefinitely. Now ingestEvent owns the schedule —
            // every code path that sets lastAppliedSkillName also
            // schedules its expiry.
            //
            // Events older than 5s arrive already-expired, so we
            // just skip the toast entirely; the 5s window is the
            // budget for the whole feature anyway.
            let age = Date().timeIntervalSince1970 - ev.ts
            let toastLifetime: TimeInterval = 5
            if age < toastLifetime {
                let name = ev.data["skill_name"]?.string
                    ?? pendingLesson?.skillName
                lastAppliedSkillName = name
                let remaining = toastLifetime - age
                scheduleAppliedToastClear(after: remaining, skillName: name)
            }
            pendingLesson = nil
        } else if ev.type == "skill_discarded" {
            pendingLesson = nil
        }
        // Trigger SwiftUI redraw.
        objectWillChange.send()
    }

    /// Schedule a single-shot clear of `lastAppliedSkillName`.
    /// Called from ingestEvent whenever the toast becomes visible
    /// so every entry point (live save, replay of a recent event)
    /// gets cleanup, not just the in-process click. The skillName
    /// guard prevents an older scheduled clear from wiping a newer
    /// toast if the user happens to save two skills back to back.
    private func scheduleAppliedToastClear(after seconds: TimeInterval,
                                           skillName: String?) {
        guard seconds > 0 else {
            lastAppliedSkillName = nil
            objectWillChange.send()
            return
        }
        let target = skillName
        Task { @MainActor [weak self] in
            try? await Task.sleep(
                nanoseconds: UInt64(seconds * 1_000_000_000))
            guard let self else { return }
            if self.lastAppliedSkillName == target {
                self.lastAppliedSkillName = nil
                self.objectWillChange.send()
            }
        }
    }

    private func json2task(at path: String) -> String? {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj["task"] as? String
    }

    private func captureFinalAnswer() {
        guard !didNotifyFinalAnswer else { return }
        for turn in turns.reversed() {
            guard let final = turn.finalOutput,
                  !final.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { continue }
            finalAnswer = final
            didNotifyFinalAnswer = true
            onFinalAnswer?(final)
            return
        }
    }
}

/// Tail an existing on-disk file: read whatever's already there, then
/// listen for append events via DispatchSource. Emits each newline-
/// terminated line through `onLine`. Stops on `stop()` or deinit.
final class FileTail: @unchecked Sendable {
    private let path: String
    private let onLine: @Sendable (String) -> Void
    private var fd: Int32 = -1
    private var src: DispatchSourceFileSystemObject?
    private var carry = Data()
    private let q = DispatchQueue(label: "openseer.filetail")
    private var stopped = false

    init(path: String, onLine: @escaping @Sendable (String) -> Void) {
        self.path = path
        self.onLine = onLine
    }

    func start() {
        q.async { [self] in
            // The file may not exist yet (daemon still creating the
            // run dir). Poll briefly until it shows up.
            for _ in 0..<60 {
                if FileManager.default.fileExists(atPath: path) { break }
                Thread.sleep(forTimeInterval: 0.5)
            }
            fd = open(path, O_RDONLY)
            guard fd >= 0 else { return }
            // Drain initial contents.
            drain()
            // Watch for further appends.
            let s = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: fd, eventMask: [.extend, .write], queue: q)
            s.setEventHandler { [weak self] in self?.drain() }
            s.setCancelHandler { [fd = self.fd] in
                if fd >= 0 { close(fd) }
            }
            s.resume()
            src = s
        }
    }

    func stop() {
        q.async { [self] in
            stopped = true
            src?.cancel()
            src = nil
            fd = -1
        }
    }

    deinit { stop() }

    private func drain() {
        guard fd >= 0, !stopped else { return }
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            carry.append(buf, count: n)
            while let nl = carry.firstIndex(of: 0x0A) {
                let line = carry.subdata(in: 0..<nl)
                carry.removeSubrange(0...nl)
                if let s = String(data: line, encoding: .utf8) { onLine(s) }
            }
        }
    }
}
