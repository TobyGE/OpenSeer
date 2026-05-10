import AVFoundation
import Foundation
import Speech

@main
struct OpenSeerVoiceListen {
    static func main() async {
        let seconds = parseSeconds()
        let locale = parseLocale()
        let debug = CommandLine.arguments.contains("--debug")
        let allowServerRecognition = CommandLine.arguments.contains("--allow-server-recognition")
        let recognizer = SFSpeechRecognizer(locale: locale) ?? SFSpeechRecognizer()
        guard let recognizer, recognizer.isAvailable else {
            fail("speech recognition is unavailable")
        }
        guard allowServerRecognition || recognizer.supportsOnDeviceRecognition else {
            fail("on-device speech recognition is unavailable for \(locale.identifier)")
        }

        let speechStatus = await requestSpeechAuthorization()
        guard speechStatus == .authorized else {
            fail("speech recognition permission is \(speechStatus.rawValue)")
        }
        let micOK = await AVCaptureDevice.requestAccess(for: .audio)
        guard micOK else {
            fail("microphone permission denied")
        }

        let engine = AVAudioEngine()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = !allowServerRecognition

        var transcript = ""
        let transcriptLock = NSLock()
        let done = DispatchSemaphore(value: 0)
        let task = recognizer.recognitionTask(with: request) { result, error in
            if let result {
                let text = result.bestTranscription.formattedString
                transcriptLock.lock()
                transcript = text
                transcriptLock.unlock()
                if debug, !text.isEmpty {
                    eprint("[voice-helper] partial: \(text)")
                }
                if result.isFinal { done.signal() }
            }
            if let error {
                if debug { eprint("[voice-helper] error: \(error)") }
                done.signal()
            }
        }

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) {
            buffer, _ in request.append(buffer)
        }

        do {
            engine.prepare()
            try engine.start()
        } catch {
            fail("couldn't start microphone: \(error)")
        }

        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        engine.stop()
        input.removeTap(onBus: 0)
        request.endAudio()
        await Task.detached {
            _ = done.wait(timeout: .now() + 5.0)
        }.value
        task.cancel()

        transcriptLock.lock()
        let final = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        transcriptLock.unlock()
        print(final)
    }

    private static func parseSeconds() -> Double {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--seconds"),
              args.indices.contains(i + 1),
              let seconds = Double(args[i + 1]) else {
            return 8.0
        }
        return max(1.0, min(seconds, 60.0))
    }

    private static func parseLocale() -> Locale {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--locale"),
              args.indices.contains(i + 1) else {
            return Locale.current
        }
        return Locale(identifier: args[i + 1])
    }

    private static func requestSpeechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
    }

    private static func fail(_ message: String) -> Never {
        eprint(message)
        exit(2)
    }

    private static func eprint(_ message: String) {
        FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    }
}
