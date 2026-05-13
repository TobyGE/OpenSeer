import Foundation

/// Aggregated view of one model "turn" — i.e. one model_started …
/// model_finished window plus the steps that landed within it. The
/// chat view collapses each turn into a single bubble; users expand
/// to see the per-step thought + action chain.
struct Turn: Identifiable {
    let id = UUID()
    /// 1-indexed agent step when the model_started event fired.
    var step: Int
    /// First step's thought (the "headline"). We pluck this from the
    /// FIRST step_recorded event in the turn since chained steps
    /// usually share or refine the same plan.
    var thought: String = ""
    /// Status reflection token at the start of `thought`
    /// (`[SUCCESS]`, `[INEFFECTIVE]`, `[REGRESSED]`, `[THINKING]`).
    var reflection: String = ""
    /// Brief one-liner per executed action in this turn.
    var actions: [Action] = []
    /// True once we see model_finished or task_finished/failed.
    var finished: Bool = false
    /// Token usage for the turn (if reported via model_finished).
    var inputTokens: Int = 0
    var outputTokens: Int = 0
    /// Wall-clock seconds the model spent on this turn.
    var elapsedMs: Int = 0
    /// True if this is the synthesized "user input" pseudo-turn
    /// shown above the first model bubble.
    var isUserPrompt: Bool = false
    var promptText: String = ""

    struct Action: Identifiable {
        let id = UUID()
        var name: String
        var summary: String
        var result: String
        /// `action.reason` from the agent. For `terminate` this is
        /// the model's final user-facing reply; we surface it as a
        /// distinct "final output" block in the bubble.
        var reason: String = ""
    }

    /// Terminate's reason text, if this turn ended with a terminate
    /// action. Used by the bubble to render a final-output block.
    var finalOutput: String? {
        guard let last = actions.last,
              last.name == "terminate",
              !last.reason.isEmpty else { return nil }
        return last.reason
    }
}

/// Convenience: turn one RunEvent stream into a list of Turn structs.
/// Pure / stateless; called by RunSession on each new event batch.
enum TurnFolder {
    /// Mutates `turns` in place with the new event. Returns true if a
    /// new turn was started or the last turn was meaningfully updated.
    @discardableResult
    static func apply(_ event: RunEvent, to turns: inout [Turn]) -> Bool {
        switch event.type {
        case "task_started":
            // Surface the user's prompt as the first bubble.
            let task = event.data["task"]?.string ?? ""
            var t = Turn(step: 0)
            t.isUserPrompt = true
            t.promptText = task
            t.finished = true
            turns.append(t)
            return true

        case "model_started":
            let n = (event.step ?? 0)
            turns.append(Turn(step: n))
            return true

        case "model_delta":
            // Streaming JSON — extract the streaming thought so the
            // user sees something show up immediately. We pluck the
            // first "thought":"...." from the buffer.
            if var last = turns.last, !last.isUserPrompt, !last.finished {
                let buf = event.data["text"]?.string ?? ""
                if let t = extractThought(from: buf) {
                    last.thought = t
                    last.reflection = extractReflection(from: t) ?? ""
                    turns[turns.count - 1] = last
                    return true
                }
            }
            return false

        case "model_finished":
            if var last = turns.last, !last.finished {
                last.finished = true
                if let usage = event.data["usage"]?.dict {
                    last.inputTokens = usage["input_tokens"] as? Int ?? 0
                    last.outputTokens = usage["output_tokens"] as? Int ?? 0
                }
                last.elapsedMs = event.data["elapsed_ms"]?.int ?? 0
                turns[turns.count - 1] = last
                return true
            }
            return false

        case "step_recorded":
            // `data.action` is the action name; `data.result` short result.
            if var last = turns.last, !last.isUserPrompt {
                let name = event.data["action"]?.string ?? "?"
                let result = event.data["result"]?.string ?? ""
                let reason = event.data["reason"]?.string ?? ""
                let summary = makeActionSummary(event.data)
                last.actions.append(.init(
                    name: name, summary: summary,
                    result: result, reason: reason
                ))
                turns[turns.count - 1] = last
                return true
            }
            return false

        case "task_finished", "task_failed":
            // Synthesize a final pseudo-turn carrying the closing
            // status if the last real turn already finished.
            return false

        default:
            return false
        }
    }

    private static func extractThought(from buf: String) -> String? {
        // Crude but effective: find the FIRST `thought` or `thinking`
        // key and read until the next unescaped quote. Anthropic
        // emits `thinking`, OpenAI emits `thought`; the Python
        // parser already accepts both, so we match here too.
        // Streaming partials are tolerated.
        let needles = ["\"thought\":\"", "\"thinking\":\""]
        var startIdx: String.Index? = nil
        for n in needles {
            if let r = buf.range(of: n) {
                if startIdx == nil || r.upperBound < (startIdx ?? r.upperBound) {
                    startIdx = r.upperBound
                }
            }
        }
        guard let start = startIdx else { return nil }
        var s = ""
        var i = start
        var esc = false
        while i < buf.endIndex {
            let c = buf[i]
            if esc { s.append(c); esc = false }
            else if c == "\\" { esc = true }
            else if c == "\"" { break }
            else { s.append(c) }
            i = buf.index(after: i)
        }
        return s.isEmpty ? nil : s
    }

    private static func extractReflection(from thought: String) -> String? {
        // Reflection tokens are bracketed at the start: "[SUCCESS]: …"
        let prefix = thought.prefix { $0 != ":" }
        let s = String(prefix)
        if s.hasPrefix("[") && s.hasSuffix("]") { return s }
        return nil
    }

    private static func makeActionSummary(_ data: [String: AnyCodable]) -> String {
        let name = data["action"]?.string ?? "?"
        switch name {
        case "click":
            if let i = data["index"]?.int { return "click idx=\(i)" }
            if let x = data["x"]?.int, let y = data["y"]?.int {
                return "click (\(x),\(y))"
            }
            return "click"
        case "type":
            let t = data["text"]?.string ?? ""
            let preview = t.prefix(40)
            return "type \"\(preview)\(t.count > 40 ? "…" : "")\""
        case "key":
            return "key \(data["key"]?.string ?? "?")"
        case "scroll":
            if let a = data["amount"]?.int { return "scroll \(a)" }
            return "scroll"
        case "open_app":
            return "open \(data["app"]?.string ?? "?")"
        case "bash":
            let c = data["cmd"]?.string ?? ""
            return "bash: \(c.prefix(60))\(c.count > 60 ? "…" : "")"
        case "web_search":
            return "search \"\(data["query"]?.string ?? "")\""
        case "web_fetch":
            return "fetch \(data["url"]?.string ?? "")"
        case "read_page":
            return "read_page" + (data["url"]?.string.map { " \($0)" } ?? "")
        case "ask_user":
            return "ask_user(\(data["kind"]?.string ?? "?"))"
        case "write_memory":
            return "write_memory"
        case "write_skill":
            return "write_skill \(data["skill_name"]?.string ?? "?")"
        case "read_skill":
            return "read_skill \(data["skill_name"]?.string ?? "?")"
        case "terminate":
            let st = data["status"]?.string ?? "done"
            return "terminate \(st)"
        default:
            return name
        }
    }
}
