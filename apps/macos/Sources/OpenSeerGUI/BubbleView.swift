import SwiftUI

/// One agent turn rendered as a collapsible bubble, plus the user-
/// prompt variant. Three layout shapes:
///   - userPrompt: right-aligned, accent-colored, no expansion
///   - turnHeadline: left-aligned, "thought" + reflection chip,
///     disclosure to step list
///   - finalSummary: emitted via Turn objects when task ended
struct BubbleView: View {
    let turn: Turn
    @State private var expanded: Bool = false

    var body: some View {
        if turn.isUserPrompt {
            userBubble
        } else {
            agentBubble
        }
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 60)
            Text(turn.promptText)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(Color.accentColor.opacity(0.85))
                .foregroundStyle(.white)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .frame(maxWidth: 520, alignment: .trailing)
        }
    }

    private var agentBubble: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "circle.hexagonpath.fill")
                .foregroundStyle(.purple)
                .font(.title3)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 6) {
                headline
                if expanded || !turn.actions.isEmpty {
                    actionList
                }
                tokenLine
            }
            Spacer(minLength: 30)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.background.secondary)
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var headline: some View {
        HStack(alignment: .firstTextBaseline) {
            if !turn.reflection.isEmpty {
                Text(turn.reflection)
                    .font(.caption2.weight(.bold))
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(reflectionColor(turn.reflection))
                    .foregroundStyle(.white)
                    .clipShape(Capsule())
            }
            Text(thoughtMinusReflection)
                .font(.callout)
                .lineLimit(expanded ? nil : 3)
            Spacer()
            if turn.actions.count > 1 {
                Button {
                    withAnimation { expanded.toggle() }
                } label: {
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
            }
        }
    }

    private var actionList: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(turn.actions) { a in
                HStack(alignment: .top, spacing: 6) {
                    Text("•").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(a.summary).font(.caption).monospaced()
                        if expanded, !a.result.isEmpty,
                           !["clicked", "pressed", "typed", "scrolled", "waited"]
                                .contains(where: { a.result.hasPrefix($0) })
                        {
                            Text(a.result).font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(expanded ? nil : 1)
                        }
                    }
                }
            }
        }
        .padding(.leading, 4)
    }

    private var tokenLine: some View {
        Group {
            if turn.finished && (turn.inputTokens > 0 || turn.elapsedMs > 0) {
                HStack(spacing: 8) {
                    if turn.inputTokens > 0 {
                        Text("\(turn.inputTokens)+\(turn.outputTokens)t")
                    }
                    if turn.elapsedMs > 0 {
                        Text("\(turn.elapsedMs)ms")
                    }
                    Spacer()
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }
        }
    }

    private var thoughtMinusReflection: String {
        guard !turn.reflection.isEmpty,
              turn.thought.hasPrefix(turn.reflection) else {
            return turn.thought
        }
        var s = turn.thought.dropFirst(turn.reflection.count)
        while s.first == ":" || s.first == " " { s = s.dropFirst() }
        return String(s)
    }

    private func reflectionColor(_ tok: String) -> Color {
        switch tok {
        case "[SUCCESS]":     return .green
        case "[INEFFECTIVE]": return .orange
        case "[REGRESSED]":   return .red
        default:              return .gray
        }
    }
}

/// Status footer for a session: showing run state ("3 turns · done · 12s"
/// or a spinner while running).
struct SessionFooter: View {
    @ObservedObject var session: RunSession
    var body: some View {
        HStack(spacing: 6) {
            switch session.status {
            case .running:
                ProgressView().controlSize(.small)
                Text("running…").font(.caption)
            case .done:
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text("done").font(.caption)
            case .fail:
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.orange)
                Text("fail").font(.caption)
            case .cap:
                Image(systemName: "stopwatch")
                    .foregroundStyle(.secondary)
                Text("step cap").font(.caption)
            case .interrupted:
                Image(systemName: "pause.circle")
                    .foregroundStyle(.secondary)
                Text("interrupted").font(.caption)
            }
            if let tid = session.traceId {
                Text("· \(tid.prefix(8))")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
            if case .daemonTrace = session.source {
                Text("· remote (Telegram)").font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            // Local subprocess we own → real cancel is meaningful
            // (terminate the process). For .daemonTrace runs we
            // don't have process control; the underlying remote
            // task would keep going. Hide the button rather than
            // mislead the user.
            if session.status == .running {
                if case .localPrompt = session.source {
                    Button("Cancel") { session.cancel() }
                        .controlSize(.small)
                }
            }
        }
        .padding(.horizontal, 12)
    }
}
