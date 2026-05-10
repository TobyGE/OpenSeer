import Foundation
import SwiftUI

/// Daemon lifecycle: start / stop a long-running `openseer daemon`
/// subprocess, watch ~/.openseer/runs/ for new trace dirs that the
/// daemon spawns, and group every run into a `ChatThread` so the GUI
/// can show one row per ongoing conversation rather than one per task.
@MainActor
final class DaemonController: ObservableObject {
    @Published private(set) var isRunning: Bool = false
    @Published private(set) var startupError: String? = nil
    /// Threads visible in the session list. Each thread holds 1..N
    /// runs. Telegram traces with the same chat_id share a thread;
    /// every local prompt gets its own thread.
    @Published private(set) var threads: [ChatThread] = []

    private var stream: CLI.StreamHandle?
    private var runsWatcher: DirectoryWatcher?
    private var pollTimer: Timer?
    private let binary: String
    /// Bumped every start/stop. The wait task captures the value at
    /// spawn time and only mutates state if it still matches —
    /// otherwise a stale wait from an old handle would clobber the
    /// freshly-started daemon's state after a rapid Stop+Start.
    private var daemonGen: Int = 0
    /// True while the user-initiated stop is in flight, so the wait
    /// block doesn't surface SIGTERM-style exits as startup errors.
    private var intentionalStop: Bool = false
    /// Local prompts that have been spawned but whose run dir
    /// hasn't been observed yet. The directory watcher reads
    /// task.json from each new dir and, if the prompt matches a
    /// pending claim, skips it instead of producing a duplicate
    /// daemon-side session.
    private var pendingLocalPrompts: [String] = []
    /// trace_ids already attached so the directory watcher doesn't
    /// double-spawn sessions when the daemon writes multiple files
    /// into a new run dir.
    private var seenTraces: Set<String> = []

    init(binary: String) {
        self.binary = binary
        // On startup, scan existing run dirs so the chat shows
        // recent history rather than appearing empty.
        loadRecentRuns(limit: 16)
    }

    func start() {
        guard !isRunning else { return }
        startupError = nil
        let h = CLI.stream(path: binary, args: ["daemon"]) { line in
            if !line.hasPrefix("[stderr] ") {
                NSLog("openseer daemon: %@", line)
            }
        }
        do {
            try h.start()
            self.stream = h
            isRunning = true
            daemonGen += 1
            let myGen = daemonGen
            startWatcher()
            Task {
                let exit = await h.wait()
                await MainActor.run {
                    guard myGen == self.daemonGen else { return }
                    self.isRunning = false
                    if exit != 0 && self.startupError == nil
                        && !self.intentionalStop {
                        self.startupError = "Daemon exited with code \(exit)"
                    }
                    self.intentionalStop = false
                }
            }
        } catch {
            startupError = "spawn failed: \(error)"
        }
    }

    func stop() {
        intentionalStop = true
        daemonGen += 1
        stream?.terminate()
        stream = nil
        runsWatcher?.stop()
        runsWatcher = nil
        pollTimer?.invalidate()
        pollTimer = nil
        isRunning = false
    }

    /// Stop the daemon AND block until the subprocess actually
    /// exits. `stop()` only sends SIGTERM and clears state — the
    /// caller (e.g. factory reset) needs to be sure no in-memory
    /// bot token / session state can still write back to disk
    /// after we wipe config files. Idempotent; safe to call when
    /// already stopped.
    func stopAndWait() async {
        guard let s = stream else { stop(); return }
        intentionalStop = true
        daemonGen += 1
        s.terminate()
        // Drop the references so other code paths see "stopped",
        // then await the wait() future on our local copy.
        stream = nil
        runsWatcher?.stop()
        runsWatcher = nil
        pollTimer?.invalidate()
        pollTimer = nil
        isRunning = false
        _ = await s.wait()
    }

    /// ChatView calls this when the user submits a local prompt.
    /// If `continueThread` is the id of an existing local thread
    /// (the one the user has selected in the sidebar), the new run
    /// is appended to it so successive prompts accumulate as turns
    /// of the same conversation. Otherwise a fresh thread is
    /// created. Telegram threads are never continued from a local
    /// composer — those belong to the bot's chat_id.
    func addLocalRun(_ s: RunSession,
                     continueThread: String? = nil) -> ChatThread {
        if let id = continueThread,
           let existing = threads.first(where: {
               $0.id == id && $0.kind == .local
           }) {
            existing.addRun(s)
            // Bump @Published so SessionListView re-sorts (the
            // newly-added run updates lastActivity, which is read
            // off thread.runs).
            objectWillChange.send()
            return existing
        }
        let id = "local:" + UUID().uuidString
        let t = ChatThread(id: id, kind: .local)
        t.addRun(s)
        threads.append(t)
        return t
    }

    /// Remove a thread (and every run it contains). When
    /// `deleteRunDirs` is true, also wipe the on-disk trace dirs so
    /// they don't reappear next launch via `loadRecentRuns`.
    func deleteThread(_ id: String, deleteRunDirs: Bool) {
        guard let idx = threads.firstIndex(where: { $0.id == id })
        else { return }
        let t = threads[idx]
        for run in t.runs where run.status == .running { run.cancel() }
        threads.remove(at: idx)
        if deleteRunDirs {
            for run in t.runs {
                guard let tid = run.traceId else { continue }
                let runDir = NSHomeDirectory()
                    + "/.openseer/runs/" + tid
                try? FileManager.default.removeItem(atPath: runDir)
            }
        }
    }

    /// Local sessions call this once their subprocess prints its
    /// trace id. Reserves the id in seenTraces so the directory
    /// watcher's later observation doesn't double-spawn it as a
    /// daemon trace.
    func reserveLocalTrace(_ traceId: String, prompt: String) {
        seenTraces.insert(traceId)
        if let i = pendingLocalPrompts.firstIndex(of: prompt) {
            pendingLocalPrompts.remove(at: i)
        }
    }

    /// ChatView.submit calls this BEFORE spawning the local task,
    /// so even if the directory watcher fires before the subprocess
    /// has printed `[agent] out_dir=…` we know to skip the new dir.
    func claimLocalPrompt(_ prompt: String) {
        pendingLocalPrompts.append(prompt)
    }

    private func consumePendingClaim(for dir: String) -> Bool {
        let taskPath = dir + "/task.json"
        guard let data = FileManager.default.contents(atPath: taskPath),
              let obj = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let prompt = obj["task"] as? String else {
            return false
        }
        if let i = pendingLocalPrompts.firstIndex(of: prompt) {
            pendingLocalPrompts.remove(at: i)
            return true
        }
        return false
    }

    private func startWatcher() {
        let dir = NSHomeDirectory() + "/.openseer/runs"
        try? FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true)
        runsWatcher = DirectoryWatcher(path: dir) { [weak self] in
            Task { @MainActor in self?.scanForNewRuns() }
        }
        runsWatcher?.start()

        // Belt-and-braces poll: parent-dir events fire on dir
        // creation, but task.json/chat.json are written INTO the
        // new dir later, which doesn't re-trigger the parent
        // watcher. Cheap to poll every 2s — one syscall.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0,
                                         repeats: true) { [weak self] _ in
            Task { @MainActor in self?.scanForNewRuns() }
        }
    }

    private func scanForNewRuns() {
        let dir = NSHomeDirectory() + "/.openseer/runs"
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: dir)
        else { return }
        for name in entries where name != "latest" && !seenTraces.contains(name) {
            let runDir = dir + "/" + name
            let taskFile = runDir + "/task.json"
            let chatFile = runDir + "/chat.json"
            // task.json is written first by TrajectoryCallback;
            // chat.json a moment later by _ActiveRunTracker. There
            // is a brief window where task.json exists but
            // chat.json doesn't — codex P2: if we marked the trace
            // seen now and fell back to a local thread, the next
            // poll would skip the dir and the Telegram conversation
            // would be split into a fresh standalone session.
            //
            // Strategy: defer (don't insert seenTraces) for up to
            // ~6s if chat.json is missing AND the dir is fresh.
            // After that we accept whatever's there (older traces
            // legitimately have no chat.json).
            guard FileManager.default.fileExists(atPath: taskFile) else { continue }
            if !FileManager.default.fileExists(atPath: chatFile),
               isDirFresh(runDir, withinSeconds: 6) {
                continue   // wait for chat.json on the next poll
            }
            seenTraces.insert(name)
            if consumePendingClaim(for: runDir) { continue }

            let s = RunSession(source: .daemonTrace(traceId: name),
                               binary: binary)
            s.attachToTrace(name)
            attachRunToThread(s, runDir: runDir)
        }
    }

    /// True if the dir was created within `withinSeconds` ago.
    /// Used to give chat.json a chance to appear before we commit
    /// to a thread classification.
    private func isDirFresh(_ path: String,
                            withinSeconds: TimeInterval) -> Bool {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
              let mtime = attrs[.modificationDate] as? Date else { return false }
        return Date().timeIntervalSince(mtime) < withinSeconds
    }

    /// Slot a daemon-spawned run into the right ChatThread, creating
    /// the thread if it doesn't exist. Falls back to a per-run thread
    /// when chat.json is missing (older traces).
    private func attachRunToThread(_ s: RunSession, runDir: String) {
        let key: String
        let kind: ChatThread.Kind
        if let meta = ChatMeta.load(runDir: runDir) {
            key = "tg:\(meta.chatId)"
            kind = .telegram(chatId: meta.chatId)
        } else {
            // No chat.json — likely a pre-grouping trace OR a local
            // run whose dir we observed (claim should have caught
            // it; this is the safety net). Use the trace id as the
            // thread key so it stands alone.
            key = "trace:" + (s.traceId ?? UUID().uuidString)
            kind = .local
        }
        if let existing = threads.first(where: { $0.id == key }) {
            existing.addRun(s)
            // Bump @Published so SwiftUI re-sorts the list.
            objectWillChange.send()
        } else {
            let t = ChatThread(id: key, kind: kind)
            t.addRun(s)
            threads.append(t)
        }
    }

    private func loadRecentRuns(limit: Int) {
        let dir = NSHomeDirectory() + "/.openseer/runs"
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: dir)
        else { return }
        let dirs = entries.filter { $0 != "latest" }
        // Mark ALL existing dirs as seen so the periodic poll
        // doesn't treat older traces as "new daemon runs" once the
        // daemon is started.
        for name in dirs { seenTraces.insert(name) }
        let withMtime: [(String, Date)] = dirs.compactMap { name in
            let p = dir + "/" + name
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: p),
                  let m = attrs[.modificationDate] as? Date else { return nil }
            return (name, m)
        }.sorted { $0.1 > $1.1 }
        for (name, mtime) in withMtime.prefix(limit) {
            let runDir = dir + "/" + name
            let s = RunSession(source: .daemonTrace(traceId: name),
                               binary: binary)
            s.createdAt = mtime
            s.attachToTrace(name)
            attachRunToThread(s, runDir: runDir)
        }
    }
}

/// Watch a directory for entry-list changes (creation/deletion of
/// children) via DispatchSource.
final class DirectoryWatcher: @unchecked Sendable {
    private let path: String
    private let onChange: @Sendable () -> Void
    private var fd: Int32 = -1
    private var src: DispatchSourceFileSystemObject?

    init(path: String, onChange: @escaping @Sendable () -> Void) {
        self.path = path
        self.onChange = onChange
    }

    func start() {
        fd = open(path, O_EVTONLY)
        guard fd >= 0 else { return }
        let q = DispatchQueue(label: "openseer.dirwatch", qos: .background)
        let s = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .extend, .rename], queue: q)
        s.setEventHandler { [weak self] in self?.onChange() }
        s.setCancelHandler { [fd = self.fd] in
            if fd >= 0 { close(fd) }
        }
        s.resume()
        src = s
    }

    func stop() {
        src?.cancel()
        src = nil
        fd = -1
    }

    deinit { stop() }
}
