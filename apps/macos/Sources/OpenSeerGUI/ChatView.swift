import SwiftUI

/// Phase 2 placeholder. Subsequent commits add: text input + send,
/// turn-aggregated bubble rendering, expandable per-step disclosure,
/// and the daemon-control panel from Phase 3.
struct ChatView: View {
    @EnvironmentObject var env: OpenSeerEnv

    var body: some View {
        VStack(spacing: 0) {
            ChatHeader()
            Divider()
            ChatPlaceholder()
        }
    }
}

private struct ChatHeader: View {
    @EnvironmentObject var env: OpenSeerEnv
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("OpenSeer").font(.headline)
                Text(env.authSummary)
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.tail)
            }
            Spacer()
            Button("Settings") { /* Phase 4 */ }
                .disabled(true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

private struct ChatPlaceholder: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("Chat window goes here.")
                .font(.title3)
                .foregroundStyle(.secondary)
            Text("Phase 2 lands the input box, subprocess runner, and turn-aggregated bubbles.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.tertiary)
                .frame(maxWidth: 420)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
