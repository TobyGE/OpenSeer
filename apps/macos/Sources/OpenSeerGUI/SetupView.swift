import SwiftUI

/// Phase 1 (this commit): skeleton multi-step wizard. Each step is
/// just a placeholder card; subsequent commits fill them with real
/// CLI calls. The structural decision worth keeping: a single
/// `SetupStep` enum drives both the side-rail nav and the body, so
/// adding/removing steps is a one-line edit.
struct SetupView: View {
    @EnvironmentObject var env: OpenSeerEnv
    @State private var step: SetupStep = .provider

    var body: some View {
        HStack(spacing: 0) {
            // Step rail
            VStack(alignment: .leading, spacing: 8) {
                Text("Setup")
                    .font(.title3.bold())
                    .padding(.bottom, 8)
                ForEach(SetupStep.allCases, id: \.self) { s in
                    StepRailItem(
                        step: s,
                        isCurrent: s == step,
                        isDone: s.rawValue < step.rawValue
                    )
                    .contentShape(Rectangle())
                    .onTapGesture { step = s }
                }
                Spacer()
            }
            .padding()
            .frame(width: 220)
            .background(.background.secondary)

            Divider()

            // Step body
            VStack(alignment: .leading, spacing: 16) {
                stepBody
                Spacer()
                HStack {
                    Button("Back") { step = step.prev() ?? step }
                        .disabled(step == .provider)
                    Spacer()
                    if step == .done {
                        Button("Open OpenSeer") {
                            Task { await env.refresh() }
                        }
                        .keyboardShortcut(.defaultAction)
                    } else {
                        Button("Next") { step = step.next() ?? step }
                            .keyboardShortcut(.defaultAction)
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    @ViewBuilder
    private var stepBody: some View {
        switch step {
        case .provider:
            ProviderStepView()
        case .auth:
            AuthStepView()
        case .permissions:
            PermissionsStepView()
        case .telegram:
            TelegramStepView()
        case .done:
            DoneStepView()
        }
    }
}

enum SetupStep: Int, CaseIterable {
    case provider = 0
    case auth
    case permissions
    case telegram
    case done

    var title: String {
        switch self {
        case .provider:    return "Model provider"
        case .auth:        return "Sign in"
        case .permissions: return "macOS permissions"
        case .telegram:    return "Telegram (optional)"
        case .done:        return "Ready"
        }
    }

    func next() -> SetupStep? { SetupStep(rawValue: rawValue + 1) }
    func prev() -> SetupStep? { SetupStep(rawValue: rawValue - 1) }
}

private struct StepRailItem: View {
    let step: SetupStep
    let isCurrent: Bool
    let isDone: Bool
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: isDone
                ? "checkmark.circle.fill"
                : (isCurrent ? "circle.dotted" : "circle"))
                .foregroundStyle(isDone ? .green : (isCurrent ? .accentColor : .secondary))
            Text(step.title)
                .foregroundStyle(isCurrent ? .primary : .secondary)
                .fontWeight(isCurrent ? .semibold : .regular)
        }
        .padding(.vertical, 4)
    }
}

// ── step bodies (skeletons; real wiring lands in Phase 1.x commits) ──

private struct ProviderStepView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Model provider").font(.title2.bold())
            Text("OpenSeer can drive your Mac with either GPT-5.5 (via Codex CLI OAuth) or Claude Haiku 4.5 (via Claude Code OAuth). Pick whichever subscription you have.")
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                ProviderCard(name: "OpenAI · GPT-5.5", auth: "Codex CLI")
                ProviderCard(name: "Anthropic · Haiku 4.5", auth: "Claude Code")
            }
            Text("(Auto-detection of existing logins lands in the next commit.)")
                .font(.caption).foregroundStyle(.tertiary)
        }
    }
}

private struct ProviderCard: View {
    let name: String
    let auth: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(name).font(.headline)
            Text("Auth: \(auth)").font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.secondary)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

private struct AuthStepView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Sign in").font(.title2.bold())
            Text("This step opens the provider's OAuth flow in your browser. The Swift app does NOT capture credentials directly — it shells out to the same `openseer auth login` flow the CLI uses.")
                .foregroundStyle(.secondary)
        }
    }
}

private struct PermissionsStepView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("macOS permissions").font(.title2.bold())
            Text("OpenSeer needs Accessibility (to control mouse/keyboard) and Screen Recording (to take screenshots). The next commit polls TCC and surfaces a System Settings deep-link if either is missing.")
                .foregroundStyle(.secondary)
        }
    }
}

private struct TelegramStepView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Telegram bot (optional)").font(.title2.bold())
            Text("Lets you control this Mac from your phone. Paste a bot token from @BotFather and the daemon will save it under the `telegram` block in ~/.openseer/config.json.")
                .foregroundStyle(.secondary)
        }
    }
}

private struct DoneStepView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("All set.").font(.title2.bold())
            Text("Click below to switch into the chat window.")
                .foregroundStyle(.secondary)
        }
    }
}
