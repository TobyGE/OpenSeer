import Foundation

/// Thin async wrapper around `Process` for running the `openseer`
/// CLI. Two forms:
///
///   - `run(path:args:)` — block-and-wait, returns full stdout/stderr.
///     Use for short commands (auth status, version).
///   - `stream(path:args:onLine:)` — line-buffered tail of stdout.
///     Use for long-running commands (task / daemon) where the UI needs
///     incremental events.
///
/// Errors are returned as part of the result rather than thrown so call
/// sites stay flat.
enum CLI {
    struct Result {
        let exitCode: Int32
        let stdout: String
        let stderr: String
    }

    static func run(path: String, args: [String]) async -> Result {
        await withCheckedContinuation { (cont: CheckedContinuation<Result, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                let task = Process()
                task.executableURL = URL(fileURLWithPath: path)
                task.arguments = args
                task.environment = childEnvironment()
                let outPipe = Pipe()
                let errPipe = Pipe()
                task.standardOutput = outPipe
                task.standardError = errPipe
                do {
                    try task.run()
                } catch {
                    cont.resume(returning: Result(
                        exitCode: -1, stdout: "",
                        stderr: "spawn failed: \(error)"
                    ))
                    return
                }
                task.waitUntilExit()
                let out = String(
                    data: outPipe.fileHandleForReading.readDataToEndOfFile(),
                    encoding: .utf8) ?? ""
                let err = String(
                    data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                    encoding: .utf8) ?? ""
                cont.resume(returning: Result(
                    exitCode: task.terminationStatus,
                    stdout: out, stderr: err
                ))
            }
        }
    }

    /// Stream stdout line-by-line via `onLine`. Returns a `StreamHandle`
    /// the caller can use to terminate the process early. The
    /// continuation in `wait()` resolves when the process exits.
    static func stream(
        path: String, args: [String],
        onLine: @escaping @Sendable (String) -> Void
    ) -> StreamHandle {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = args
        task.environment = childEnvironment()
        let outPipe = Pipe()
        let errPipe = Pipe()
        task.standardOutput = outPipe
        task.standardError = errPipe

        // Line-buffer the read handle. readabilityHandler fires whenever
        // bytes are available; we accumulate into a buffer and flush
        // complete lines through onLine, leaving any partial line for
        // the next callback.
        let buf = LineBuffer(onLine: onLine)
        outPipe.fileHandleForReading.readabilityHandler = { handle in
            buf.feed(handle.availableData)
        }
        // Stderr is just merged into the same line stream with a tag so
        // crash messages don't disappear.
        errPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return }
            let s = String(data: data, encoding: .utf8) ?? ""
            for raw in s.split(separator: "\n") {
                onLine("[stderr] " + String(raw))
            }
        }
        return StreamHandle(task: task, outPipe: outPipe, errPipe: errPipe)
    }

    final class StreamHandle: @unchecked Sendable {
        private let task: Process
        private let outPipe: Pipe
        private let errPipe: Pipe
        init(task: Process, outPipe: Pipe, errPipe: Pipe) {
            self.task = task
            self.outPipe = outPipe
            self.errPipe = errPipe
        }
        func start() throws { try task.run() }
        func terminate() { task.terminate() }
        var isRunning: Bool { task.isRunning }
        /// Block until the process exits, returning the exit code.
        func wait() async -> Int32 {
            await withCheckedContinuation { (cont: CheckedContinuation<Int32, Never>) in
                DispatchQueue.global(qos: .background).async { [task, outPipe, errPipe] in
                    task.waitUntilExit()
                    // Flush any remaining buffered output by detaching
                    // the readability handlers; the buffer's deinit will
                    // emit the trailing partial line if any.
                    outPipe.fileHandleForReading.readabilityHandler = nil
                    errPipe.fileHandleForReading.readabilityHandler = nil
                    cont.resume(returning: task.terminationStatus)
                }
            }
        }
    }
}

/// Build the env passed to every `openseer …` child. Forces
/// unbuffered Python I/O so stdout lines (the agent's `out_dir=…`
/// header in particular) reach our LineBuffer immediately instead
/// of getting block-buffered behind a pipe — codex P1: without
/// this the GUI's 5-second deadline to discover a local task's
/// trace id often expires while the agent is still in capture/
/// model work, marking the session failed even though it's running
/// fine.
private func childEnvironment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    env["PYTHONUNBUFFERED"] = "1"
    return env
}

/// Accumulates bytes and flushes complete (newline-terminated) UTF-8
/// lines through an `onLine` callback. Thread-safe via a serial queue
/// because Pipe.readabilityHandler can fire on arbitrary threads.
private final class LineBuffer: @unchecked Sendable {
    private let onLine: @Sendable (String) -> Void
    private var carry = Data()
    private let q = DispatchQueue(label: "openseer.linebuf")

    init(onLine: @escaping @Sendable (String) -> Void) {
        self.onLine = onLine
    }

    func feed(_ data: Data) {
        if data.isEmpty { return }
        q.async { [self] in
            carry.append(data)
            while let nl = carry.firstIndex(of: 0x0A) {
                let line = carry.subdata(in: 0..<nl)
                carry.removeSubrange(0...nl)
                if let s = String(data: line, encoding: .utf8) {
                    onLine(s)
                }
            }
        }
    }

    deinit {
        if !carry.isEmpty,
           let s = String(data: carry, encoding: .utf8) {
            onLine(s)
        }
    }
}
