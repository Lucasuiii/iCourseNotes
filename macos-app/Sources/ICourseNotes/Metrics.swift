import Charts
import SwiftUI

enum TrendRange: Int, CaseIterable, Identifiable {
    case five = 5, fifteen = 15, hour = 60
    var id: Int { rawValue }
    var label: String { self == .hour ? "1 小时" : "\(rawValue) 分钟" }
}

// MARK: - hardware

struct HardwareStrip: View {
    let macmon: Macmon
    var body: some View {
        let s = macmon.latest
        let recent = Array(macmon.samples.suffix(90))
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 5), spacing: 12) {
            MetricTile(icon: "cpu", title: "GPU 活跃", value: s.map { String(format: "%.0f", $0.gpuPercent) }, unit: "%",
                       detail: s?.gpuMHz.map { String(format: "%.0f MHz", $0) }, tint: .blue,
                       spark: recent.map(\.gpuPercent), sparkMax: 100)
            MetricTile(icon: "bolt.fill", title: "系统功耗", value: s.map { String(format: "%.1f", $0.systemWatts) }, unit: "W",
                       detail: s.map { String(format: "CPU %.1f W · ANE %.1f W", $0.cpuWatts, $0.aneWatts) }, tint: .orange,
                       spark: recent.map(\.systemWatts))
            MetricTile(icon: "square.stack.3d.up.fill", title: "GPU 功耗", value: s.map { String(format: "%.1f", $0.gpuWatts) }, unit: "W",
                       detail: s.map { $0.systemWatts > 0 ? String(format: "占系统 %.0f%%", $0.gpuWatts / $0.systemWatts * 100) : "—" },
                       tint: .pink, spark: recent.map(\.gpuWatts))
            MemoryTile(sample: s)
            MetricTile(icon: "thermometer.medium", title: "温度", value: s?.gpuTemp.map { String(format: "%.0f", $0) }, unit: "°C",
                       detail: s?.cpuTemp.map { String(format: "CPU %.0f °C", $0) }, tint: .teal,
                       spark: recent.compactMap(\.gpuTemp))
        }
    }
}

struct MetricTile: View {
    let icon: String
    let title: String
    let value: String?
    let unit: String
    var detail: String?
    let tint: Color
    var spark: [Double] = []
    var sparkMax: Double? = nil

    var body: some View {
        Card(padding: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Label(title, systemImage: icon).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    .labelStyle(TintedIconLabel(tint: tint))
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(value ?? "—").font(.system(size: 28, weight: .semibold, design: .rounded)).monospacedDigit()
                        .contentTransition(.numericText())
                    Text(unit).font(.callout.weight(.medium)).foregroundStyle(.secondary)
                }
                Sparkline(values: spark, max: sparkMax, tint: tint).frame(height: 22)
                Text(detail ?? " ").font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
            }
        }
    }
}

struct MemoryTile: View {
    let sample: HardwareSample?
    var body: some View {
        Card(padding: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Label("内存", systemImage: "memorychip").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    .labelStyle(TintedIconLabel(tint: .purple))
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(sample.map { String(format: "%.1f", $0.ramGib) } ?? "—")
                        .font(.system(size: 28, weight: .semibold, design: .rounded)).monospacedDigit()
                    Text(sample.map { String(format: "/ %.0f GB", $0.ramTotalGib) } ?? "GB").font(.callout.weight(.medium)).foregroundStyle(.secondary)
                }
                let ratio = sample.map { $0.ramTotalGib > 0 ? $0.ramGib / $0.ramTotalGib : 0 } ?? 0
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.quaternary)
                        Capsule().fill(LinearGradient(colors: [.purple.opacity(0.7), ratio > 0.85 ? .red : .purple], startPoint: .leading, endPoint: .trailing))
                            .frame(width: g.size.width * ratio)
                    }
                }
                .frame(height: 8).padding(.vertical, 7)
                Text(sample.map { String(format: "Swap %.2f GB", $0.swapGib) } ?? " ").font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }
}

struct TintedIconLabel: LabelStyle {
    let tint: Color
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 5) { configuration.icon.foregroundStyle(tint); configuration.title }
    }
}

struct Sparkline: View {
    let values: [Double]
    var max: Double?
    let tint: Color
    var body: some View {
        Chart {
            ForEach(Array(values.enumerated()), id: \.offset) { i, v in
                AreaMark(x: .value("t", i), y: .value("v", v))
                    .foregroundStyle(LinearGradient(colors: [tint.opacity(0.35), tint.opacity(0.02)], startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("t", i), y: .value("v", v))
                    .foregroundStyle(tint).lineStyle(StrokeStyle(lineWidth: 1.4)).interpolationMethod(.monotone)
            }
        }
        .chartXAxis(.hidden).chartYAxis(.hidden)
        .chartYScale(domain: 0...(max ?? Swift.max(values.max() ?? 1, 1) * 1.1))
    }
}

struct TrendChart: View {
    let title: String
    let unit: String
    let points: [HardwareSample]
    let value: KeyPath<HardwareSample, Double>
    let color: Color
    var domain: ClosedRange<Double>? = nil
    var secondary: KeyPath<HardwareSample, Double>? = nil
    var secondaryLabel: String = ""
    var start: Date = .distantPast

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                if let mean = mean { Text(String(format: "均值 %.1f %@", mean, unit)).font(.caption2).foregroundStyle(.tertiary) }
                if secondary != nil {
                    Spacer()
                    HStack(spacing: 4) { Capsule().fill(Color.pink).frame(width: 10, height: 3); Text(secondaryLabel).font(.caption2).foregroundStyle(.secondary) }
                }
            }
            Chart {
                ForEach(points) { p in
                    AreaMark(x: .value("时间", p.time), y: .value(title, p[keyPath: value]), series: .value("s", "a"))
                        .foregroundStyle(LinearGradient(colors: [color.opacity(0.28), color.opacity(0.0)], startPoint: .top, endPoint: .bottom))
                        .interpolationMethod(.monotone)
                    LineMark(x: .value("时间", p.time), y: .value(title, p[keyPath: value]), series: .value("s", "a"))
                        .foregroundStyle(color).lineStyle(StrokeStyle(lineWidth: 1.6)).interpolationMethod(.monotone)
                    if let secondary {
                        LineMark(x: .value("时间", p.time), y: .value(secondaryLabel, p[keyPath: secondary]), series: .value("s", "b"))
                            .foregroundStyle(Color.pink.opacity(0.85)).lineStyle(StrokeStyle(lineWidth: 1.2, dash: [3, 2]))
                            .interpolationMethod(.monotone)
                    }
                }
            }
            .chartYScale(domain: domain ?? 0...Swift.max(1, (points.map { $0[keyPath: value] }.max() ?? 1) * 1.15))
            .chartXScale(domain: start...Date())
            .chartXAxis { AxisMarks(values: .automatic(desiredCount: 5)) { _ in AxisGridLine().foregroundStyle(.quaternary); AxisValueLabel(format: .dateTime.hour(.twoDigits(amPM: .omitted)).minute()) } }
            .chartYAxis { AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { _ in AxisGridLine().foregroundStyle(.quaternary); AxisValueLabel() } }
            .frame(height: 160)
        }
    }

    private var mean: Double? {
        points.isEmpty ? nil : points.map { $0[keyPath: value] }.reduce(0, +) / Double(points.count)
    }
}

// MARK: - lecture card

struct LectureRunCard: View {
    let lecture: LectureRun
    let isCurrent: Bool
    let history: [PerfPoint]

    static let steps: [(String, String)] = [("acquisition", "获取"), ("vad", "切分"), ("ppt", "课件"), ("asr", "识别"),
                                            ("summary", "摘要"), ("candidate_validation", "校验"), ("independent_review", "审核")]

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(lecture.courseTitle ?? lecture.courseId).font(.headline)
                        Text("\(lecture.date.map { shortDate($0) + " " + weekday($0) } ?? "") \(sessionLabel(lecture.title, date: lecture.date))")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let e = lecture.errorType ?? lecture.runtimeErrorType, ["failed", "publication_failed", "paused"].contains(lecture.status) {
                        Text(e).font(.caption.monospaced()).foregroundStyle(.secondary).padding(.trailing, 4)
                    }
                    StatusPill(status: lecture.status)
                }
                StageStepper(current: currentStep, status: lecture.status)
                if showBlocks {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("识别块").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                            Spacer()
                            Text(blocksText).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        ProgressView(value: lecture.fraction ?? 0)
                            .tint(Color.status(lecture.status))
                    }
                }
                HStack(spacing: 18) {
                    if let a = lecture.audioSeconds { Fact(icon: "waveform", text: "音频 \(Labels.duration(a))") }
                    if let m = lecture.missingSeconds { Fact(icon: m == 0 ? "checkmark.seal" : "exclamationmark.triangle", text: "缺口 \(Int(m)) 秒") }
                    if let eta { Fact(icon: "timer", text: eta) }
                    if let t = lecture.checkpointTime { Fact(icon: "externaldrive", text: "检查点 \(relative(Date(timeIntervalSince1970: t)))") }
                    if let c = lecture.dataCommit { Fact(icon: "arrow.up.doc", text: "data \(c.prefix(7))") }
                    if let p = lecture.publication?.status { Fact(icon: "paperplane", text: "发布 \(Labels.status(p))") }
                    Spacer()
                }
            }
        }
        .overlay(alignment: .leading) {
            if isCurrent { RoundedRectangle(cornerRadius: 2).fill(Color.blue).frame(width: 3).padding(.vertical, 14) }
        }
    }

    private var currentStep: Int {
        switch lecture.status {
        case "awaiting_content_review", "awaiting_review", "awaiting_publication": return 6
        case "completed", "completed_editorial", "delivered", "published", "publishing": return 7
        default: break
        }
        let stage = lecture.stage ?? lecture.phase ?? "acquisition"
        switch stage {
        case "starting", "authentication", "source_selection", "acquisition": return 0
        case "vad": return 1
        case "ppt": return 2
        case "asr", "asr_fallback": return 3
        case "summary": return 4
        case "candidate_validation", "complete", "content_validation": return 5
        case "independent_review": return 6
        default: return 0
        }
    }

    private var showBlocks: Bool { (lecture.plannedBlocks ?? 0) > 0 }
    private var blocksText: String {
        let s = Int(lecture.savedBlocks ?? 0), p = Int(lecture.plannedBlocks ?? 0)
        return "\(s) / \(p) 块" + (p > 0 ? String(format: " · %.0f%%", Double(s) / Double(p) * 100) : "")
    }

    /// ASR-only remaining time from the block rate of the last ~10 minutes.
    private var eta: String? {
        guard isCurrent, lecture.stage == "asr", let planned = lecture.plannedBlocks, let saved = lecture.savedBlocks, planned > saved else { return nil }
        let pts = history.compactMap { p -> (Double, Double)? in
            guard let t = p.time, let b = p.savedBlocks else { return nil }; return (t, b)
        }.filter { $0.0 > Date().timeIntervalSince1970 - 900 }
        guard let first = pts.first, let last = pts.last, last.1 > first.1, last.0 > first.0 else { return nil }
        let rate = (last.1 - first.1) / (last.0 - first.0)
        return "识别剩余约 \(Labels.duration((planned - saved) / rate))"
    }
}

struct Fact: View {
    let icon: String
    let text: String
    var body: some View {
        Label(text, systemImage: icon).font(.caption).foregroundStyle(.secondary)
    }
}

struct StageStepper: View {
    let current: Int
    let status: String

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(LectureRunCard.steps.enumerated()), id: \.offset) { i, step in
                let done = i < current || current >= LectureRunCard.steps.count
                let active = i == current && current < LectureRunCard.steps.count
                VStack(spacing: 5) {
                    ZStack {
                        Circle().fill(done ? Color.green : active ? Color.status(status) : Color.secondary.opacity(0.15))
                            .frame(width: 18, height: 18)
                        if done { Image(systemName: "checkmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.white) }
                        else if active && status == "running" { Circle().fill(.white).frame(width: 6, height: 6) }
                        else if active && status == "paused" { Image(systemName: "pause.fill").font(.system(size: 7)).foregroundStyle(.white) }
                        else if active && status.contains("fail") { Image(systemName: "xmark").font(.system(size: 8, weight: .bold)).foregroundStyle(.white) }
                    }
                    Text(step.1).font(.caption2.weight(active ? .semibold : .regular)).foregroundStyle(active ? .primary : .secondary)
                }
                .frame(width: 44)
                if i < LectureRunCard.steps.count - 1 {
                    Rectangle().fill(i < current ? Color.green.opacity(0.6) : Color.secondary.opacity(0.15))
                        .frame(height: 2).offset(y: -9)
                }
            }
        }
    }
}
