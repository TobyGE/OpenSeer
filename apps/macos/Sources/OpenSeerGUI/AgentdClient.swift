import Darwin
import Foundation

/// Connection to the `openseer agentd` WebSocket daemon. Phase 1
/// (skeleton): reads the rendezvous file the daemon writes on
/// startup, opens a WebSocket, completes the token auth handshake,
/// exposes a request/response API and an event stream.
///
/// Not yet wired into RunSession / voice orb / main composer — those
/// migrations are Phase 2+. For now `runProbe()` validates the full
/// roundtrip end-to-end.
@MainActor
final class AgentdClient: NSObject {
    static let shared = AgentdClient()

    // MARK: - Rendezvous (written by the Python daemon)

    private struct Rendezvous: Codable {
        let host: String
        let port: Int
        let token: String
        let pid: Int
        let started_at: Double
        let protocol_version: Int
    }

    static func rendezvousPath() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".openseer/agentd.json")
    }

    // MARK: - State

    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    private var rendezvous: Rendezvous?

    /// Server→client replies keyed by request_id. The send side
    /// installs a continuation; the receive loop pops + resumes.
    private var pendingRequests: [String: CheckedContinuation<[String: Any], Error>] = [:]

    /// Server→client event stream keyed by run_id. start_task
    /// callers register a handler; the receive loop calls it for
    /// every event tagged with that run_id.
    private var eventHandlers: [String: ([String: Any]) -> Void] = [:]

    /// Events that arrived for a run_id before its handler was
    /// registered. This is essential: the daemon can (and for the
    /// Phase 1 stub does) emit `task_started` before the caller
    /// gets the start_task ack and calls observe() — without
    /// buffering, the dispatcher would log those as unrouted and
    /// drop them. Replayed in order on observe().
    private var pendingEvents: [String: [[String: Any]]] = [:]

    /// Run IDs we've seen a `task_finished` / `task_failed` for
    /// before any handler was registered. When observe() is later
    /// called for one of these IDs, we replay the buffer and then
    /// immediately tear down the handler since no more events are
    /// coming.
    private var pendingTerminated: Set<String> = []

    /// run_id → handler that resolves an ask_user round-trip. The
    /// handler receives the parsed server payload (question, kind,
    /// options, request_id) and returns the user's reply
    /// asynchronously. AgentdClient sends `user_reply` to the
    /// daemon with that string.
    var askUserHandler: ((AskUserRequest) async -> String?)? = nil

    struct AskUserRequest: Equatable {
        let runId: String
        let requestId: String
        let question: String
        let kind: String         // "confirm" | "choose" | "text"
        let options: [String]
        let attachments: [String]
    }

    private var nextRequestId = 0

    /// Lazily-spawned `openseer agentd` subprocess, kept so we can
    /// later add a "Stop daemon" gesture if we want. Nil after the
    /// rendezvous file is owned by some other (e.g. user-launched)
    /// agentd we attached to.
    private var spawnedDaemon: Process?

    enum AgentdError: Error, CustomStringConvertible {
        case noRendezvous(String)
        case connectionClosed(String)
        case authFailed(String)
        case badResponse(String)

        var description: String {
            switch self {
            case .noRendezvous(let m): return "no rendezvous: \(m)"
            case .connectionClosed(let m): return "connection closed: \(m)"
            case .authFailed(let m): return "auth failed: \(m)"
            case .badResponse(let m): return "bad response: \(m)"
            }
        }
    }

    // MARK: - Connect / auth

    private func loadRendezvous() throws -> Rendezvous {
        let url = Self.rendezvousPath()
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw AgentdError.noRendezvous(
                "\(url.path) missing — start `openseer agentd` first")
        }
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(Rendezvous.self, from: data)
    }

    /// Open the WebSocket and complete the auth handshake. Safe to
    /// call when already connected (no-op).
    func connect() async throws {
        if task != nil { return }
        let r = try loadRendezvous()
        rendezvous = r
        guard let url = URL(string: "ws://\(r.host):\(r.port)") else {
            throw AgentdError.connectionClosed("bad url")
        }
        let cfg = URLSessionConfiguration.default
        let session = URLSession(configuration: cfg)
        self.session = session
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()

        // Receive loop is detached from connect()'s lifetime so we
        // keep draining messages even after this function returns.
        Task { @MainActor [weak self] in
            await self?.receiveLoop()
        }

        let ack = try await sendRequest(
            "auth", payload: ["token": r.token])
        guard ack["type"] as? String == "ack" else {
            throw AgentdError.authFailed("\(ack)")
        }
        NSLog("[agentd] connected on :%d (protocol v%d)",
              r.port, r.protocol_version)
    }

    func disconnect() {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
        // Fail any outstanding requests so callers don't hang.
        for (_, cont) in pendingRequests {
            cont.resume(throwing: AgentdError.connectionClosed("disconnect()"))
        }
        pendingRequests.removeAll()
        eventHandlers.removeAll()
        pendingEvents.removeAll()
        pendingTerminated.removeAll()
    }

    // MARK: - Send / receive

    /// Send `{type, request_id, ...payload}`, await the matching
    /// ack/error/pong reply. Returns the full reply dict.
    @discardableResult
    func sendRequest(_ type: String,
                     payload: [String: Any] = [:]) async throws -> [String: Any] {
        guard let task = self.task else {
            throw AgentdError.connectionClosed("not connected")
        }
        nextRequestId += 1
        let rid = "r\(nextRequestId)"
        var msg = payload
        msg["type"] = type
        msg["request_id"] = rid
        let data = try JSONSerialization.data(
            withJSONObject: msg, options: [.withoutEscapingSlashes])
        guard let text = String(data: data, encoding: .utf8) else {
            throw AgentdError.badResponse("non-utf8 payload")
        }
        return try await withCheckedThrowingContinuation { cont in
            pendingRequests[rid] = cont
            task.send(.string(text)) { [weak self] error in
                guard let error else { return }
                Task { @MainActor [weak self] in
                    if let c = self?.pendingRequests.removeValue(forKey: rid) {
                        c.resume(throwing: error)
                    }
                }
            }
        }
    }

    /// Register a handler for events tagged with `run_id`. If
    /// events arrived for this run_id before this call (very common
    /// — the daemon often emits `task_started` between the
    /// start_task ack and the caller's observe()), they're replayed
    /// here in order.
    ///
    /// The handler is NOT removed when `task_finished` / `task_failed`
    /// fires. Post-run events still belong to this run by run_id
    /// (the reflection pass runs in `on_run_end` and emits
    /// `skill_proposed` shortly after `task_finished`, and the
    /// matching `skill_applied` / `skill_discarded` come back from
    /// the daemon on user action). Auto-removing here meant those
    /// trailing events landed in `pendingEvents` and never reached
    /// the UI; the lesson chip silently never appeared.
    ///
    /// Memory: handlers persist for the life of this AgentdClient.
    /// A long-running session that fires hundreds of runs accumulates
    /// closures, but each is small and the entries get evicted on
    /// disconnect. Future: add explicit `unobserve(runId:)` when
    /// RunSession objects are torn down.
    func observe(runId: String,
                 handler: @escaping ([String: Any]) -> Void) {
        eventHandlers[runId] = handler
        if let buffered = pendingEvents.removeValue(forKey: runId) {
            for msg in buffered {
                handler(msg)
            }
        }
        // We still want to clear the terminated-flag bookkeeping
        // entry; it's only used to detect "terminator landed before
        // observe registered" and a re-observe after that is
        // pointless anyway — but the handler itself stays.
        pendingTerminated.remove(runId)
    }

    private func receiveLoop() async {
        while let task = self.task {
            do {
                let msg = try await task.receive()
                let text: String
                switch msg {
                case .string(let s): text = s
                case .data(let d): text = String(data: d, encoding: .utf8) ?? ""
                @unknown default: text = ""
                }
                guard let data = text.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any] else {
                    NSLog("[agentd] non-json msg: %@", text)
                    continue
                }
                dispatch(obj)
            } catch {
                NSLog("[agentd] receive failed: %@", "\(error)")
                // Resume outstanding continuations with the failure
                // so callers don't hang waiting on a dead socket.
                for (_, cont) in pendingRequests {
                    cont.resume(throwing: AgentdError.connectionClosed("\(error)"))
                }
                pendingRequests.removeAll()
                self.task = nil
                return
            }
        }
    }

    private func dispatch(_ msg: [String: Any]) {
        let type = msg["type"] as? String ?? ""
        if type == "ask_user" {
            handleAskUser(msg)
            return
        }
        if type == "event", let runId = msg["run_id"] as? String {
            let inner = msg["event"] as? [String: Any]
            let et = inner?["type"] as? String
            let terminator = (et == "task_finished" || et == "task_failed")
            if let handler = eventHandlers[runId] {
                handler(msg)
                // Intentionally NOT removing on terminator — the
                // run's reflection pass fires `skill_proposed` from
                // `on_run_end`, which the agent emits AFTER
                // `task_finished`. Tearing the handler down here
                // routed those trailing events into `pendingEvents`
                // forever (nobody calls observe() again), so the
                // lesson chip never appeared. See observe() doc for
                // the new lifecycle.
            } else {
                // No handler registered yet — buffer for replay
                // when observe(runId:) eventually catches up.
                pendingEvents[runId, default: []].append(msg)
                if terminator {
                    pendingTerminated.insert(runId)
                }
            }
            return
        }
        if let rid = msg["request_id"] as? String,
           let cont = pendingRequests.removeValue(forKey: rid) {
            cont.resume(returning: msg)
            return
        }
        NSLog("[agentd] unrouted msg: %@", "\(msg)")
    }

    // MARK: - Probe (Task #1 deliverable)

    // MARK: - High-level API

    /// Ensure agentd is running and we have a live, authed
    /// connection. Will (a) reuse an existing daemon if the
    /// rendezvous responds, or (b) spawn `openseer agentd` if not.
    /// Pass the resolved `openseer` binary path (same one
    /// `OpenSeerEnv.binaryPath` exposes).
    func ensureRunning(binary: String,
                       startupTimeout: TimeInterval = 10) async throws {
        // 1. If we're already connected and the socket is alive,
        // we're done.
        if task != nil {
            do {
                _ = try await sendRequest("ping", payload: [:])
                return
            } catch {
                // Stale connection; reset and retry.
                disconnect()
            }
        }
        // 2. Rendezvous file present → try connecting to whatever
        // daemon wrote it. If it doesn't answer the ping, treat the
        // file as stale and overwrite by spawning fresh.
        if FileManager.default.fileExists(
            atPath: Self.rendezvousPath().path) {
            do {
                try await connect()
                _ = try await sendRequest("ping", payload: [:])
                return
            } catch {
                NSLog("[agentd] stale rendezvous, respawning: %@",
                      "\(error)")
                disconnect()
                try? FileManager.default.removeItem(
                    at: Self.rendezvousPath())
            }
        }
        // 3. Spawn a fresh agentd. We don't supervise it past this
        // process's lifetime — if the user quits OpenSeerGUI the
        // daemon also goes. Future: detach via launchctl when we
        // want it to keep serving telegram in the background.
        try spawnDaemon(binary: binary)

        // 4. Wait for the rendezvous file to appear, then connect.
        let deadline = Date().addingTimeInterval(startupTimeout)
        while Date() < deadline {
            if FileManager.default.fileExists(
                atPath: Self.rendezvousPath().path) {
                try await connect()
                _ = try await sendRequest("ping", payload: [:])
                return
            }
            try await Task.sleep(nanoseconds: 120_000_000)
        }
        throw AgentdError.connectionClosed(
            "daemon failed to start within \(Int(startupTimeout))s")
    }

    private func spawnDaemon(binary: String) throws {
        // Singleton invariant: only one `openseer agentd` may run at
        // a time. Multiple daemons each take their own AX /
        // screen-recording locks and serialize on macOS's default 30s
        // messaging timeout, surfacing as exactly-30s prep delay per
        // agent step. Best-effort SIGTERM every other matching
        // process before we spawn fresh; we don't wait for the OS
        // to confirm exits because the rendezvous-file poll in
        // `ensureRunning` already handles the startup race.
        terminateStaleDaemons()

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binary)
        proc.arguments = ["agentd"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        // Pipe daemon stderr/stdout to a rotating log so users can
        // tail it if something explodes; otherwise daemon noise
        // would vanish.
        let logURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".openseer/agentd.log")
        FileManager.default.createFile(
            atPath: logURL.path, contents: Data())
        let logHandle = try FileHandle(forWritingTo: logURL)
        proc.standardOutput = logHandle
        proc.standardError = logHandle

        try proc.run()
        spawnedDaemon = proc
        NSLog("[agentd] spawned pid=%d, log=%@",
              proc.processIdentifier, logURL.path)
    }

    /// Find every `openseer.cli agentd` python process and SIGTERM it.
    /// Called before spawning to keep at most one daemon alive —
    /// duplicates cause the AX-timeout contention described above.
    /// pgrep is shipped with macOS and matches against the full
    /// command line via `-f`, which catches our python -m form.
    private func terminateStaleDaemons() {
        let pgrep = Process()
        pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        // Match both forms the daemon can be launched as:
        //   • console-script  → `/.../bin/openseer agentd`
        //   • module entry    → `python3 -m openseer.cli agentd`
        // Anchoring on `agentd$` keeps us from matching the macOS
        // helpers `talagentd` / `gamecontrolleragentd` (no `openseer`
        // in their argv anyway) and from matching incidental uses of
        // the word "agentd" mid-command. Case-sensitive by design so
        // the GUI binary itself (`OpenSeerGUI`) doesn't match.
        pgrep.arguments = ["-f", "openseer.* agentd$"]
        let pipe = Pipe()
        pgrep.standardOutput = pipe
        pgrep.standardError = Pipe()    // swallow noise
        do {
            try pgrep.run()
            pgrep.waitUntilExit()
        } catch {
            NSLog("[agentd] singleton: pgrep failed: %@", "\(error)")
            return
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: data, encoding: .utf8) ?? ""
        let selfPid = ProcessInfo.processInfo.processIdentifier
        let pids: [pid_t] = text.split(separator: "\n").compactMap {
            pid_t($0.trimmingCharacters(in: .whitespaces))
        }.filter { $0 != selfPid && $0 > 0 }
        for pid in pids {
            let rc = kill(pid, SIGTERM)
            NSLog("[agentd] singleton: SIGTERM stale daemon pid=%d (rc=%d)",
                  pid, rc)
        }
        // Tiny pause so the doomed daemons get a chance to release
        // their AX / screen-recording locks before the fresh one
        // grabs them. Empirically 150ms is plenty; we don't block
        // longer because the rendezvous poll covers startup races
        // and waitpid-style sync would need us to be each daemon's
        // parent (most aren't, e.g. CLI-started ones reparent to
        // launchd).
        if !pids.isEmpty {
            usleep(150_000)
        }
    }

    /// Persist the SKILL.md the daemon's reflection pass parked at
    /// `~/.openseer/runs/<runId>/proposed_skill.md`. The body itself
    /// is on disk on the daemon side; this request just authorizes
    /// the write. Returns the path the daemon wrote to on success.
    @discardableResult
    func applySkill(runId: String) async throws -> String {
        let ack = try await sendRequest(
            "apply_skill", payload: ["run_id": runId])
        guard let path = ack["skill_path"] as? String else {
            throw AgentdError.badResponse(
                "apply_skill ack missing skill_path: \(ack)")
        }
        return path
    }

    /// Tell the daemon the user rejected the proposed skill — it
    /// unlinks the sidecar so the suggestion can't be re-applied
    /// after dismissal and emits a `skill_discarded` event so any
    /// stale UI chips clear themselves.
    func discardSkill(runId: String) async throws {
        _ = try await sendRequest(
            "discard_skill", payload: ["run_id": runId])
    }

    /// Start a task on the daemon. Returns the run_id immediately
    /// after the ack; events flow through `onEvent` as the agent
    /// produces them. `onEvent` will see at least one terminator
    /// (`task_finished` or `task_failed`) at the end.
    @discardableResult
    func startTask(prompt: String,
                   dryRun: Bool,
                   sessionContext: String?,
                   backgroundMode: Bool = false,
                   onEvent: @escaping ([String: Any]) -> Void)
        async throws -> String {
        var payload: [String: Any] = [
            "task": prompt,
            "dry_run": dryRun,
            "background_mode": backgroundMode,
        ]
        if let ctx = sessionContext, !ctx.isEmpty {
            payload["session_context"] = ctx
        }
        let ack = try await sendRequest("start_task", payload: payload)
        guard let runId = ack["run_id"] as? String else {
            throw AgentdError.badResponse(
                "start_task ack missing run_id: \(ack)")
        }
        // Register the observer *after* the ack: any events the
        // daemon emitted between our send and now are buffered in
        // `pendingEvents` and replayed by observe().
        observe(runId: runId, handler: onEvent)
        return runId
    }

    /// Cancel a running task. Daemon writes the CANCEL sentinel +
    /// asyncio-cancels the runner; the agent loop emits its own
    /// task_finished(status="interrupted") in response.
    func cancelTask(runId: String) async {
        do {
            _ = try await sendRequest(
                "cancel_task", payload: ["run_id": runId])
        } catch {
            NSLog("[agentd] cancel failed: %@", "\(error)")
        }
    }

    /// Hand-off: suspend the agent so the user can drive the
    /// mouse/keyboard for a few steps. Agent re-reads AX/screen
    /// state when resumed, so it picks up whatever changed.
    func holdTask(runId: String) async {
        do {
            _ = try await sendRequest(
                "hold_task", payload: ["run_id": runId])
        } catch {
            NSLog("[agentd] hold failed: %@", "\(error)")
        }
    }

    func resumeTask(runId: String) async {
        do {
            _ = try await sendRequest(
                "resume_task", payload: ["run_id": runId])
        } catch {
            NSLog("[agentd] resume failed: %@", "\(error)")
        }
    }

    /// Handle an incoming ask_user from the daemon. Routes to the
    /// askUserHandler, awaits its return, and posts a user_reply
    /// back. If no handler is registered we reply with null which
    /// the agent treats as "user didn't respond" and terminates.
    private func handleAskUser(_ msg: [String: Any]) {
        guard let runId = msg["run_id"] as? String,
              let reqId = msg["request_id"] as? String else {
            NSLog("[agentd] malformed ask_user: %@", "\(msg)")
            return
        }
        let req = AskUserRequest(
            runId: runId,
            requestId: reqId,
            question: msg["question"] as? String ?? "",
            kind: msg["kind"] as? String ?? "text",
            options: msg["options"] as? [String] ?? [],
            attachments: msg["attachments"] as? [String] ?? []
        )
        guard let handler = askUserHandler else {
            NSLog("[agentd] no askUserHandler — replying null for %@",
                  req.requestId)
            sendUserReply(requestId: req.requestId, reply: nil)
            return
        }
        Task { @MainActor [weak self] in
            let reply = await handler(req)
            self?.sendUserReply(requestId: req.requestId, reply: reply)
        }
    }

    private func sendUserReply(requestId: String, reply: String?) {
        // `ask_id` carries the original ask_user's request_id;
        // `request_id` on this user_reply message is allocated by
        // sendRequest for the ack flow. Two distinct ids to avoid
        // colliding the ask_user round-trip with the user_reply's
        // own ack.
        Task { @MainActor [weak self] in
            do {
                var payload: [String: Any] = ["ask_id": requestId]
                if let reply { payload["reply"] = reply }
                else        { payload["reply"] = NSNull() }
                _ = try await self?.sendRequest(
                    "user_reply", payload: payload)
            } catch {
                NSLog("[agentd] sending user_reply failed: %@",
                      "\(error)")
            }
        }
    }

    // MARK: - Smoke test

    /// Smoke test: connect, ping, start the stubbed task, wait for
    /// its two events, disconnect. Logs each step via NSLog so the
    /// run shows up in Console.app under process == OpenSeerGUI.
    func runProbe() async {
        NSLog("[agentd] probe: starting")
        do {
            try await connect()
            let pong = try await sendRequest(
                "ping", payload: ["payload": "hi"])
            NSLog("[agentd] probe: pong=%@", "\(pong)")

            let ack = try await sendRequest(
                "start_task", payload: ["task": "probe"])
            guard let runId = ack["run_id"] as? String else {
                NSLog("[agentd] probe: ack missing run_id: %@", "\(ack)")
                return
            }
            NSLog("[agentd] probe: ack run_id=%@", runId)

            // Collect both stub events. The receive loop pushes them
            // into our handler; we resume the continuation when the
            // terminator (`task_finished`) arrives.
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                var finished = false
                observe(runId: runId) { msg in
                    guard !finished else { return }
                    let event = msg["event"] as? [String: Any]
                    let et = event?["type"] as? String ?? "?"
                    NSLog("[agentd] probe: event %@", et)
                    if et == "task_finished" || et == "task_failed" {
                        finished = true
                        cont.resume()
                    }
                }
            }
            NSLog("[agentd] probe: PASS")
        } catch {
            NSLog("[agentd] probe: FAIL — %@", "\(error)")
        }
    }
}
