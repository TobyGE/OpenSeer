// swift-tools-version: 5.9
//
// Swift Package Manager target for the OpenSeer macOS app.
//
// Why SPM and not an Xcode project: this repo is otherwise pure Python and
// pure-text (no .xcodeproj to merge), and SPM lets a developer run the app
// with just `swift run OpenSeerGUI` -- no Xcode required for development.
// A proper notarized .app bundle for end-user distribution later will need
// either an Xcode project + signing or a tool like swift-bundler; that's a
// separate (Phase 5+) concern and explicitly out of MVP scope.
//
// The app is intentionally a thin Swift shell on top of the existing Python
// `openseer` CLI: launches subprocesses for setup / auth / task / daemon and
// renders streamed events. We do NOT reimplement the agent loop in Swift.

import PackageDescription

let package = Package(
    name: "OpenSeerGUI",
    platforms: [
        // SwiftUI + Combine + AsyncSequence we use require Sonoma+. Most
        // OpenSeer users are already on Sequoia/Sonoma for the Quartz-AX
        // dump path the daemon uses, so this is not a new constraint.
        .macOS(.v14),
    ],
    products: [
        .executable(name: "OpenSeerGUI", targets: ["OpenSeerGUI"]),
    ],
    targets: [
        .executableTarget(
            name: "OpenSeerGUI",
            path: "Sources/OpenSeerGUI"
        ),
    ]
)
