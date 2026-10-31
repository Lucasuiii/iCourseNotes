import Foundation
import Observation
import SwiftUI

enum AppSection: String, CaseIterable, Identifiable {
    case recognize, notes, sync
    var id: String { rawValue }
    var title: String {
        switch self {
        case .recognize: return "识别"
        case .notes: return "笔记"
        case .sync: return "同步"
        }
    }
    var icon: String {
        switch self {
        case .recognize: return "waveform"
        case .notes: return "book.pages"
        case .sync: return "arrow.triangle.2.circlepath"
        }
    }
}

struct Toast: Identifiable, Equatable {
    let id = UUID()
    let text: String
    let isError: Bool
}

@MainActor @Observable
final class AppModel {
    var section: AppSection = .notes {
        didSet { if section == .notes && oldValue != .notes { Task { await loadNotes() } } }
    }
    var bridge: Bridge?
    let macmon = Macmon()

    // progress
    var status: StatusResponse?
    var statusError: String?
    var selectedQueue: String?
    var busyAction: String?

    // scan
    var inventory: InventoryResponse?
    var scanning = false
    var scanStarted: Date?
    var selection: Set<String> = []
    var starting = false

    // notes
    var notes: NotesList?
    var syncing = false
    var selectedNote: NoteRow?
    var remote: RemoteInfo?
    var remoteError: String?
    var noteCourse: String = "全部"

    var toast: Toast?
    var exporting: (done: Int, total: Int)?
    var showBatchExport = false
    @ObservationIgnored private var pdfExporter: PDFExporter?
    var exporter: PDFExporter {
        if let pdfExporter { return pdfExporter }
        let e = PDFExporter(); pdfExporter = e; return e
    }
    private var pollTask: Task<Void, Never>?

    init() {
        if let root = Bridge.locateProject() { bridge = Bridge(projectRoot: root) }
    }

    func startup() {
        guard bridge != nil else { return }
        startPolling()
        // Launch arguments (e.g. `--args -section notes -noteSub 695615`) open a view directly.
        let d = UserDefaults.standard
        if let s = d.string(forKey: "section").flatMap(AppSection.init(rawValue:)) { section = s }
        Task {
            await loadInventory(); await loadNotes()
            if let sub = d.string(forKey: "noteSub") { selectedNote = noteRows.first { $0.subId == sub } }
            // Verification hook: export one note and the newest term (merged) without dialogs.
            if let dir = d.string(forKey: "exportTestDir") { await exportSelfTest(URL(fileURLWithPath: dir)) }
        }
    }

    func setProject(_ url: URL) {
        UserDefaults.standard.set(url.path, forKey: "projectRoot")
        bridge = Bridge.locateProject().map(Bridge.init)
        if bridge == nil { show("所选目录不是 iCourse 项目根目录", error: true) } else { startup() }
    }

    func show(_ text: String, error: Bool = false) {
        toast = Toast(text: text, isError: error)
        let id = toast?.id
        Task { try? await Task.sleep(for: .seconds(error ? 7 : 4)); if toast?.id == id { toast = nil } }
    }

    // MARK: progress

    var currentQueue: QueueInfo? {
        guard let qs = status?.queues else { return nil }
        if let sel = selectedQueue, let q = qs.first(where: { $0.control == sel }) { return q }
        if let latest = status?.latestControl, let q = qs.first(where: { $0.control == latest }) { return q }
        return qs.first
    }

    var activeLecture: LectureRun? {
        guard let q = currentQueue else { return nil }
        return q.lectures.first { $0.subId == q.currentSubId && ["running", "starting"].contains($0.status) }
            ?? q.lectures.first { ["running", "starting"].contains($0.status) }
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshStatus()
                let running = self?.currentQueue?.coordinatorAlive ?? false
                try? await Task.sleep(for: .seconds(running ? 4 : 12))
            }
        }
    }

    func refreshStatus() async {
        guard let bridge else { return }
        var args = ["status"]
        if let sel = selectedQueue { args += ["--queue", sel] }
        if let sub = activeLecture?.subId ?? currentQueue?.currentSubId { args += ["--history-sub", sub] }
        do {
            status = try await bridge.run(args, as: StatusResponse.self, timeout: 40)
            statusError = nil
            if currentQueue?.coordinatorAlive == true { macmon.start() } else { macmon.stop() }
            // A lecture that just finished recognition must appear in 笔记 for review.
            let ready = Set((status?.queues ?? []).flatMap(\.lectures).filter { $0.status == "awaiting_content_review" }.map(\.subId))
            let listed = Set((notes?.local ?? []).map(\.subId))
            if !ready.isSubset(of: listed) { await loadNotes() }
        } catch {
            statusError = error.localizedDescription
        }
    }

    func pause(_ queue: QueueInfo, force: Bool = false, afterCurrent: Bool = false) async -> ActionResponse? {
        guard let bridge else { return nil }
        busyAction = "pause"
        defer { busyAction = nil }
        var args = ["pause", "--control", queue.control]
        if force { args.append("--force") }
        if afterCurrent { args.append("--after-current") }
        do {
            let r = try await bridge.run(args, as: ActionResponse.self)
            if r.ok { show(afterCurrent ? "将在本堂完成后暂停队列" : "已请求暂停，检查点保存后停止") }
            else if r.needsConfirmation != true { show(r.message ?? r.errorType ?? "暂停失败", error: true) }
            await refreshStatus()
            return r
        } catch { show(error.localizedDescription, error: true); return nil }
    }

    func resume(_ queue: QueueInfo, retryFailed: Bool) async {
        guard let bridge else { return }
        busyAction = "resume"
        defer { busyAction = nil }
        var args = ["resume", "--control", queue.control]
        if retryFailed { args.append("--retry-failed") }
        do {
            let r = try await bridge.run(args, as: ActionResponse.self)
            r.ok ? show(retryFailed ? "已重新启动失败课次（复用已保存的块）" : "已从断点续跑") : show(r.message ?? r.errorType ?? "续跑失败", error: true)
            try? await Task.sleep(for: .seconds(1.5))
            await refreshStatus()
        } catch { show(error.localizedDescription, error: true) }
    }

    // MARK: scan

    func loadInventory() async {
        guard let bridge else { return }
        if let r = try? await bridge.run(["inventory"], as: InventoryResponse.self), r.ok {
            inventory = r
            selection = Set((r.ready ?? []).filter { $0.selectable == true }.map(\.subId))
        }
    }

    func scan() async {
        guard let bridge, !scanning else { return }
        scanning = true; scanStarted = Date()
        defer { scanning = false; scanStarted = nil }
        do {
            let r = try await bridge.run(["scan"], as: InventoryResponse.self, timeout: 960)
            if r.ok {
                inventory = r
                selection = Set((r.ready ?? []).filter { $0.selectable == true }.map(\.subId))
                let n = r.ready?.count ?? 0
                show(n > 0 ? "发现 \(n) 堂可识别的新回放" : "没有新的可识别回放")
            } else {
                show(r.message ?? "检查失败：\(r.errorType ?? "未知")", error: true)
            }
        } catch { show(error.localizedDescription, error: true) }
    }

    func startRecognition() async {
        guard let bridge, let control = inventory?.control, !selection.isEmpty else { return }
        starting = true
        defer { starting = false }
        do {
            let r = try await bridge.run(["start", "--control", control, "--sub-ids", selection.sorted().joined(separator: ",")],
                                         as: ActionResponse.self, timeout: 600)
            if r.ok {
                show("识别已启动")
                selectedQueue = nil
                await loadInventory()
                await refreshStatus()
                section = .recognize
            } else {
                show(r.message ?? r.errorType ?? "启动失败", error: true)
            }
        } catch { show(error.localizedDescription, error: true) }
    }

    // MARK: notes

    var noteRows: [NoteRow] {
        var rows: [NoteRow] = []
        for l in notes?.local ?? [] {
            rows.append(NoteRow(subId: l.subId, courseId: l.courseId, courseTitle: l.courseTitle, date: l.date ?? "",
                                title: l.title ?? "", source: .local(control: l.control), status: l.status, figures: nil, emailed: false,
                                approved: l.approved ?? false, term: l.term ?? "", termLabel: l.termLabel ?? "其他"))
        }
        for n in notes?.lectures ?? [] {
            rows.append(NoteRow(subId: n.subId, courseId: n.courseId, courseTitle: n.courseTitle, date: n.date ?? "",
                                title: n.title ?? "", source: .data, status: "published", figures: n.figures, emailed: n.emailed ?? false,
                                term: n.term ?? "", termLabel: n.termLabel ?? "其他"))
        }
        return rows.sorted { $0.date == $1.date ? $0.isLocal && !$1.isLocal : $0.date > $1.date }
    }

    var courses: [String] {
        var seen: [String] = []
        for r in noteRows where !seen.contains(r.courseTitle) { seen.append(r.courseTitle) }
        return seen
    }

    func loadNotes() async {
        guard let bridge else { return }
        do {
            notes = try await bridge.run(["notes-list"], as: NotesList.self, timeout: 60)
            if notes?.syncedAt == nil { await syncNotes() }
        } catch { show(error.localizedDescription, error: true) }
    }

    func syncNotes() async {
        guard let bridge, !syncing else { return }
        syncing = true
        defer { syncing = false }
        do {
            let r = try await bridge.run(["notes-sync"], as: ActionResponse.self, timeout: 400)
            if r.ok {
                notes = try await bridge.run(["notes-list"], as: NotesList.self, timeout: 60)
                show("已同步 data 最新笔记")
            } else {
                show(r.message ?? "同步失败：\(r.errorType ?? "未知")", error: true)
            }
        } catch { show(error.localizedDescription, error: true) }
    }

    func fetchNote(_ row: NoteRow) async throws -> Data {
        guard let bridge else { throw BridgeError(message: "未找到项目目录") }
        let args = row.control.map { ["note", "--control", $0] } ?? ["note", "--sub-id", row.subId]
        let data = try await bridge.raw(args, timeout: 60)
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw BridgeError(message: "笔记数据无效")
        }
        if obj["ok"] as? Bool != true {
            throw BridgeError(message: obj["message"] as? String ?? "读取失败：\(obj["error_type"] as? String ?? "未知")")
        }
        return try JSONSerialization.data(withJSONObject: obj["note"] ?? [:])
    }

    func exportSelfTest(_ dir: URL) async {
        let rows = noteRows.filter { !$0.isLocal }
        guard let first = rows.first, let term = rows.map(\.term).max() else { return }
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try await exporter.export(try await fetchNote(first), to: dir.appending(path: "single.pdf"), transcript: true)
            let termRows = rows.filter { $0.term == term }.sorted { ($0.courseTitle, $0.date) < ($1.courseTitle, $1.date) }
            var items: [PDFExporter.Item] = []
            for r in termRows { items.append(.init(row: r, payload: try await fetchNote(r))) }
            _ = try await exporter.exportBatch(items, folder: nil, merged: dir.appending(path: "merged.pdf"), transcript: false) { _ in }
            try Data("ok \(termRows.count)".utf8).write(to: dir.appending(path: "done.txt"))
        } catch {
            try? Data("error \(error.localizedDescription)".utf8).write(to: dir.appending(path: "done.txt"))
        }
    }

    func approve(control: String, summarySha: String, checks: [String], notes: String) async -> Bool {
        guard let bridge else { return false }
        let checksJSON = String(data: (try? JSONSerialization.data(withJSONObject: checks)) ?? Data("[]".utf8), encoding: .utf8)!
        do {
            let r = try await bridge.run(["approve", "--control", control, "--summary-sha256", summarySha, "--checks", checksJSON, "--notes", notes],
                                         as: ActionResponse.self)
            if r.ok { show("已审核通过，可在“同步”页推送") } else { show(r.message ?? r.errorType ?? "审核未保存", error: true) }
            return r.ok
        } catch { show(error.localizedDescription, error: true); return false }
    }

    func unapprove(_ control: String) async {
        guard let bridge else { return }
        if let r = try? await bridge.run(["unapprove", "--control", control], as: ActionResponse.self), !r.ok {
            show(r.message ?? "无法撤回", error: true)
        }
        await loadNotes()
    }

    func checkRemote() async {
        guard let bridge else { return }
        do {
            remote = try await bridge.run(["remote"], as: RemoteInfo.self, timeout: 90)
            remoteError = nil
        } catch { remoteError = error.localizedDescription }
    }

    func dryRun(control: String) async -> DryRunResult? {
        guard let bridge else { return nil }
        do {
            let r = try await bridge.run(["push", "--control", control, "--dry-run"], as: ActionResponse.self, timeout: 900)
            if let res = r.result { return res }
            show(r.message ?? r.errorType ?? "检查失败", error: true)
        } catch { show(error.localizedDescription, error: true) }
        return nil
    }

    func compare(control: String) async -> CompareResult? {
        guard let bridge else { return nil }
        do { return try await bridge.run(["compare", "--control", control], as: CompareResult.self, timeout: 120) }
        catch { show(error.localizedDescription, error: true); return nil }
    }

    func keepRemote(control: String) async {
        guard let bridge else { return }
        do {
            let r = try await bridge.run(["keep-remote", "--control", control], as: ActionResponse.self, timeout: 120)
            r.ok ? show("已保留云端版本，本机结果不会写入 data") : show(r.message ?? r.errorType ?? "操作失败", error: true)
        } catch { show(error.localizedDescription, error: true) }
        await loadNotes(); await checkRemote()
    }

    func push(control: String, email: Bool, overwrite: String? = nil) async {
        guard let bridge else { return }
        var args = ["push", "--control", control]
        if email { args.append("--email") }
        if let overwrite { args += ["--overwrite-remote", overwrite] }
        do {
            let r = try await bridge.run(args, as: ActionResponse.self)
            guard r.ok else { show(r.message ?? r.errorType ?? "推送未开始", error: true); return }
            show("开始推送")
            for _ in 0..<120 {
                try? await Task.sleep(for: .seconds(3))
                await checkRemote()
                let st = remote?.local?.first { $0.control == control }?.publicationStatus
                if st == "completed" { show("推送完成，已远端回读核验"); break }
                if st == "failed" { show("推送失败，未自动重试", error: true); break }
            }
            await syncNotes()
            await checkRemote()
        } catch { show(error.localizedDescription, error: true) }
    }
}
