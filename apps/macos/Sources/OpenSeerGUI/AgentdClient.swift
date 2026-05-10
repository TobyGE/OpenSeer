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

    private var nextRequestId = 0

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
    /// here in order. Handler is removed automatically when a
    /// `task_finished` / `task_failed` event has been delivered.
    func observe(runId: String,
                 handler: @escaping ([String: Any]) -> Void) {
        eventHandlers[runId] = handler
        if let buffered = pendingEvents.removeValue(forKey: runId) {
            for msg in buffered {
                handler(msg)
            }
        }
        if pendingTerminated.remove(runId) != nil {
            eventHandlers.removeValue(forKey: runId)
        }
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
        if type == "event", let runId = msg["run_id"] as? String {
            let inner = msg["event"] as? [String: Any]
            let et = inner?["type"] as? String
            let terminator = (et == "task_finished" || et == "task_failed")
            if let handler = eventHandlers[runId] {
                handler(msg)
                if terminator {
                    eventHandlers.removeValue(forKey: runId)
                }
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
