// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "ICourseNotes",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "ICourseNotes",
            path: "Sources/ICourseNotes"
        )
    ]
)
