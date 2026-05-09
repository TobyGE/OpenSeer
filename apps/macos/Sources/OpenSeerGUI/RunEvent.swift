import Foundation

/// One row in `~/.openseer/runs/<id>/events.jsonl`. The Python
/// `TrajectoryCallback` writes every event the agent emits in this
/// shape:  `{"type": ..., "ts": float, "step": int|null, "data": {...}}`.
///
/// We don't need every field; this struct keeps the ones the GUI
/// renders. The full `data` blob stays as a generic dict so we can
/// pluck whatever a particular event type cares about (action name,
/// thought text, AX summary, …) without forcing every type into one
/// rigid shape.
struct RunEvent: Decodable, Identifiable {
    let type: String
    let ts: Double
    let step: Int?
    let data: [String: AnyCodable]

    var id: String { "\(ts)-\(type)-\(step ?? -1)" }

    enum CodingKeys: String, CodingKey { case type, ts, step, data }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        ts = try c.decode(Double.self, forKey: .ts)
        step = try c.decodeIfPresent(Int.self, forKey: .step)
        data = (try? c.decode([String: AnyCodable].self, forKey: .data)) ?? [:]
    }
}

/// Tiny dynamic JSON value. Enough to extract `String`, `Int`, `Bool`,
/// `Double`, `Array`, and nested dicts via subscript-y accessors.
struct AnyCodable: Decodable {
    let value: Any?
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { value = nil; return }
        if let b = try? c.decode(Bool.self) { value = b; return }
        if let i = try? c.decode(Int.self) { value = i; return }
        if let d = try? c.decode(Double.self) { value = d; return }
        if let s = try? c.decode(String.self) { value = s; return }
        if let arr = try? c.decode([AnyCodable].self) {
            value = arr.map { $0.value as Any? }
            return
        }
        if let dict = try? c.decode([String: AnyCodable].self) {
            value = dict.mapValues { $0.value as Any? }
            return
        }
        value = nil
    }
    var string: String? { value as? String }
    var int: Int? { value as? Int }
    var bool: Bool? { value as? Bool }
    var double: Double? { value as? Double }
    var array: [Any?]? { value as? [Any?] }
    var dict: [String: Any?]? { value as? [String: Any?] }
}
