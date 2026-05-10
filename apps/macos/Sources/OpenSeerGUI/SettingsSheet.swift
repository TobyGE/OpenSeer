import SwiftUI

/// Settings is a tabbed sheet with four tabs:
///   - General: provider, dry-run / confirm-each / max-steps defaults
///   - Memory:  edit ~/.openseer/SOUL.md  and  MEMORY.md  inline
///   - Skills:  list installed skills + open SKILL.md in default editor
///   - Telegram: same form the wizard uses (for re-edits later)
///
/// All edits write through ConfigStore / file-write helpers; no
/// direct Python subprocess calls except `openseer check --json` to
/// re-probe state on save.
struct SettingsSheet: View {
    @EnvironmentObject var env: OpenSeerEnv
    @Binding var statusBlob: SystemStatus?
    var onClose: () -> Void
    @State private var tab: Tab = .memory

    enum Tab: String, CaseIterable, Identifiable {
        case general, memory, skills, telegram
        var id: String { rawValue }
        var label: String {
            switch self {
            case .general:  return "General"
            case .memory:   return "Memory"
            case .skills:   return "Skills"
            case .telegram: return "Telegram"
            }
        }
        var icon: String {
            switch self {
            case .general:  return "slider.horizontal.3"
            case .memory:   return "brain"
            case .skills:   return "books.vertical"
            case .telegram: return "paperplane"
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Settings").font(.title3.bold())
                Spacer()
                Button("Done", action: onClose)
                    .keyboardShortcut(.cancelAction)
            }
            .padding()
            Divider()
            HStack(spacing: 0) {
                // Sidebar tabs
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Tab.allCases) { t in
                        tabButton(t)
                    }
                    Spacer()
                }
                .padding(8)
                .frame(width: 160)
                .background(.background.secondary)
                Divider()
                content
            }
        }
        .frame(width: 760, height: 540)
    }

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .general:  GeneralPane()
        case .memory:   MemoryPane()
        case .skills:   SkillsPane()
        case .telegram: TelegramPane(statusBlob: $statusBlob,
                                     binary: env.binaryPath ?? "")
        }
    }

    private func tabButton(_ t: Tab) -> some View {
        Button {
            tab = t
        } label: {
            HStack(spacing: 8) {
                Image(systemName: t.icon).frame(width: 16)
                Text(t.label)
                Spacer()
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(t == tab ? Color.accentColor.opacity(0.18)
                        : Color.clear)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(t == tab ? .primary : .secondary)
    }
}

// ── General ────────────────────────────────────────────────────────

private struct GeneralPane: View {
    @EnvironmentObject var env: OpenSeerEnv
    @AppStorage("voiceLocale") private var voiceLocale = "zh-CN"
    @State private var provider: String = ""
    @State private var saved: Bool = false

    var body: some View {
        Form {
            Section("Model provider") {
                Picker("Provider", selection: $provider) {
                    Text("OpenAI · GPT-5.5").tag("openai")
                    Text("Anthropic · Haiku 4.5").tag("anthropic")
                }
                .pickerStyle(.segmented)
                Button(saved ? "Saved ✓" : "Save provider") {
                    Task {
                        await ConfigStore.setProvider(provider)
                        // Re-evaluate readiness: switching to a
                        // not-logged-in provider must bounce the
                        // user back to the wizard, otherwise the
                        // chat stays open and the next task fails
                        // in CLI preflight (codex P2).
                        await env.refresh()
                        saved = true
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        saved = false
                    }
                }
                .disabled(provider.isEmpty)
            }
            Section("Voice") {
                Picker("Recognition language", selection: $voiceLocale) {
                    Text("中文普通话").tag("zh-CN")
                    Text("English").tag("en-US")
                    Text("System default").tag("system")
                }
                .pickerStyle(.segmented)
                Text("Chinese recognition may use Apple's server-based Speech service if the on-device language pack is not installed.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("About") {
                Text("OpenSeer is a chat-first, memory-aware computer-use agent for macOS. The Swift app is a thin shell on top of the existing `openseer` Python CLI.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear {
            if let data = FileManager.default.contents(
                atPath: NSHomeDirectory() + "/.openseer/config.json"),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let p = obj["provider"] as? String {
                provider = p
            }
        }
    }
}

// ── Memory (SOUL.md + MEMORY.md) ───────────────────────────────────

private struct MemoryPane: View {
    @State private var soul: String = ""
    @State private var memory: String = ""
    @State private var dirty: Set<String> = []
    @State private var saved: Bool = false

    private let soulPath = NSHomeDirectory() + "/.openseer/SOUL.md"
    private let memPath  = NSHomeDirectory() + "/.openseer/MEMORY.md"

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("These files are full-injected into the system prompt every run. Edit anytime — saved files take effect on the next task.")
                .font(.caption).foregroundStyle(.secondary)
                .padding(.horizontal, 12).padding(.top, 12)
            HSplitView {
                editor(title: "SOUL.md (voice / tone)",
                       text: $soul,
                       key: "soul")
                editor(title: "MEMORY.md (durable user facts)",
                       text: $memory,
                       key: "memory")
            }
            HStack {
                Spacer()
                if saved {
                    Label("Saved", systemImage: "checkmark.seal.fill")
                        .foregroundStyle(.green)
                }
                Button("Save") {
                    if dirty.contains("soul") {
                        try? soul.write(toFile: soulPath,
                                        atomically: true, encoding: .utf8)
                    }
                    if dirty.contains("memory") {
                        try? memory.write(toFile: memPath,
                                          atomically: true, encoding: .utf8)
                    }
                    dirty.removeAll()
                    saved = true
                    Task {
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        saved = false
                    }
                }
                .disabled(dirty.isEmpty)
                .buttonStyle(.borderedProminent)
            }
            .padding(12)
        }
        .onAppear {
            soul = (try? String(contentsOfFile: soulPath, encoding: .utf8)) ?? ""
            memory = (try? String(contentsOfFile: memPath, encoding: .utf8)) ?? ""
        }
    }

    private func editor(title: String, text: Binding<String>, key: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption.bold())
            TextEditor(text: Binding(
                get: { text.wrappedValue },
                set: { v in
                    text.wrappedValue = v
                    dirty.insert(key)
                }))
                .font(.body.monospaced())
                .border(Color.secondary.opacity(0.3), width: 1)
        }
        .padding(8)
    }
}

// ── Skills list ────────────────────────────────────────────────────

private struct SkillsPane: View {
    @State private var skills: [SkillEntry] = []

    struct SkillEntry: Identifiable {
        let id = UUID()
        var name: String
        var family: String
        var description: String
        var path: String
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Installed skills are loaded into the prompt index every turn. Click \"Open\" to edit a skill's SKILL.md in your default editor.")
                .font(.caption).foregroundStyle(.secondary)
                .padding(12)
            List(skills) { s in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(s.name).font(.headline)
                        Text(s.description)
                            .font(.caption).foregroundStyle(.secondary)
                            .lineLimit(2)
                        Text("\(s.family)  ·  \(s.path)")
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Button("Open") {
                        NSWorkspace.shared.open(URL(fileURLWithPath: s.path))
                    }
                }
                .padding(.vertical, 4)
            }
            .listStyle(.inset)
        }
        .onAppear { skills = SkillsPane.discover() }
    }

    /// Walk both bundled (./openseer/skills) and user
    /// (~/.openseer/skills) trees, parse the front-matter, return
    /// SkillEntry rows.
    static func discover() -> [SkillEntry] {
        let roots = [
            // The repo's own skills (when the dev runs the GUI from
            // a working tree).
            FileManager.default.currentDirectoryPath + "/openseer/skills",
            // User-installed skills (`/learn` writes here).
            NSHomeDirectory() + "/.openseer/skills",
        ]
        var out: [SkillEntry] = []
        for root in roots {
            let url = URL(fileURLWithPath: root)
            guard let it = FileManager.default.enumerator(
                at: url, includingPropertiesForKeys: nil) else { continue }
            for case let f as URL in it where f.lastPathComponent == "SKILL.md" {
                if let s = parse(f) { out.append(s) }
            }
        }
        return out.sorted { $0.name < $1.name }
    }

    static func parse(_ url: URL) -> SkillEntry? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            return nil
        }
        var name = url.deletingLastPathComponent().lastPathComponent
        var family = "?"
        var description = ""
        // Tiny YAML pluck.
        if text.hasPrefix("---") {
            let after = text.dropFirst(3)
            if let end = after.range(of: "\n---") {
                let block = after[after.startIndex..<end.lowerBound]
                for line in block.split(separator: "\n") {
                    let pair = line.split(separator: ":", maxSplits: 1)
                    guard pair.count == 2 else { continue }
                    let k = pair[0].trimmingCharacters(in: .whitespaces)
                    let v = pair[1].trimmingCharacters(in: .whitespaces)
                    switch k {
                    case "name": name = v
                    case "family": family = v
                    case "description": description = v
                    default: break
                    }
                }
            }
        }
        return SkillEntry(name: name, family: family,
                          description: description, path: url.path)
    }
}

// ── Telegram pane (mirrors wizard, for re-edits) ──────────────────

private struct TelegramPane: View {
    @Binding var statusBlob: SystemStatus?
    let binary: String
    @State private var enabled: Bool = false
    @State private var token: String = ""
    @State private var ids: String = ""
    @State private var prefix: String = ""
    @State private var maxSteps: String = "200"
    @State private var interval: String = "30"
    @State private var dirty: Bool = false
    @State private var msg: String = ""

    var body: some View {
        Form {
            Section("Bot") {
                Toggle("Enable Telegram daemon", isOn: $enabled)
                    .onChange(of: enabled) { _, _ in dirty = true }
                if statusBlob?.telegram.tokenPresent == true {
                    Text("Existing token preserved unless you type a new one.")
                        .font(.caption).foregroundStyle(.tertiary)
                }
                SecureField("Bot token (paste from @BotFather)",
                            text: $token)
                    .onChange(of: token) { _, _ in dirty = true }
                TextField("Allowed chat IDs (comma-separated)", text: $ids)
                    .onChange(of: ids) { _, _ in dirty = true }
                TextField("Trigger prefix", text: $prefix)
                    .onChange(of: prefix) { _, _ in dirty = true }
            }
            Section("Run controls") {
                TextField("Max steps", text: $maxSteps)
                    .onChange(of: maxSteps) { _, _ in dirty = true }
                TextField("Step check interval", text: $interval)
                    .onChange(of: interval) { _, _ in dirty = true }
            }
            HStack {
                Spacer()
                if !msg.isEmpty {
                    Text(msg).font(.caption).foregroundStyle(.secondary)
                }
                Button("Save") { save() }
                    .disabled(!dirty)
                    .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
        .onAppear { hydrate() }
    }

    private func hydrate() {
        guard let tg = statusBlob?.telegram else { return }
        enabled = tg.enabled
        ids = tg.allowedChatIds.map(String.init).joined(separator: ", ")
        prefix = tg.triggerPrefix
        if let m = tg.maxSteps { maxSteps = String(m) }
        if let v = tg.stepCheckInterval { interval = String(v) }
        token = ""
        dirty = false
    }

    private func save() {
        let parsed = ids
            .split(whereSeparator: { c in c == "," || c == " " || c == "\n" })
            .compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
        do {
            try ConfigStore.updateTelegram(
                enabled: enabled,
                token: token.isEmpty ? nil : token,
                allowedChatIds: parsed,
                triggerPrefix: prefix,
                maxSteps: Int(maxSteps),
                stepCheckInterval: Int(interval),
            )
            token = ""
            dirty = false
            msg = "Saved."
            Task {
                if let s = await StatusProbe.fetch(binary: binary) {
                    statusBlob = s
                }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                msg = ""
            }
        } catch {
            msg = "Save failed: \(error.localizedDescription)"
        }
    }
}
