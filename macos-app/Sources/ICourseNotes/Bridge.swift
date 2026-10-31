import Foundation

/// Runs `bridge/app_bridge.py` with the shared Python environment. Each call is
/// one short-lived process that prints a single JSON object.
struct Bridge: Sendable {
    let projectRoot: URL

    var python: URL { projectRoot.appending(path: "work/qwen-local-asr-test-env/bin/python") }
    /// The bridge ships inside the app bundle; a development build falls back to the source tree.
    var script: URL {
        if let bundled = Bundle.main.resourceURL?.appending(path: "bridge/app_bridge.py"),
           FileManager.default.fileExists(atPath: bundled.path) { return bundled }
        return URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appending(path: "bridge/app_bridge.py")
    }

    static func isProject(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.appending(path: "work/local-runtime/active-source.json").path)
    }

    /// Saved choice first, then the folder that contains the app (and its parents).
    static func locateProject() -> URL? {
        if let saved = UserDefaults.standard.string(forKey: "projectRoot") {
            let url = URL(fileURLWithPath: saved)
            if isProject(url) { return url }
        }
        var url = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<4 {
            if isProject(url) { return url }
            url = url.deletingLastPathComponent()
        }
        return nil
    }

    func run<T: Decodable>(_ args: [String], as type: T.Type, timeout: TimeInterval = 120) async throws -> T {
        let data = try await raw(args, timeout: timeout)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            if let generic = try? decoder.decode(ActionResponse.self, from: data), !generic.ok {
                throw BridgeError(message: generic.message ?? "后端错误：\(generic.errorType ?? "未知")")
            }
            throw BridgeError(message: "无法解析后端返回（\(type)）")
        }
    }

    func raw(_ args: [String], timeout: TimeInterval) async throws -> Data {
        let python = python, script = script, root = projectRoot
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = python
                p.arguments = [script.path] + args
                p.currentDirectoryURL = root
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                env["DYLD_FALLBACK_LIBRARY_PATH"] = "/opt/homebrew/lib"
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["ICOURSE_PROJECT_ROOT"] = root.path
                p.environment = env
                let out = Pipe(), err = Pipe()
                p.standardOutput = out
                p.standardError = err
                var collected = Data()
                let lock = NSLock()
                out.fileHandleForReading.readabilityHandler = { h in
                    let d = h.availableData
                    lock.lock(); collected.append(d); lock.unlock()
                }
                err.fileHandleForReading.readabilityHandler = { h in _ = h.availableData }
                do { try p.run() } catch {
                    cont.resume(throwing: BridgeError(message: "无法启动 Python 环境：\(python.path)"))
                    return
                }
                let timer = DispatchWorkItem { if p.isRunning { p.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: timer)
                p.waitUntilExit()
                timer.cancel()
                out.fileHandleForReading.readabilityHandler = nil
                err.fileHandleForReading.readabilityHandler = nil
                let rest = out.fileHandleForReading.readDataToEndOfFile()
                lock.lock(); collected.append(rest); let result = collected; lock.unlock()
                if result.isEmpty {
                    cont.resume(throwing: BridgeError(message: p.terminationReason == .uncaughtSignal ? "后端超时" : "后端没有返回结果"))
                } else {
                    cont.resume(returning: result)
                }
            }
        }
    }
}
