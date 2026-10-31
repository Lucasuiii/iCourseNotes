import SwiftUI

/// One page for recognition: check new playback, choose lectures, follow the queue.
/// Hardware monitoring appears only while a queue is actually recognising.
struct RecognizeScreen: View {
    @Environment(AppModel.self) private var model
    @State private var range: TrendRange = .fifteen
    @State private var confirmStart = false
    @State private var pauseConfirm: (queue: QueueInfo, message: String)?
    @State private var showOverview = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                if let q = model.currentQueue, q.coordinatorAlive {
                    queueBlock(q)
                    scanBlock
                } else {
                    scanBlock
                    if let q = model.currentQueue { queueBlock(q) }
                }
                if let err = model.statusError {
                    Label(err, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                }
            }
            .padding(.horizontal, 32).padding(.vertical, 26)
            .frame(maxWidth: 980)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("识别")
        .toolbar {
            ToolbarItemGroup {
                if let qs = model.status?.queues, qs.count > 1 {
                    Menu {
                        ForEach(qs) { q in
                            Button("\(shortTime(q.startedDate)) · \(q.lectures.map { $0.courseTitle ?? "" }.joined(separator: "、")) · \(Labels.status(q.status))") {
                                model.selectedQueue = q.control; Task { await model.refreshStatus() }
                            }
                        }
                    } label: { Label("历史队列", systemImage: "clock.arrow.circlepath") }
                    .help("查看以前的识别队列")
                }
            }
        }
        .confirmationDialog("开始本机识别？", isPresented: $confirmStart) {
            Button("开始识别 \(model.selection.count) 堂") { Task { await model.startRecognition() } }
            Button("取消", role: .cancel) {}
        } message: {
            Text("按最新优先依次处理：获取音频 → 单模型双块识别 → 课件 OCR、作业视觉、精选配图 → 摘要与理论复核。完成后停在待审核，不会自动推送或发邮件。")
        }
        .confirmationDialog("现在暂停不太安全", isPresented: Binding(get: { pauseConfirm != nil }, set: { if !$0 { pauseConfirm = nil } }),
                            presenting: pauseConfirm) { c in
            Button("本堂完成后暂停") { Task { _ = await model.pause(c.queue, afterCurrent: true) } }
            Button("仍然立即暂停", role: .destructive) { Task { _ = await model.pause(c.queue, force: true) } }
            Button("取消", role: .cancel) {}
        } message: { c in Text(c.message) }
    }

    // MARK: scan

    private var scanBlock: some View {
        let ready = model.inventory?.ready ?? []
        let selectable = ready.filter { $0.selectable == true }
        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("新回放").font(.title2.weight(.semibold))
                    Group {
                        if model.scanning, let s = model.scanStarted {
                            TimelineView(.periodic(from: s, by: 1)) { ctx in
                                Text("正在登录校园并核对订阅课程的播放地址… \(Int(ctx.date.timeIntervalSince(s))) 秒")
                            }
                        } else if let t = model.inventory?.scannedAt {
                            Text("上次检查 \(relative(Date(timeIntervalSince1970: t)))")
                        } else {
                            Text("检查订阅课程是否有新的课堂回放")
                        }
                    }
                    .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button { Task { await model.scan() } } label: {
                    Label(model.scanning ? "检查中" : "检查新回放", systemImage: "arrow.clockwise")
                }
                .controlSize(.large)
                .disabled(model.scanning)
            }

            if model.scanning && model.inventory == nil {
                ProgressView().frame(maxWidth: .infinity).padding(30)
            } else if model.inventory != nil {
                if ready.isEmpty {
                    quietCard(icon: "checkmark.circle", text: "没有新的可识别回放。已有笔记、已删除、课表排除和本地已有任务的课次都会自动跳过。")
                } else {
                    VStack(spacing: 0) {
                        ForEach(Array(ready.enumerated()), id: \.element.id) { i, l in
                            if i > 0 { Divider().padding(.leading, 64) }
                            ReadyRow(lecture: l, checked: model.selection.contains(l.subId), started: model.inventory?.started == true) { on in
                                if on { model.selection.insert(l.subId) } else { model.selection.remove(l.subId) }
                            }
                        }
                    }
                    .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    if !selectable.isEmpty {
                        HStack {
                            Text("单个 bf16 模型 · 双块解码 · 完成后等你审核").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Button { confirmStart = true } label: {
                                HStack {
                                    if model.starting { ProgressView().controlSize(.small) }
                                    Text(model.starting ? "正在准备…" : "开始识别 \(model.selection.count) 堂")
                                }
                                .padding(.horizontal, 8)
                            }
                            .buttonStyle(.borderedProminent).controlSize(.large)
                            .disabled(model.selection.isEmpty || model.starting || model.status?.workerActive == true)
                        }
                        if model.status?.workerActive == true {
                            Label("已有识别在运行，一次只加载一个模型；完成后再开始。", systemImage: "info.circle").font(.caption).foregroundStyle(.orange)
                        }
                    }
                }
                otherLectures
            }
        }
    }

    @ViewBuilder private var otherLectures: some View {
        let deferred = model.inventory?.deferred ?? []
        let courses = model.inventory?.courses ?? []
        if !deferred.isEmpty || !courses.isEmpty {
            DisclosureGroup(isExpanded: $showOverview) {
                VStack(alignment: .leading, spacing: 14) {
                    if !deferred.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("暂无播放地址").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            ForEach(deferred) { l in
                                HStack {
                                    Text(shortDate(l.date)).monospacedDigit().frame(width: 64, alignment: .leading)
                                    Text(l.courseTitle)
                                    Text(sessionLabel(l.title, date: l.date)).foregroundStyle(.secondary)
                                    Spacer()
                                    Text("稍后再查").foregroundStyle(.tertiary)
                                }
                                .font(.callout)
                            }
                        }
                    }
                    ForEach(courses) { c in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(c.courseTitle).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            ForEach(c.lectures.prefix(4)) { l in
                                HStack {
                                    Text(l.date.map(shortDate) ?? "").monospacedDigit().frame(width: 64, alignment: .leading)
                                    Text(sessionLabel(l.title, date: l.date)).foregroundStyle(.secondary)
                                    Spacer()
                                    Text(state(l)).foregroundStyle(state(l) == "已有笔记" ? Color.green : Color.secondary)
                                }
                                .font(.callout)
                            }
                        }
                    }
                }
                .padding(.top, 10)
            } label: {
                Text("订阅课程概览").font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private func state(_ l: CourseLecture) -> String {
        if l.deleted == true { return "已删除" }
        if (model.inventory?.deferred ?? []).contains(where: { $0.subId == l.subId }) { return "暂无播放地址" }
        if l.hasSummary == true { return "已有笔记" }
        if l.localPending == true { return "本地处理中" }
        if l.localNotNeeded == true { return "无需处理" }
        if l.sessionSelected == false { return "课表排除" }
        if l.available == false { return "未开放" }
        return "可识别"
    }

    // MARK: queue

    @ViewBuilder private func queueBlock(_ q: QueueInfo) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 10) {
                        Text(q.coordinatorAlive ? "正在识别" : "最近一次识别").font(.title2.weight(.semibold))
                        StatusPill(status: q.isRunning ? "running" : q.status, label: q.pauseRequested ? "将暂停" : nil)
                    }
                    Text("启动于 \(shortTime(q.startedDate))\(q.appManaged ? "" : " · 旧流程启动，仅查看")")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                queueButtons(q)
            }
            if q.coordinatorAlive {
                VStack(alignment: .leading, spacing: 12) {
                    HardwareStrip(macmon: model.macmon)
                    trend
                }
            }
            ForEach(q.lectures) { lecture in
                LectureRunCard(lecture: lecture, isCurrent: lecture.subId == q.currentSubId && q.coordinatorAlive,
                               history: lecture.subId == q.currentSubId ? (model.status?.history ?? []) : [])
            }
        }
    }

    @ViewBuilder private func queueButtons(_ q: QueueInfo) -> some View {
        HStack(spacing: 8) {
            if model.busyAction != nil { ProgressView().controlSize(.small) }
            if q.appManaged && q.coordinatorAlive && !q.pauseRequested {
                Button {
                    Task {
                        if let r = await model.pause(q), r.needsConfirmation == true { pauseConfirm = (q, r.message ?? "") }
                    }
                } label: { Label("暂停", systemImage: "pause.fill") }
                .help("保存检查点后停止；之后可以从断点续跑")
            }
            if q.canResume {
                Button { Task { await model.resume(q, retryFailed: false) } } label: { Label("继续", systemImage: "play.fill") }
                    .buttonStyle(.borderedProminent)
            }
            if q.canRetryFailed {
                Button { Task { await model.resume(q, retryFailed: true) } } label: { Label("重试失败课次", systemImage: "arrow.counterclockwise") }
                    .disabled(model.status?.workerActive ?? false)
                    .help("复用已保存的识别块继续；不明确的模型请求仍由后端阻止重放")
            }
        }
        .controlSize(.large)
        .disabled(model.busyAction != nil)
    }

    private var trend: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("趋势").font(.headline)
                    Text("全机数据，含其他应用").font(.caption).foregroundStyle(.tertiary)
                    Spacer()
                    Picker("", selection: $range) { ForEach(TrendRange.allCases) { Text($0.label).tag($0) } }
                        .pickerStyle(.segmented).frame(width: 210)
                }
                let cutoff = Date().addingTimeInterval(-Double(range.rawValue) * 60)
                let pts = model.macmon.samples.filter { $0.time >= cutoff }
                if pts.count < 2 {
                    Text(model.macmon.error ?? "等待采样…").font(.callout).foregroundStyle(.secondary).frame(height: 150)
                } else {
                    HStack(spacing: 24) {
                        TrendChart(title: "GPU 活跃", unit: "%", points: pts, value: \.gpuPercent, color: .blue, domain: 0...100, start: cutoff)
                        TrendChart(title: "系统功耗", unit: "W", points: pts, value: \.systemWatts, color: .orange,
                                   secondary: \.gpuWatts, secondaryLabel: "GPU", start: cutoff)
                    }
                }
            }
        }
    }

    private func quietCard(icon: String, text: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(.secondary)
            Text(text).font(.callout).foregroundStyle(.secondary)
            Spacer()
        }
        .padding(16)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
