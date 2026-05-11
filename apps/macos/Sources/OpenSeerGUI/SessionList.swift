import SwiftUI

/// Middle column: list of conversation threads. One row per
/// ChatThread (newest activity first). A row may contain many
/// sequential runs (e.g. all messages from one Telegram chat).
struct SessionListView: View {
    @ObservedObject var daemon: DaemonController
    @Binding var selectedID: String?
    var onNew: () -> Void
    @State private var pendingDelete: ChatThread? = nil

    private var ordered: [ChatThread] {
        daemon.threads.sorted { $0.lastActivity > $1.lastActivity }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            list
        }
        .frame(maxWidth: .infinity)
        .background(.background.tertiary)
        .alert("Delete this conversation?",
               isPresented: Binding(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } })) {
            Button("Cancel", role: .cancel) { pendingDelete = nil }
            Button("Delete", role: .destructive) {
                if let t = pendingDelete {
                    if selectedID == t.id { selectedID = nil }
                    daemon.deleteThread(t.id, deleteRunDirs: true)
                }
                pendingDelete = nil
            }
        } message: {
            let t = pendingDelete
            Text(t?.title ?? "")
                + Text("\n\n\(t?.runs.count ?? 0) run(s) and their "
                       + "events.jsonl + screenshots will be deleted "
                       + "from disk.")
        }
    }

    private var header: some View {
        HStack {
            Text("Sessions").font(.headline)
            Spacer()
            Button {
                onNew()
            } label: {
                Image(systemName: "square.and.pencil")
            }
            .buttonStyle(.borderless)
            .help("Start a new chat")
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    private var list: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) {
                if ordered.isEmpty {
                    Text("No sessions yet — type a task or send to "
                         + "the Telegram bot to start.")
                        .font(.caption).foregroundStyle(.secondary)
                        .padding(12)
                }
                ForEach(ordered) { t in
                    SessionRow(thread: t,
                               selected: t.id == selectedID)
                        .contentShape(Rectangle())
                        .onTapGesture { selectedID = t.id }
                        .contextMenu {
                            Button(role: .destructive) {
                                pendingDelete = t
                            } label: {
                                Label("Delete conversation",
                                      systemImage: "trash")
                            }
                        }
                }
            }
            .padding(.vertical, 4)
        }
    }
}

private struct SessionRow: View {
    @ObservedObject var thread: ChatThread
    let selected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            statusDot
            VStack(alignment: .leading, spacing: 2) {
                Text(thread.title)
                    .font(.callout)
                    .lineLimit(2)
                    .truncationMode(.tail)
                HStack(spacing: 6) {
                    Text(sourceLabel)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    if thread.runs.count > 1 {
                        Text("·").foregroundStyle(.tertiary).font(.caption2)
                        Text("\(thread.runs.count) runs")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    Text("·").foregroundStyle(.tertiary).font(.caption2)
                    Text(relativeTime)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(selected ? Color.accentColor.opacity(0.18) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .padding(.horizontal, 6)
    }

    private var statusDot: some View {
        Circle()
            .fill(dotColor)
            .frame(width: 8, height: 8)
            .padding(.top, 6)
    }

    private var dotColor: Color {
        switch thread.status {
        case .running: return .blue
        case .done:    return .green
        case .fail:    return .orange
        case .cap:     return .gray
        case .interrupted: return .secondary
        case .held:    return .yellow
        }
    }

    private var sourceLabel: String {
        switch thread.kind {
        case .local: return "local"
        case .telegram: return "Telegram"
        }
    }

    private var relativeTime: String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: thread.lastActivity,
                                 relativeTo: Date())
    }
}
