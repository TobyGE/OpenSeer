import Foundation

/// Read/write helpers for ~/.openseer/config.json.
///
/// We touch only the keys the GUI explicitly manages (provider,
/// telegram block) and preserve everything else verbatim — the
/// daemon and the Python CLI may add their own keys we don't know
/// about, and we don't want a save in the GUI to drop them.
enum ConfigStore {
    static let path: URL = {
        URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent(".openseer/config.json")
    }()

    enum Error: Swift.Error, LocalizedError {
        case readFailed(String)
        case writeFailed(String)
        case invalidJSON

        var errorDescription: String? {
            switch self {
            case .readFailed(let m): return "config read failed: \(m)"
            case .writeFailed(let m): return "config write failed: \(m)"
            case .invalidJSON: return "config.json is malformed"
            }
        }
    }

    /// Load, mutate, atomically rewrite. Creates the parent dir if
    /// missing. Returns the merged dict so callers can chain reads.
    static func load() throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: path.path) else {
            return [:]
        }
        let data: Data
        do { data = try Data(contentsOf: path) }
        catch { throw Error.readFailed(error.localizedDescription) }
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else {
            throw Error.invalidJSON
        }
        return dict
    }

    static func save(_ dict: [String: Any]) throws {
        let parent = path.deletingLastPathComponent()
        try? FileManager.default.createDirectory(
            at: parent, withIntermediateDirectories: true)
        let data: Data
        do {
            data = try JSONSerialization.data(
                withJSONObject: dict,
                options: [.prettyPrinted, .sortedKeys])
        } catch {
            throw Error.writeFailed(error.localizedDescription)
        }
        do {
            try data.write(to: path, options: [.atomic])
        } catch {
            throw Error.writeFailed(error.localizedDescription)
        }
        // 0600 — the file may contain a bot token + chat ids; keep it
        // off other users' read access on shared boxes.
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: path.path)
    }

    @MainActor
    static func setProvider(_ provider: String) async {
        do {
            var dict = try load()
            dict["provider"] = provider
            try save(dict)
        } catch {
            // Provider write is non-critical for wizard flow; the
            // user can re-pick later. Log to console for debugging.
            NSLog("setProvider failed: %@", error.localizedDescription)
        }
    }

    /// Update the `telegram` block. ``token`` is preserved when nil
    /// (don't clobber a saved token because the user didn't re-type
    /// it). Other keys are written as given.
    static func updateTelegram(
        enabled: Bool,
        token: String?,
        allowedChatIds: [Int],
        triggerPrefix: String,
        maxSteps: Int?,
        stepCheckInterval: Int?,
    ) throws {
        var dict = try load()
        var tg: [String: Any] = (dict["telegram"] as? [String: Any]) ?? [:]
        tg["enabled"] = enabled
        if let t = token, !t.isEmpty { tg["token"] = t }
        tg["allowed_chat_ids"] = allowedChatIds
        tg["trigger_prefix"] = triggerPrefix
        if let m = maxSteps { tg["max_steps"] = m }
        if let v = stepCheckInterval { tg["step_check_interval"] = v }
        dict["telegram"] = tg
        try save(dict)
    }
}
