import Foundation
import SwiftUI

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

    /// Append a run to the thread. No-op if already present.
    func addRun(_ s: RunSession) {
        if !runs.contains(where: { $0.id == s.id }) {
            runs.append(s)
        }
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
