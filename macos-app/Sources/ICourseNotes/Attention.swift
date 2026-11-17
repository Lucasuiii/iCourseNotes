import AppKit
import SwiftUI

/// One thing that needs the user, gathered from every source the app already reads.
struct AttentionItem: Identifiable, Hashable {
    enum Kind: Int { case cloud, failed, review, push, statusRead }
    enum Target: Hashable { case note(control: String), section(AppSection), url(String) }
    let id: String
    let kind: Kind
    let title: String
    let detail: String
    let target: Target
    /// What exactly was seen: a new cloud run, error or state brings the item back after it is ticked off.
    var version: String = ""
    var ackKey: String { id + "|" + version }

    var icon: String {
        switch kind {
        case .cloud: return "icloud.slash"
        case .failed: return "exclamationmark.triangle"
        case .review: return "checkmark.seal"
        case .push: return "arrow.up.circle"
        case .statusRead: return "wifi.exclamationmark"
        }
    }
    var isProblem: Bool { kind == .cloud || kind == .failed || kind == .statusRead }
}

extension AppModel {
    /// Items the user has not ticked off.
    var attentionItems: [AttentionItem] { allAttentionItems.filter { !acknowledged.contains($0.ackKey) } }
    var hiddenAttentionCount: Int { allAttentionItems.count - attentionItems.count }

    func acknowledge(_ item: AttentionItem) { acknowledge([item]) }

    func acknowledge(_ items: [AttentionItem]) {
        // Keep only keys that still describe a current item, so the stored set cannot grow without bound.
        let current = Set(allAttentionItems.map(\.ackKey))
        acknowledged = acknowledged.intersection(current).union(items.map(\.ackKey))
    }

    func restoreAcknowledged() { acknowledged = [] }

    var allAttentionItems: [AttentionItem] {
        var items: [AttentionItem] = []
        if let c = cloud, c.health == "failing" || c.health == "stale", let run = c.latest {
            let when = shortTime(run.created.map { Date(timeIntervalSince1970: $0) })
            let jobs = (run.failedJobs ?? []).joined(separator: "、")
            let streak = c.consecutiveFailures > 1 ? "，已连续 \(c.consecutiveFailures) 次" : ""
            let detail = c.health == "stale" && run.conclusion == "success"
                ? "超过 26 小时没有成功运行（最近一次 \(when)）"
                : "\(when) 失败\(jobs.isEmpty ? "" : "（\(jobs)）")\(streak)"
            items.append(AttentionItem(id: "cloud", kind: .cloud, title: "云端每日检查异常", detail: detail,
                                       target: run.url.map { .url($0) } ?? .section(.sync), version: "\(run.id)"))
        } else if let e = cloudError {
            items.append(AttentionItem(id: "cloud-read", kind: .statusRead, title: "无法读取云端状态", detail: e, target: .section(.sync), version: e))
        }
        if let e = publicationError {
            items.append(AttentionItem(id: "pub-read", kind: .statusRead, title: "推送状态读取失败",
                                       detail: "显示的是上一次读取的结果 · \(e)", target: .section(.sync), version: e))
        }
        if let e = statusError {
            items.append(AttentionItem(id: "status-read", kind: .statusRead, title: "本机识别状态读取失败", detail: e, target: .section(.recognize), version: e))
        }
        for l in currentQueue?.lectures ?? [] where ["failed", "interrupted", "publication_failed"].contains(l.status) && l.supersededBy == nil {
            items.append(AttentionItem(id: "fail-" + l.subId, kind: .failed, title: "识别失败 · \(l.courseTitle ?? "")",
                                       detail: [l.date, Labels.stage(l.stage ?? l.phase)].compactMap { $0 }.joined(separator: " · "),
                                       target: .section(.recognize), version: l.status + (l.errorType ?? l.runtimeErrorType ?? "")))
        }
        if let q = currentQueue, ["interrupted", "coordinator_failed"].contains(q.status) {
            items.append(AttentionItem(id: "queue-" + q.control, kind: .failed, title: "识别队列\(Labels.status(q.status))",
                                       detail: "启动于 \(shortTime(q.startedDate))", target: .section(.recognize), version: q.status))
        }
        for l in publication?.local ?? notes?.local ?? [] {
            let when = [l.date, l.title].compactMap { $0 }.joined(separator: " ")
            if l.publicationStatus == "failed" {
                items.append(AttentionItem(id: "pubfail-" + l.control, kind: .failed, title: "推送失败 · \(l.courseTitle)",
                                           detail: l.publicationMessage ?? when, target: .section(.sync),
                                           version: (l.publicationError ?? "") + "\(l.updated ?? 0)"))
            } else if l.status == "awaiting_content_review" && l.approved != true {
                items.append(AttentionItem(id: "review-" + l.control, kind: .review, title: "待审核 · \(l.courseTitle)",
                                           detail: when, target: .note(control: l.control), version: "review"))
            } else if l.approved == true && l.publicationStatus == nil {
                items.append(AttentionItem(id: "push-" + l.control, kind: .push, title: "待推送 · \(l.courseTitle)",
                                           detail: when, target: .section(.sync), version: "push"))
            }
        }
        return items.sorted { $0.kind.rawValue < $1.kind.rawValue }
    }

    func open(_ item: AttentionItem) {
        switch item.target {
        case .section(let s): section = s
        case .url(let u): if let url = URL(string: u) { NSWorkspace.shared.open(url) }
        case .note(let control):
            section = .notes
            selectedNote = noteRows.first { $0.control == control }
        }
    }
}

/// Toolbar entry visible on every tab: a calm line when nothing is pending, otherwise a count.
struct AttentionButton: View {
    @Environment(AppModel.self) private var model
    @State private var open = false

    var body: some View {
        let items = model.attentionItems
        let problems = items.filter(\.isProblem).count
        Button { open.toggle() } label: {
            HStack(spacing: 6) {
                if items.isEmpty {
                    Image(systemName: "checkmark.circle").font(.system(size: 13)).foregroundStyle(.secondary)
                } else {
                    Image(systemName: problems > 0 ? "exclamationmark.circle.fill" : "tray.full")
                        .font(.system(size: 13))
                        .foregroundStyle(problems > 0 ? Color.red : Color.orange)
                    Text("\(items.count)").font(.callout.weight(.medium)).monospacedDigit()
                }
            }
            .font(.caption)
        }
        .help(items.isEmpty ? calmLine : "待处理 \(items.count) 项")
        .popover(isPresented: $open, arrowEdge: .bottom) { AttentionList(items: items) { open = false } }
    }

    private var calmLine: String {
        if model.hiddenAttentionCount > 0 { return "无新事项 · 已知悉 \(model.hiddenAttentionCount)" }
        if model.cloud?.health == "running" { return "全部正常 · 云端运行中" }
        if let t = model.cloud?.lastSuccessAt { return "全部正常 · 云端 \(relative(Date(timeIntervalSince1970: t)))成功" }
        return model.cloud == nil ? "全部正常" : "全部正常 · 云端状态未知"
    }
}

struct AttentionList: View {
    @Environment(AppModel.self) private var model
    let items: [AttentionItem]
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(items.isEmpty ? "没有需要处理的事项" : "需要处理").font(.headline)
                Spacer()
                Button {
                    Task { await model.refreshCloud() }
                } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).disabled(model.cloudChecking).help("刷新云端状态")
            }
            .padding(12)
            Divider()
            ForEach(items) { item in
                HStack(spacing: 0) {
                Button { model.open(item); close() } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: item.icon).frame(width: 18)
                            .foregroundStyle(item.isProblem ? Color.red : Color.orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title).font(.callout.weight(.medium))
                            Text(item.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                    .padding(.leading, 12).padding(.vertical, 8)
                }
                .buttonStyle(.plain)
                Divider().frame(height: 28).padding(.horizontal, 6)
                Button { withAnimation(.snappy) { model.acknowledge(item) } } label: {
                    Image(systemName: "checkmark.circle").font(.system(size: 16))
                        .frame(width: 32, height: 32).contentShape(Rectangle())
                }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("标为已知悉：不再提示，直到出现新的情况")
                .accessibilityLabel("标为已知悉")
                .padding(.trailing, 8)
                }
            }
            Divider()
            HStack(alignment: .firstTextBaseline) {
                Text(footer).font(.caption2).foregroundStyle(.secondary)
                Spacer()
                if model.hiddenAttentionCount > 0 {
                    Button("显示已知悉 \(model.hiddenAttentionCount)") { model.restoreAcknowledged() }
                        .buttonStyle(.link).font(.caption2)
                }
                if items.count > 1 {
                    Button("全部已知悉") { withAnimation(.snappy) { model.acknowledge(items) } }
                        .buttonStyle(.link).font(.caption2)
                }
            }
            .padding(12)
        }
        .frame(width: 340)
    }

    private var footer: String {
        guard let c = model.cloud else { return model.cloudError.map { "云端：\($0)" } ?? "云端状态读取中…" }
        let checked = "云端状态检查于 \(relative(Date(timeIntervalSince1970: c.checkedAt)))"
        guard let t = c.lastSuccessAt else { return "\(checked) · 最近 \(c.runs.count) 次每日检查均未成功" }
        return "\(checked) · 上次成功 \(shortTime(Date(timeIntervalSince1970: t)))"
    }
}
