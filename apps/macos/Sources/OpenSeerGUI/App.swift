import SwiftUI

/// SwiftUI app entry. Single window for now; we can split into a
/// dedicated settings window later (Phase 4) if it grows.
@main
struct OpenSeerApp: App {
    @StateObject private var openseer = OpenSeerEnv.shared

    var body: some Scene {
        WindowGroup("OpenSeer") {
            RootView()
                .environmentObject(openseer)
                .frame(minWidth: 960, minHeight: 600)
        }
        .windowResizability(.contentSize)
    }
}

/// RootView decides whether to show the setup wizard (first run / missing
/// auth) or the main chat window. The decision is a simple `needsSetup`
/// computed from `OpenSeerEnv.status`; the env refreshes on launch and
/// after the wizard completes.
struct RootView: View {
    @EnvironmentObject var env: OpenSeerEnv

    var body: some View {
        Group {
            switch env.status {
            case .loading:
                ProgressView("Checking OpenSeer status…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .needsSetup:
                SetupView()
            case .ready:
                ChatView()
            case .error(let msg):
                ErrorView(message: msg)
            }
        }
        .task { await env.refresh() }
    }
}

struct ErrorView: View {
    let message: String
    @EnvironmentObject var env: OpenSeerEnv
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 48))
                .foregroundStyle(.orange)
            Text("OpenSeer can't start")
                .font(.title2.bold())
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 40)
            Button("Retry") {
                Task { await env.refresh() }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
