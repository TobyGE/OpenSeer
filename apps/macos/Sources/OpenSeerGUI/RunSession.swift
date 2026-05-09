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

    @Published var turns: [Turn] = []
    @Published var status: Status = .running
    @Published var traceId: String? = nil
    @Published var errorMessage: String? = nil

    enum Status: Equatable {
        case running, done, fail, cap, interrupted
    }

    private var stream: CLI.StreamHandle?
    private var watcher: FileTail?
    private let binary: String

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
                    onTraceFound: ((String) -> Void)? = nil) {
        guard case .localPrompt = source else { return }
        let runsDir = NSHomeDirectory() + "/.openseer/runs"

        var args = ["task", prompt]
        if !dryRun { args.append("--execute") }
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
            return
        }

        // 5-second deadline to see the trace_id line. If the CLI
        // doesn't print one (older builds, or it died before
        // logging), surface that and fail the session — without a
        // watcher we'll never get task_finished events.
        Task.detached { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
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
        Task { [weak self] in
            guard let stream = self?.stream else { return }
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

    func cancel() {
        stream?.terminate()
        watcher?.stop()
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
        // Capture trace_id from task_started (first event of the run).
        if ev.type == "task_started", let tid = ev.data["trace_id"]?.string {
            traceId = tid
        }
        TurnFolder.apply(ev, to: &turns)
        if ev.type == "task_finished" {
            let st = ev.data["status"]?.string ?? "done"
            status = (st == "done" ? .done
                      : st == "cap" ? .cap : .fail)
        } else if ev.type == "task_failed" {
            status = .fail
            errorMessage = ev.data["error"]?.string
        }
        // Trigger SwiftUI redraw.
        objectWillChange.send()
    }

    private func json2task(at path: String) -> String? {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj["task"] as? String
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
