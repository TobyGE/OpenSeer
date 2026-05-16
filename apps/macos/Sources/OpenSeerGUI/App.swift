import AppKit
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
                .onAppear {
                    NSApp.activate(ignoringOtherApps: true)
                    promoteMainWindowToFront()
                }
        }
        .windowResizability(.contentSize)
    }

    /// Force the SwiftUI-created main window to make-key + order-front.
    /// SwiftUI's saved-state restoration path occasionally creates the
    /// window without bringing it forward, which manifests as "OpenSeer
    /// is running but no window appears" on relaunch — the floating
    /// voice orb panel is up and steals attention while the main
    /// content window sits below other apps. We pick the first non-
    /// panel window (the orb is an NSPanel with `.nonactivatingPanel`,
    /// so isKind(of: NSPanel) excludes it) and explicitly raise it.
    private func promoteMainWindowToFront() {
        // Defer one runloop tick — SwiftUI hasn't necessarily inserted
        // the window into NSApp.windows yet by the time .onAppear fires
        // on the first redraw.
        DispatchQueue.main.async {
            for w in NSApp.windows where !w.isKind(of: NSPanel.self) {
                if w.contentViewController != nil || w.contentView != nil {
                    w.makeKeyAndOrderFront(nil)
                    break
                }
            }
        }
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
                MainView(binary: env.binaryPath ?? "/usr/local/bin/openseer")
            case .error(let msg):
                ErrorView(message: msg)
            }
        }
        .task { await env.refresh() }
    }
}

struct MainView: View {
    @StateObject private var controller: MainController

    init(binary: String) {
        _controller = StateObject(
            wrappedValue: MainController(binary: binary))
    }

    var body: some View {
        ChatView(controller: controller)
        // The floating voice orb is no longer auto-shown on launch.
        // Users summon it with control+S and dismiss it with the
        // same shortcut — see GlobalHotkey + MainController.
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
