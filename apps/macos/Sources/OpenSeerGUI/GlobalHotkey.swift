import AppKit
import Carbon.HIToolbox

/// System-wide control+S hotkey that toggles the voice orb from any
/// app. Uses Carbon's RegisterEventHotKey because it's the only path
/// that actually consumes the keystroke (NSEvent global monitor can
/// observe but not block, so the focused app would also receive it).
@MainActor
final class GlobalHotkey {
    static let shared = GlobalHotkey()

    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private var onTrigger: () -> Void = {}

    private init() {}

    /// Install the handler + register control+S. Returns true on
    /// success. Safe to call once at app launch; no-op on subsequent
    /// calls (we only register a single binding for now).
    @discardableResult
    func install(onTrigger: @escaping () -> Void) -> Bool {
        guard hotKeyRef == nil else {
            // Already installed; just refresh the handler.
            self.onTrigger = onTrigger
            return true
        }
        self.onTrigger = onTrigger

        var spec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed))
        let opaque = Unmanaged.passUnretained(self).toOpaque()
        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { (_, _, userData) -> OSStatus in
                // Carbon callback isn't declared @MainActor-safe;
                // hop through a main-actor task instead of assuming
                // isolation from a C callback.
                guard let userData else { return noErr }
                let hk = Unmanaged<GlobalHotkey>.fromOpaque(userData)
                    .takeUnretainedValue()
                Task { @MainActor in
                    hk.onTrigger()
                }
                return noErr
            },
            1, &spec, opaque, &eventHandler)
        guard installStatus == noErr else {
            NSLog("[hotkey] InstallEventHandler failed: %d",
                  Int(installStatus))
            return false
        }

        // Control+S. "S for Seer / Summon". Plain control+S is
        // largely unused by macOS apps (Cocoa text fields treat it
        // as "transpose chars" in classic Emacs bindings, but that
        // virtually no one relies on), and it's one fewer modifier
        // for the user's pinky than the older cmd+option+S.
        let modifiers: UInt32 = UInt32(controlKey)
        let keyCode: UInt32 = UInt32(kVK_ANSI_S)
        // Signature is an OSType (FourCharCode) — any unique 32-bit
        // value will do. We use 'OSRH' = OpenSeer Hotkey so
        // collision with another app is essentially impossible.
        let hkID = EventHotKeyID(
            signature: OSType(bitPattern: 0x4F_53_52_48), id: 1)
        var ref: EventHotKeyRef?
        let regStatus = RegisterEventHotKey(
            keyCode, modifiers, hkID,
            GetApplicationEventTarget(), 0, &ref)
        guard regStatus == noErr, let ref else {
            NSLog("[hotkey] RegisterEventHotKey failed: %d",
                  Int(regStatus))
            return false
        }
        hotKeyRef = ref
        NSLog("[hotkey] registered control+S")
        return true
    }
}

/// Snapshot of the frontmost app at the moment the user pressed
/// the hotkey. We capture BEFORE bringing OpenSeer to front so the
/// app name doesn't immediately become "OpenSeer" itself.
struct FrontmostContext {
    let appName: String
    let bundleId: String?
    let pid: pid_t
}

@MainActor
enum FrontmostCapture {
    static func capture() -> FrontmostContext? {
        guard let app = NSWorkspace.shared.frontmostApplication else {
            return nil
        }
        return FrontmostContext(
            appName: app.localizedName ?? "Unknown",
            bundleId: app.bundleIdentifier,
            pid: app.processIdentifier
        )
    }
}

extension Notification.Name {
    /// Posted when the global hotkey fires (or any other path that
    /// wants the orb panel open + listening). Voice orb listens
    /// via .onReceive and opens itself.
    static let openVoiceOrb = Notification.Name(
        "openseer.openVoiceOrb")
    /// Posted when the hotkey is pressed while the orb is already
    /// onscreen — tells VoiceOrbView to stop the listen loop and
    /// collapse its open-state BEFORE the panel is orderedOut, so
    /// SFSpeech/VoiceInput doesn't keep recording invisibly and
    /// auto-submit a stray utterance after dismissal (codex P1).
    static let dismissVoiceOrb = Notification.Name(
        "openseer.dismissVoiceOrb")
}
