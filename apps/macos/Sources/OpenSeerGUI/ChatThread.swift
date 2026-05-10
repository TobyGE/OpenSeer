import Foundation
import SwiftUI
import Combine

/// A logical conversation thread. Holds 1..N RunSession objects in
/// chronological order. The session list shows ChatThreads (not raw
/// runs); the detail pane concatenates every run's turns.
///
/// Identity:
///   - Telegram traces group by `tg:<chat_id>` so successive messages
///     in the same chat accumulate as turns of one ongoing thread.
///   - Local prompts each get their own thread (`local:<uuid>`) — the
///     GUI composer doesn't yet have a "continue this thread" UX, so
///     every Send is its own conversation. Easy to lift later.
@MainActor
final class ChatThread: ObservableObject, Identifiable {
    let id: String              // e.g. "tg:12345" or "local:<uuid>"
    let kind: Kind
    @Published var runs: [RunSession] = []
    /// Forward subscriptions: when a child RunSession publishes a
    /// change (turns appended, status flipped on task_finished), we
    /// re-publish so the SessionRow watching `thread.title` /
    /// `thread.status` re-evaluates. Without this the list row
    /// stays frozen on the trace-id fallback even after events
    /// have been parsed.
    private var childCancellables: [UUID: AnyCancellable] = [:]

    enum Kind: Equatable {
        case local
        case telegram(chatId: Int64)
    }

    init(id: String, kind: Kind) {
        self.id = id
        self.kind = kind
    }

    var sortedRuns: [RunSession] {
        runs.sorted { $0.createdAt < $1.createdAt }
    }

    /// Newest run's createdAt — drives session-list ordering and the
    /// "12s ago" relative-time label.
    var lastActivity: Date {
        runs.map { $0.createdAt }.max() ?? .distantPast
    }

    /// Title shown in the session list. Prefer the FIRST user prompt
    /// in the thread; fall back to most-recent if the first run hasn't
    /// hydrated its task yet (daemon traces tail events.jsonl async).
    var title: String {
        if let first = sortedRuns.first(where: { !$0.title.isEmpty
                                                && $0.title != "New session" }) {
            return first.title
        }
        return runs.last?.title ?? "New session"
    }

    /// Latest run's status — drives the colored dot in the row.
    var status: RunSession.Status {
        sortedRuns.last?.status ?? .done
    }

    /// True if any run in this thread is currently running.
    var isAnyRunActive: Bool {
        runs.contains { $0.status == .running }
    }

    /// Append a run to the thread. No-op if already present. Also
    /// subscribes to the run's objectWillChange so this thread
    /// re-publishes whenever a child run's turns / status update.
    func addRun(_ s: RunSession) {
        guard !runs.contains(where: { $0.id == s.id }) else { return }
        runs.append(s)
        childCancellables[s.id] = s.objectWillChange
            .sink { [weak self] _ in
                // Force SessionRow / detail-pane re-render. We're
                // already on the main actor (RunSession is @MainActor).
                self?.objectWillChange.send()
            }
    }

    /// Format prior runs into the same `RECENT SESSION CONTEXT`
    /// block the Telegram daemon's ChatSessions injects, so a
    /// continued thread's follow-up prompts give the agent the same
    /// visibility the user assumes from the GUI grouping. Caps at
    /// the most recent 5 runs to keep the prompt tight.
    func renderSessionContext() -> String {
        let prior = sortedRuns.suffix(5)
        guard !prior.isEmpty else { return "" }
        var lines = ["RECENT SESSION CONTEXT (read-only, prior tasks "
                     + "in this conversation):"]
        for run in prior {
            let task = run.title
                .replacingOccurrences(of: "\n", with: " ")
            let status = describe(run.status)
            let result = lastResult(of: run)
                .replacingOccurrences(of: "\n", with: " ")
                .prefix(140)
            let summary = result.isEmpty
                ? "\"\(task)\" → \(status)"
                : "\"\(task)\" → \(status): \(result)"
            lines.append("  - " + summary)
        }
        lines.append("END SESSION CONTEXT")
        return lines.joined(separator: "\n")
    }

    private func describe(_ s: RunSession.Status) -> String {
        switch s {
        case .running: return "running"
        case .done:    return "done"
        case .fail:    return "fail"
        case .cap:     return "cap"
        case .interrupted: return "interrupted"
        }
    }

    private func lastResult(of run: RunSession) -> String {
        // Prefer terminate.reason (the model's user-facing reply);
        // otherwise fall back to the last action's brief result.
        for turn in run.turns.reversed() {
            if let final = turn.finalOutput, !final.isEmpty {
                return final
            }
            if let last = turn.actions.last, !last.result.isEmpty {
                return last.result
            }
        }
        return ""
    }
}

/// Sidecar `chat.json` that the daemon writes alongside every run.
/// Used by the GUI to determine which thread a run belongs to.
struct ChatMeta: Decodable {
    let kind: String
    let chatId: Int64

    enum CodingKeys: String, CodingKey {
        case kind
        case chatId = "chat_id"
    }

    /// Read `<runDir>/chat.json` if present. Returns nil for older
    /// runs (which never group; they fall back to per-run threads).
    static func load(runDir: String) -> ChatMeta? {
        let path = runDir + "/chat.json"
        guard let data = FileManager.default.contents(atPath: path) else {
            return nil
        }
        return try? JSONDecoder().decode(ChatMeta.self, from: data)
    }
}
