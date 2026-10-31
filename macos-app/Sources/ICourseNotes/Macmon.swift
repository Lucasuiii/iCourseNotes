import Foundation
import Observation

struct HardwareSample: Identifiable, Hashable {
    var id: Date { time }
    let time: Date
    let gpuPercent: Double
    let cpuPercent: Double
    let systemWatts: Double
    let gpuWatts: Double
    let cpuWatts: Double
    let aneWatts: Double
    let ramGib: Double
    let ramTotalGib: Double
    let swapGib: Double
    let gpuTemp: Double?
    let cpuTemp: Double?
    let gpuMHz: Double?
}

/// Streams `macmon pipe` (one JSON object per line) and keeps one hour of history.
@MainActor @Observable
final class Macmon {
    private(set) var samples: [HardwareSample] = []
    private(set) var latest: HardwareSample?
    private(set) var chip: String = ""
    private(set) var error: String?
    private var process: Process?
    private var buffer = Data()

    static let path = "/opt/homebrew/bin/macmon"

    func start() {
        guard process == nil else { return }
        if chip.isEmpty {
            var size = 0
            sysctlbyname("machdep.cpu.brand_string", nil, &size, nil, 0)
            var buf = [CChar](repeating: 0, count: max(size, 1))
            sysctlbyname("machdep.cpu.brand_string", &buf, &size, nil, 0)
            let gb = Int(ProcessInfo.processInfo.physicalMemory / 1_073_741_824)
            chip = "\(String(cString: buf)) · \(gb) GB"
        }
        guard FileManager.default.isExecutableFile(atPath: Self.path) else {
            error = "未找到 macmon（/opt/homebrew/bin/macmon）"
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Self.path)
        p.arguments = ["pipe", "-i", "1000"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor in self?.consume(data) }
        }
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.process = nil
                self?.error = "macmon 已退出"
            }
        }
        do {
            try p.run()
            process = p
            error = nil
        } catch {
            self.error = "macmon 启动失败"
        }
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            if let s = parse(Data(line)) {
                latest = s
                samples.append(s)
                let cutoff = Date().addingTimeInterval(-3600)
                if let first = samples.first, first.time < cutoff {
                    samples.removeAll { $0.time < cutoff }
                }
            }
        }
    }

    private func parse(_ data: Data) -> HardwareSample? {
        guard let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        func d(_ k: String, _ src: [String: Any]? = nil) -> Double? { ((src ?? o)[k] as? NSNumber)?.doubleValue }
        let mem = o["memory"] as? [String: Any] ?? [:]
        let temp = o["temp"] as? [String: Any] ?? [:]
        if let soc = o["soc"] as? [String: Any], chip.isEmpty {
            let name = soc["chip_name"] as? String ?? "Apple Silicon"
            let gb = (soc["memory_gb"] as? NSNumber)?.intValue
            chip = gb.map { "\(name) · \($0) GB" } ?? name
        }
        let gib = 1_073_741_824.0
        return HardwareSample(
            time: Date(),
            gpuPercent: (d("gpu_active_ratio") ?? 0) * 100,
            cpuPercent: (d("cpu_active_ratio") ?? ((d("pcpu_active_ratio") ?? 0))) * 100,
            systemWatts: d("sys_power") ?? 0,
            gpuWatts: d("gpu_power") ?? 0,
            cpuWatts: d("cpu_power") ?? 0,
            aneWatts: d("ane_power") ?? 0,
            ramGib: (d("ram_usage", mem) ?? 0) / gib,
            ramTotalGib: (d("ram_total", mem) ?? 0) / gib,
            swapGib: (d("swap_usage", mem) ?? 0) / gib,
            gpuTemp: d("gpu_temp_avg", temp),
            cpuTemp: d("cpu_temp_avg", temp),
            gpuMHz: d("gpu_freq_mhz"))
    }
}
