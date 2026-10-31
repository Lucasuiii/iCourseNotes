import Foundation

struct BridgeError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct StatusResponse: Decodable {
    let ok: Bool
    let time: Double?
    let latestControl: String?
    let queues: [QueueInfo]?
    let workerActive: Bool?
    let cryptoError: String?
    let source: SourceInfo?
    let freeDiskGib: Double?
    let history: [PerfPoint]?
}

struct SourceInfo: Decodable { let path: String?; let sha: String? }

struct QueueInfo: Decodable, Identifiable, Hashable {
    var id: String { control }
    let control: String
    let name: String
    let status: String
    let started: Double?
    let finished: Double?
    let currentSubId: String?
    let coordinatorAlive: Bool
    let pauseRequested: Bool
    let appManaged: Bool
    let errorType: String?
    let lectures: [LectureRun]

    var startedDate: Date? { started.map { Date(timeIntervalSince1970: $0) } }
    var isRunning: Bool { coordinatorAlive && status == "running" }
    var canResume: Bool {
        appManaged && !coordinatorAlive && lectures.contains { $0.status == "paused" }
    }
    var canRetryFailed: Bool {
        appManaged && !coordinatorAlive && lectures.contains { $0.status == "failed" }
    }
}

struct LectureRun: Decodable, Identifiable, Hashable {
    var id: String { subId }
    let courseId: String
    let subId: String
    let courseTitle: String?
    let date: String?
    let title: String?
    let control: String
    let output: String?
    let status: String
    let phase: String?
    let errorType: String?
    let dataCommit: String?
    let emailStatus: String?
    let cleanupComplete: Bool?
    let savedBlocks: Double?
    let plannedBlocks: Double?
    let stage: String?
    let audioSeconds: Double?
    let missingSeconds: Double?
    let checkpointTime: Double?
    let runtimeErrorType: String?
    let workerAlive: Bool?
    let safeToPause: Bool?
    let performance: PerfPoint?
    let publication: PublicationInfo?

    var fraction: Double? {
        guard let s = savedBlocks, let p = plannedBlocks, p > 0 else { return nil }
        return min(1, s / p)
    }
}

struct PublicationInfo: Decodable, Hashable {
    let status: String?
    let errorType: String?
    let notesCommit: String?
    let dataCommit: String?
    let emailStatus: String?
    let cleanupComplete: Bool?
}

struct PerfPoint: Decodable, Hashable {
    let time: Double?
    let gpuActivePercent: Double?
    let systemWatts: Double?
    let gpuWatts: Double?
    let ramGib: Double?
    let swapGib: Double?
    let savedBlocks: Double?
    let plannedBlocks: Double?
}

// MARK: scan

struct InventoryResponse: Decodable {
    let ok: Bool
    let control: String?
    let today: String?
    let mode: String?
    let baselineRevision: String?
    let started: Bool?
    let scannedAt: Double?
    let ready: [InventoryLecture]?
    let deferred: [InventoryLecture]?
    let courses: [InventoryCourse]?
    let errorType: String?
    let message: String?
}

struct InventoryLecture: Decodable, Identifiable, Hashable {
    var id: String { subId }
    let courseId: String
    let courseTitle: String
    let subId: String
    let title: String?
    let date: String
    let selectable: Bool?
    let localExisting: Bool?
    let resourceError: String?
    let localControls: [LocalControl]?
}

struct LocalControl: Decodable, Hashable { let control: String; let status: String }

struct InventoryCourse: Decodable, Identifiable, Hashable {
    var id: String { courseId }
    let courseId: String
    let courseTitle: String
    let lectures: [CourseLecture]
}

struct CourseLecture: Decodable, Hashable, Identifiable {
    var id: String { subId }
    let subId: String
    let title: String?
    let date: String?
    let available: Bool?
    let hasSummary: Bool?
    let deleted: Bool?
    let sessionSelected: Bool?
    let localNotNeeded: Bool?
    let localPending: Bool?
}

// MARK: notes

struct NotesList: Decodable {
    let ok: Bool
    let revision: String?
    let syncedAt: Double?
    let lectures: [NoteIndex]?
    let local: [LocalNoteIndex]?
}

struct NoteIndex: Decodable, Hashable, Identifiable {
    var id: String { subId }
    let subId: String
    let courseId: String
    let courseTitle: String
    let date: String?
    let title: String?
    let emailed: Bool?
    let figures: Int?
    let transcriptChars: Int?
    let summaryChars: Int?
    let term: String?
    let termLabel: String?
}

struct LocalNoteIndex: Decodable, Hashable, Identifiable {
    var id: String { control }
    let subId: String
    let courseId: String
    let courseTitle: String
    let date: String?
    let title: String?
    let control: String
    let status: String?
    let dataCommit: String?
    let approved: Bool?
    let approvedAt: Double?
    let appManaged: Bool?
    let publicationStatus: String?
    let publicationError: String?
    let notesCommit: String?
    let emailStatus: String?
    let updated: Double?
    let term: String?
    let termLabel: String?
}

struct RemoteInfo: Decodable {
    let ok: Bool
    let repository: String?
    let remoteHead: String?
    let cachedRevision: String?
    let syncedAt: Double?
    let upToDate: Bool
    let local: [LocalNoteIndex]?
}

/// Unified row for the notes browser.
struct NoteRow: Hashable, Identifiable {
    enum Source: Hashable { case data, local(control: String) }
    var id: String { source == .data ? "d" + subId : "l" + subId }
    let subId: String
    let courseId: String
    let courseTitle: String
    let date: String
    let title: String
    let source: Source
    let status: String?
    let figures: Int?
    let emailed: Bool
    var approved = false
    var term = ""
    var termLabel = "其他"

    var isLocal: Bool { if case .local = source { return true } else { return false } }
    var control: String? { if case .local(let c) = source { return c } else { return nil } }
}

struct ActionResponse: Decodable {
    let ok: Bool
    let message: String?
    let errorType: String?
    let needsConfirmation: Bool?
    let stage: String?
    let started: Bool?
    let pid: Int?
    let revision: String?
    let lectures: AnyCount?
    let dryRun: Bool?
    let result: DryRunResult?
}

/// `lectures` is a count for notes-sync and a list for start.
enum AnyCount: Decodable {
    case count(Int), other
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let n = try? c.decode(Int.self) { self = .count(n) } else { self = .other }
    }
}

struct DryRunResult: Decodable {
    let status: String?
    let base: String?
    let blocks: Int?
    let figures: Int?
    let summaryReviewStatus: String?
    let scopeVerified: Bool?
    let errorType: String?
    let detail: String?
}

// MARK: display helpers

enum Labels {
    static func status(_ s: String?) -> String {
        switch s ?? "" {
        case "pending": return "排队中"
        case "starting": return "启动中"
        case "running": return "识别中"
        case "paused": return "已暂停"
        case "failed": return "失败"
        case "publication_failed": return "发布失败"
        case "awaiting_content_review", "awaiting_review": return "待审核"
        case "awaiting_publication": return "待发布"
        case "publishing": return "发布中"
        case "published", "delivered": return "已发布"
        case "completed", "completed_editorial", "complete": return "已完成"
        case "not_needed": return "无需处理"
        case "blocked_resource": return "资源不可用"
        case "finished_with_failures": return "部分失败"
        case "coordinator_failed": return "协调器失败"
        case "interrupted": return "意外中断"
        case "awaiting_resources", "no_new_available_lectures": return "无可用回放"
        case "not_started": return "未启动"
        default: return s ?? "—"
        }
    }

    static func stage(_ s: String?) -> String {
        switch s ?? "" {
        case "starting": return "准备"
        case "authentication": return "校园登录"
        case "source_selection": return "选择媒体源"
        case "acquisition": return "获取音频"
        case "vad": return "语音切分"
        case "ppt": return "课件 OCR"
        case "asr": return "本机识别"
        case "asr_fallback": return "缺口补救"
        case "summary": return "摘要与复核"
        case "candidate_validation": return "候选校验"
        case "content_validation": return "导出审核材料"
        case "independent_review": return "等待人工审核"
        case "complete": return "完成"
        case "publication": return "发布"
        case "editorial_delivery": return "交付"
        case "cleanup": return "清理"
        default: return s ?? "—"
        }
    }

    static func publication(_ s: String?) -> String {
        switch s ?? "" {
        case "validating": return "校验中"
        case "publishing": return "推送中"
        case "published": return "已推送，回读完成"
        case "mailing": return "发送邮件"
        case "cleaning": return "清理中间文件"
        case "completed": return "已完成"
        case "failed": return "失败"
        default: return s ?? "—"
        }
    }

    static func duration(_ seconds: Double?) -> String {
        guard let s = seconds, s.isFinite else { return "—" }
        let m = Int(s) / 60
        return m >= 60 ? "\(m / 60) 小时 \(m % 60) 分" : "\(m) 分钟"
    }
}
