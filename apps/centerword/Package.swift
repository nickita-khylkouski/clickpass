// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CenterWord",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "CenterWord", targets: ["CenterWord"]),
    ],
    targets: [
        .executableTarget(
            name: "CenterWord",
            path: "Sources/CenterWordApp",
            linkerSettings: [
                .linkedFramework("Carbon"),
            ]
        ),
        .testTarget(
            name: "CenterWordTests",
            dependencies: ["CenterWord"],
            path: "Tests/CenterWordTests"
        ),
    ]
)
