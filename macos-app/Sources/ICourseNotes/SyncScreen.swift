import SwiftUI

/// GitHub data: pull the newest published notes, push lectures reviewed on this Mac.
struct SyncScreen: View {
    @Environment(AppModel.self) private var model
    @State private var pushTarget: LocalNoteIndex?
    @State private var email = false
    @State private var checking: String?
    @State private var dryRuns: [String: DryRunResult] = [:]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                pullBlock
                pushBlock
                historyBlock
            }
            .padding(.horizontal, 32).padding(.vertical, 26)
            .frame(maxWidth: 860)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("同步")
        .task { await model.checkRemote() }
        .sheet(item: $pushTarget) { item in pushSheet(item) }
    }

    // MARK: pull

    private var pullBlock: some View {
        let r = model.remote
        return VStack(alignment: .leading, spacing: 12) {
            Text("GitHub data").font(.title2.weight(.semibold))
            HStack(spacing: 16) {
                ZStack {
                    Circle().fill((r?.upToDate ?? false) ? Color.green.opacity(0.15) : Color.blue.opacity(0.15))
                    Image(systemName: (r?.upToDate ?? false) ? "checkmark.icloud" : "icloud.and.arrow.down")
                        .font(.title2).foregroundStyle((r?.upToDate ?? false) ? .green : .blue)
                }
                .frame(width: 52, height: 52)
                VStack(alignment: .leading, spacing: 3) {
                    if let r {
                        Text(r.upToDate ? "本机笔记已是最新" : r.cachedRevision == nil ? "尚未拉取笔记" : "云端有新的提交").font(.headline)
                        Text("\(r.repository ?? "") · data \(r.remoteHead?.prefix(7) ?? "—") · 本机 \(r.cachedRevision?.prefix(7) ?? "—") · 拉取于 \(relative(r.syncedAt.map { Date(timeIntervalSince1970: $0) }))")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text(model.remoteError ?? "正在连接 GitHub…").font(.headline)
                    }
                }
                Spacer()
                Button { Task { await model.checkRemote() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("重新检查云端")
                Button { Task { await model.syncNotes(); await model.checkRemote() } } label: {
                    HStack { if model.syncing { ProgressView().controlSize(.small) }; Text(model.syncing ? "拉取中" : "拉取最新") }
                }
                .buttonStyle(.borderedProminent).disabled(model.syncing)
            }
            .padding(18)
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            Text("拉取只读：解密云端 data 后加密缓存在本机，不修改云端。").font(.caption).foregroundStyle(.tertiary)
        }
    }

    // MARK: push

    private var local: [LocalNoteIndex] { model.remote?.local ?? model.notes?.local ?? [] }

    private var pushBlock: some View {
        let approved = local.filter { $0.approved == true && $0.publicationStatus == nil }
        let reviewing = local.filter { $0.approved != true && $0.status == "awaiting_content_review" }
        let inFlight = local.filter { ["validating", "publishing", "mailing", "cleaning", "published"].contains($0.publicationStatus ?? "") }
        let failed = local.filter { $0.publicationStatus == "failed" }
        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("推送").font(.title2.weight(.semibold))
                Spacer()
                Text("每堂单独推送，只写入这一堂").font(.caption).foregroundStyle(.secondary)
            }
            if approved.isEmpty && reviewing.isEmpty && inFlight.isEmpty && failed.isEmpty {
                HStack(spacing: 10) {
                    Image(systemName: "tray").foregroundStyle(.secondary)
                    Text("没有待推送的本机结果").foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(16)
                .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            ForEach(inFlight) { item in row(item, accessory: AnyView(HStack { ProgressView().controlSize(.small); Text(Labels.publication(item.publicationStatus)).foregroundStyle(.secondary) })) }
            ForEach(approved) { item in
                row(item, accessory: AnyView(HStack(spacing: 8) {
                    if let d = dryRuns[item.control] {
                        Image(systemName: d.status == "passed" ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(d.status == "passed" ? .green : .red)
                            .help(d.status == "passed" ? "推送前检查通过（未推送）" : "检查未通过：\(d.errorType ?? "") \(d.detail ?? "")")
                    }
                    Button { Task { await dryRun(item) } } label: {
                        if checking == item.control { ProgressView().controlSize(.small) } else { Text("检查") }
                    }
                    .help("按推送流程在本机构建并校验，不推送、不发邮件")
                    .disabled(checking != nil)
                    Button("推送…") { email = false; pushTarget = item }.buttonStyle(.borderedProminent)
                }))
            }
            ForEach(failed) { item in
                row(item, accessory: AnyView(HStack(spacing: 8) {
                    Label(item.publicationConflict == true ? "云端已有这堂课" : (item.publicationError ?? "失败"),
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(item.publicationConflict == true ? .orange : .red)
                        .help("没有写入 data；断点与备份已保留")
                    Button(item.publicationConflict == true ? "选择版本…" : "重新推送…") { pushTarget = item }
                }))
            }
            ForEach(reviewing) { item in
                row(item, accessory: AnyView(Button("去审核") {
                    model.section = .notes
                    model.selectedNote = model.noteRows.first { $0.control == item.control }
                }))
            }
        }
    }

    private var historyBlock: some View {
        let done = local.filter { $0.publicationStatus == "completed" }.sorted { ($0.updated ?? 0) > ($1.updated ?? 0) }
        return Group {
            if !done.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("最近推送").font(.headline)
                    ForEach(done.prefix(8)) { item in
                        HStack {
                            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                            Text("\(item.courseTitle) · \(shortDate(item.date ?? ""))")
                            Spacer()
                            Text(item.emailStatus == "sent" ? "已发邮件" : "未发邮件").font(.caption).foregroundStyle(.secondary)
                            if let c = item.notesCommit {
                                Link(String(c.prefix(7)), destination: URL(string: "https://github.com/Lucasuiii/iCourseNotes/commit/\(c)")!)
                                    .font(.caption.monospaced())
                            }
                        }
                        .font(.callout)
                    }
                }
            }
        }
    }

    private func row(_ item: LocalNoteIndex, accessory: AnyView) -> some View {
        HStack(spacing: 12) {
            DateBadge(date: item.date ?? "", tint: .purple)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.courseTitle).font(.body.weight(.medium))
                Text(item.approved == true ? "已审核 \(relative(item.approvedAt.map { Date(timeIntervalSince1970: $0) }))" : "等待审核")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            accessory
        }
        .padding(12)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func pushSheet(_ item: LocalNoteIndex) -> some View {
        PushSheet(item: item) { pushTarget = nil }
    }

    private func dryRun(_ item: LocalNoteIndex) async {
        checking = item.control
        defer { checking = nil }
        if let r = await model.dryRun(control: item.control) { dryRuns[item.control] = r }
    }
}

/// Push dialog. It first compares with data; when the cloud already has this lecture the user
/// picks which version stays.
struct PushSheet: View {
    @Environment(AppModel.self) private var model
    let item: LocalNoteIndex
    let close: () -> Void
    @State private var result: CompareResult?
    @State private var failed = false
    @State private var email = false
    @State private var confirmOverwrite = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                Text(result?.conflict == true ? "云端已有这堂课" : "推送到 GitHub data").font(.title2.weight(.semibold))
                Text("\(item.courseTitle) · \(shortDate(item.date ?? ""))").foregroundStyle(.secondary)
            }
            if let r = result {
                if r.conflict { conflict(r) } else { plain }
            } else if failed {
                Label("无法读取云端 data，请稍后再试", systemImage: "wifi.exclamationmark").foregroundStyle(.secondary)
            } else {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text("正在与云端对比…").foregroundStyle(.secondary) }
                    .frame(maxWidth: .infinity, minHeight: 80)
            }
            HStack {
                Spacer()
                Button("取消") { close() }.keyboardShortcut(.cancelAction)
                if let r = result {
                    if r.conflict {
                        Button("保留云端") {
                            close(); Task { await model.keepRemote(control: item.control) }
                        }
                        Button("用本机覆盖云端") {
                            let sha = r.remote?.summarySha256, mail = email
                            close(); Task { await model.push(control: item.control, email: mail, overwrite: sha) }
                        }
                        .buttonStyle(.borderedProminent).tint(.orange)
                        .disabled(!confirmOverwrite || r.remote?.summarySha256 == nil || r.remote?.deleted == true)
                    } else {
                        Button(email ? "推送并发邮件" : "推送") {
                            let mail = email
                            close(); Task { await model.push(control: item.control, email: mail) }
                        }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    }
                }
            }
        }
        .padding(24)
        .frame(width: 520)
        .task {
            result = await model.compare(control: item.control)
            failed = result == nil
        }
    }

    private var plain: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Label("先备份最新 data，再只替换这一堂并核对范围", systemImage: "externaldrive.badge.checkmark")
                Label("加密往返核验后普通推送，不强推；推送后从云端回读比对", systemImage: "arrow.up.circle")
                Label("成功后清理音频与中间文件，保留摘要、转录和精选图", systemImage: "trash.slash")
            }
            .font(.callout).foregroundStyle(.secondary)
            Divider()
            emailToggle
            Text("此操作只执行一次，不能撤销").font(.caption).foregroundStyle(.orange)
        }
    }

    private func conflict(_ r: CompareResult) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("云端 data 里这堂课已经有笔记（可能是云端每日任务识别的）。选择保留哪一份：")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: 12) {
                side("云端版本", tint: .blue, rows: [
                    ("摘要", "\(r.remote?.summaryChars ?? 0) 字"),
                    ("配图", "\(r.remote?.figures ?? 0) 张"),
                    ("转录", "\(r.remote?.transcriptChars ?? 0) 字"),
                    ("模型", r.remote?.summaryModel ?? "—"),
                    ("邮件", r.remote?.emailed == true ? "已发送" : "未发送"),
                ])
                side("本机版本", tint: .purple, rows: [
                    ("摘要", "\(r.local?.summaryChars ?? 0) 字"),
                    ("配图", "\(r.local?.figures ?? 0) 张"),
                    ("转录", "\(r.local?.transcriptChars ?? 0) 字"),
                    ("理论审核", r.local?.review == "passed" ? "通过" : r.local?.review == "revised" ? "修订一次" : (r.local?.review ?? "—")),
                    ("审核", "已由你审核"),
                ])
            }
            Button {
                close()
                model.section = .notes
                model.selectedNote = model.noteRows.first { !$0.isLocal && $0.subId == item.subId }
            } label: { Label("先在笔记里对照阅读两份", systemImage: "book.pages") }
            .buttonStyle(.link)
            Divider()
            if r.remote?.deleted == true {
                Label("这堂课在 data 中已删除并永久忽略，不能覆盖", systemImage: "nosign").foregroundStyle(.red)
            } else {
                Toggle(isOn: $confirmOverwrite) {
                    Text("我确认用本机版本替换云端笔记。被替换的云端版本会加密备份在本机，并另存一份可读副本。")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                }
                .toggleStyle(.checkbox)
                emailToggle
            }
        }
    }

    private var emailToggle: some View {
        Toggle(isOn: $email) {
            VStack(alignment: .leading, spacing: 2) {
                Text("同时发送邮件")
                Text("只尝试一次；云端已发过邮件的不会重发").font(.caption).foregroundStyle(.secondary)
            }
        }
        .toggleStyle(.switch)
    }

    private func side(_ title: String, tint: Color, rows: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.headline).foregroundStyle(tint)
            ForEach(rows, id: \.0) { k, v in
                HStack { Text(k).foregroundStyle(.secondary); Spacer(); Text(v) }.font(.callout)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
    }
}
