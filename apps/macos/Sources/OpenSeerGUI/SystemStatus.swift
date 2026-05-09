import Foundation

/// Decoded shape of `openseer check --json`. Lives in one place so
/// every view that needs system state can bind to the same model.
///
/// The JSON schema is owned by Python (openseer/check.py); keep the
/// fields here in lockstep with collect()'s output.
struct SystemStatus: Codable, Equatable {
    let version: Int
    let providers: Providers
    let selectedProvider: String?
    let permissions: Permissions
    let telegram: Telegram
    let binaryPaths: BinaryPaths

    struct Providers: Codable, Equatable {
        let openai: ProviderStatus
        let anthropic: ProviderStatus
    }

    struct ProviderStatus: Codable, Equatable {
        let loggedIn: Bool
        let expiresInS: Int
        let plan: String?
        let subscription: String?
        let error: String?
    }

    struct Permissions: Codable, Equatable {
        let accessibility: Bool
        let screenRecording: Bool
    }

    struct Telegram: Codable, Equatable {
        let configured: Bool
        let enabled: Bool
        let tokenPresent: Bool
        let allowedChatIds: [Int]
        let triggerPrefix: String
        let maxSteps: Int?
        let stepCheckInterval: Int?
    }

    struct BinaryPaths: Codable, Equatable {
        let codex: String?
        let claude: String?
    }

    enum CodingKeys: String, CodingKey {
        case version, providers, permissions, telegram
        case selectedProvider = "selected_provider"
        case binaryPaths = "binary_paths"
    }
}

extension SystemStatus.ProviderStatus {
    enum CodingKeys: String, CodingKey {
        case loggedIn = "logged_in"
        case expiresInS = "expires_in_s"
        case plan, subscription, error
    }
}

extension SystemStatus.Permissions {
    enum CodingKeys: String, CodingKey {
        case accessibility
        case screenRecording = "screen_recording"
    }
}

extension SystemStatus.Telegram {
    enum CodingKeys: String, CodingKey {
        case configured, enabled
        case tokenPresent = "token_present"
        case allowedChatIds = "allowed_chat_ids"
        case triggerPrefix = "trigger_prefix"
        case maxSteps = "max_steps"
        case stepCheckInterval = "step_check_interval"
    }
}

/// Loads SystemStatus by invoking `openseer check --json`. Returns nil
/// on parse failure so callers can show an error rather than crash.
@MainActor
final class StatusProbe {
    static func fetch(binary: String) async -> SystemStatus? {
        let r = await CLI.run(path: binary, args: ["check", "--json"])
        guard r.exitCode == 0 else { return nil }
        guard let data = r.stdout.data(using: .utf8) else { return nil }
        let decoder = JSONDecoder()
        return try? decoder.decode(SystemStatus.self, from: data)
    }
}
