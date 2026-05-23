import AppKit
import Combine
import SwiftUI

@MainActor
final class FloatingVoiceOrbWindow {
    static let shared = FloatingVoiceOrbWindow()

    private var panel: VoiceOrbPanel?
    private var hosting: NSHostingView<VoiceOrbWindowRoot>?
    private var lockHandle: FileHandle?
    private var appActivationObserver: NSObjectProtocol?

    private init() {}

    /// Toggle the floating orb's visibility. Returns `true` if the
    /// panel is now visible, `false` if hidden. Called from the
    /// global hotkey: same key summons and dismisses the orb so it
    /// only occupies screen space when the user wants it there.
    @discardableResult
    func toggle(controller: MainController) -> Bool {
        if let panel, panel.isVisible {
            // Tear down listening + collapse VoiceOrbView's open
            // state BEFORE hiding the panel. Without this, SFSpeech
            // / VoiceInput stay live, recording invisibly, and the
            // next utterance auto-submits a task the user can't see
            // they were triggering (codex P1 on 8e9cb5e).
            NotificationCenter.default.post(
                name: .dismissVoiceOrb, object: nil)
            panel.orderOut(nil)
            return false
        }
        show(controller: controller)
        return true
    }

    func show(controller: MainController) {
        guard acquireInstanceLockIfNeeded() else { return }
        let root = VoiceOrbWindowRoot(
            controller: controller,
            onExpansionChange: { [weak self] expanded in
                self?.resize(expanded: expanded)
            }
        )
        if panel != nil, let hosting {
            hosting.rootView = root
            raisePanel(showIfNeeded: true)
            return
        }

        let hosting = NSHostingView(rootView: root)
        hosting.frame = NSRect(origin: .zero, size: Self.collapsedSize)

        let panel = VoiceOrbPanel(
            contentRect: hosting.frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentView = hosting
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        // Keep the orb out of OpenSeer's Quartz screenshots. The
        // Python agent captures the full screen, so without this the
        // floating panel can block the very content it is trying to
        // inspect.
        panel.sharingType = .none
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = true
        // `.floating` is not sticky enough across app activation; when
        // Chrome becomes active it can reorder its normal windows above
        // another app's floating panel. Status-bar level keeps the orb
        // above ordinary app windows without jumping to screen-saver level.
        panel.level = .statusBar
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .ignoresCycle,
            .stationary,
        ]

        self.panel = panel
        self.hosting = hosting
        place(panel)
        installFrontmostObserver()
        raisePanel(showIfNeeded: true)
    }

    private static let collapsedSize = NSSize(width: 96, height: 96)
    // Tall enough to hold the panel + step bubble + answer bubble +
    // orb stack with fixed-height ScrollViews inside each. If your
    // screen is small enough that this still pokes off-screen, you'll
    // see the top bubble's content clipped — the orb stays put.
    private static let expandedSize = NSSize(width: 360, height: 640)

    private func acquireInstanceLockIfNeeded() -> Bool {
        if lockHandle != nil { return true }
        let dir = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent(".openseer", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("voice-orb.lock")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let handle = try? FileHandle(forWritingTo: url) else {
            return true
        }
        if flock(handle.fileDescriptor, LOCK_EX | LOCK_NB) == 0 {
            lockHandle = handle
            return true
        }
        try? handle.close()
        return false
    }

    private func place(_ panel: NSPanel) {
        guard let screen = NSScreen.screens.first ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        let origin = NSPoint(
            x: visible.maxX - size.width - 22,
            y: visible.minY + 22
        )
        panel.setFrameOrigin(origin)
    }

    private func installFrontmostObserver() {
        guard appActivationObserver == nil else { return }
        appActivationObserver = NSWorkspace.shared.notificationCenter
            .addObserver(
                forName: NSWorkspace.didActivateApplicationNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    self?.raisePanel()
                }
            }
    }

    private func raisePanel(showIfNeeded: Bool = false) {
        guard let panel else { return }
        guard showIfNeeded || panel.isVisible else { return }
        panel.level = .statusBar
        panel.orderFrontRegardless()
    }

    /// Two-state resize: collapsed (just the orb) or expanded (room
    /// for panel + bubbles + orb). Anchor is the panel's bottom-right
    /// corner, so the orb stays at the same screen position across
    /// the switch.
    private func resize(expanded: Bool) {
        guard let panel else { return }
        let newSize = expanded ? Self.expandedSize : Self.collapsedSize
        let old = panel.frame
        var frame = NSRect(
            x: old.maxX - newSize.width,
            y: old.minY,
            width: newSize.width,
            height: newSize.height
        )
        if let screen = panel.screen ?? NSScreen.screens.first ?? NSScreen.main {
            let visible = screen.visibleFrame
            frame.origin.x = min(max(frame.origin.x, visible.minX + 8),
                                 visible.maxX - newSize.width - 8)
            // Clamp y so the orb (bottom-right) stays inside the
            // visible area even when expandedSize is taller than the
            // screen. The top will be clipped, not the orb.
            frame.origin.y = max(frame.origin.y, visible.minY + 8)
        }
        panel.setFrame(frame, display: true, animate: true)
    }
}

private final class VoiceOrbPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

private struct VoiceOrbWindowRoot: View {
    @ObservedObject var controller: MainController
    let onExpansionChange: (Bool) -> Void
    @State private var expanded = false
    @StateObject private var liveObserver = LiveTurnObserver()

    var body: some View {
        VStack {
            Spacer(minLength: 0)
            HStack {
                Spacer(minLength: 0)
                VoiceOrbView(
                    // .held counts as "task in progress" for orb
                    // semantics — barge-in, etc. still want to see
                    // an active task. Held vs running is then
                    // signaled separately via isTaskHeld for the
                    // Hold/Resume button label.
                    //
                    // Source-of-truth here is liveObserver, not
                    // controller.selectedRunningRun, because the
                    // observer subscribes directly to the run's
                    // objectWillChange. MainController's @Published
                    // fields don't fire on run.status transitions,
                    // so reading status off the controller misses
                    // the running↔held flip.
                    isTaskRunning: liveObserver.runStatus == .running
                        || liveObserver.runStatus == .held,
                    isTaskHeld: liveObserver.runStatus == .held,
                    spokenAnswer: controller.voiceAnswer,
                    liveStep: liveObserver.current,
                    pendingLesson: liveObserver.pendingLesson,
                    savedSkillName: liveObserver.lastAppliedSkillName,
                    onSubmit: controller.submitVoicePrompt,
                    onAnswerConsumed: { controller.voiceAnswer = nil },
                    onApplyLesson: {
                        if let run = controller.selectedOrbRun {
                            controller.applyPendingLesson(on: run)
                        }
                    },
                    onDiscardLesson: {
                        if let run = controller.selectedOrbRun {
                            controller.discardPendingLesson(on: run)
                        }
                    },
                    onHoldToggle: { controller.toggleHoldOnSelectedRun() },
                    onStop: { controller.stopSelectedRun() },
                    isWindowExpanded: $expanded
                )
            }
        }
        .padding(10)
        .frame(width: expanded ? 360 : 96,
               height: expanded ? 640 : 96)
        .background(Color.black.opacity(0.001))
        .onChange(of: expanded) { _, newValue in
            onExpansionChange(newValue)
        }
        // Follow `selectedOrbRun`, which extends "active run" to
        // also include a recently-finished run that's still showing
        // a skill-proposed chip or just-saved toast. The earlier
        // codex P1 (d684db8) made us include .held alongside
        // .running so the Resume button didn't vanish during hand-
        // off; the present fix extends the same idea to the post-
        // run reflection events (skill_proposed lands AFTER
        // task_finished, when the run's status is already .done).
        .onChange(of: controller.selectedOrbRun?.id) { _, _ in
            liveObserver.bind(to: controller.selectedOrbRun)
        }
        .task {
            liveObserver.bind(to: controller.selectedOrbRun)
        }
    }
}

/// Watches the currently-running RunSession's `turns.last` and republishes
/// it as a flat `LiveStepInfo` value. Without this seam SwiftUI wouldn't
/// see updates inside the nested ObservableObject — `controller.selected
/// RunningRun?.turns` is two ObservableObjects deep, and only the outer
/// one is bound. We rebind on every `selectedRunningRun` change so the
/// orb tracks across thread switches.
@MainActor
final class LiveTurnObserver: ObservableObject {
    @Published private(set) var current: LiveStepInfo? = nil
    /// Mirror of run.status. Republished on the outer ObservableObject
    /// so a SwiftUI view binding to LiveTurnObserver re-renders when
    /// the agent enters/leaves the .held state.
    @Published private(set) var runStatus: RunSession.Status? = nil
    @Published private(set) var pendingLesson: RunSession.ProposedLesson? = nil
    @Published private(set) var lastAppliedSkillName: String? = nil
    private weak var run: RunSession?
    private var cancellable: AnyCancellable?

    func bind(to run: RunSession?) {
        guard run !== self.run else { return }
        self.run = run
        cancellable?.cancel()
        guard let run else {
            current = nil
            runStatus = nil
            pendingLesson = nil
            lastAppliedSkillName = nil
            return
        }
        cancellable = run.objectWillChange.sink { [weak self, weak run] _ in
            DispatchQueue.main.async {
                self?.current = Self.snapshot(from: run)
                self?.runStatus = run?.status
                self?.pendingLesson = run?.pendingLesson
                self?.lastAppliedSkillName = run?.lastAppliedSkillName
            }
        }
        current = Self.snapshot(from: run)
        runStatus = run.status
        pendingLesson = run.pendingLesson
        lastAppliedSkillName = run.lastAppliedSkillName
    }

    private static func snapshot(from run: RunSession?) -> LiveStepInfo? {
        guard let run else { return nil }
        guard let last = run.turns.last(where: { !$0.isUserPrompt })
        else { return nil }
        let lastAction = last.actions.last
        return LiveStepInfo(
            step: last.step,
            reflection: last.reflection,
            thought: last.thought,
            action: lastAction.map { "\($0.name) \($0.summary)" },
            isFailed: run.status == .fail
        )
    }
}
