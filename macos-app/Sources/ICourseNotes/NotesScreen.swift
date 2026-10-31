import SwiftUI

struct NoteMeta {
    var summarySha: String?
    var publishable = false
    var blockReason: String?
    var status: String?
    var approved = false
    var isLocal = false
    var control: String?

    init() {}
    init(_ data: Data) {
        guard let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        summarySha = o["summary_sha256"] as? String
        publishable = o["publishable"] as? Bool ?? false
        blockReason = o["publish_block_reason"] as? String
        status = o["status"] as? String
        approved = o["approved"] as? Bool ?? false
        isLocal = o["source"] as? String == "local"
        control = o["control"] as? String
    }
}

struct NotesScreen: View {
    @Environment(AppModel.self) private var model
    @AppStorage("readerScale") private var scale = 1.0
    @State private var query = ""
    @State private var payload: Data?
    @State private var meta = NoteMeta()
    @State private var loading = false
    @State private var loadError: String?
    @State private var report = RenderReport()
    @State private var showApproval = false
    @AppStorage("noteTerm") private var chosenTerm = ""
    @State private var collapsed: Set<String> = []

    var body: some View {
        @Bindable var model = model
        NavigationSplitView {
            list.navigationSplitViewColumnWidth(min: 200, ideal: 236, max: 300)
        } detail: {
            reader
        }
        .navigationTitle("")
        .searchable(text: $query, placement: .sidebar, prompt: "搜索课程或日期")
        .toolbar {
            ToolbarItemGroup {
                Menu {
                    Button("导出当前笔记…") { exportCurrent(transcript: false) }
                        .disabled(payload == nil)
                    Button("导出当前笔记（含整理转录）…") { exportCurrent(transcript: true) }
                        .disabled(payload == nil)
                    Divider()
                    Button("批量导出…") { model.showBatchExport = true }
                } label: { Label("导出 PDF", systemImage: "square.and.arrow.up") }
                .help("导出 PDF")
                .disabled(model.exporting != nil)
                Menu {
                    Picker("字号", selection: $scale) {
                        Text("小").tag(0.92)
                        Text("标准").tag(1.0)
                        Text("大").tag(1.1)
                        Text("特大").tag(1.22)
                    }
                    .pickerStyle(.inline)
                } label: { Label("字号", systemImage: "textformat.size") }
                .help("阅读字号")
            }
        }
        .sheet(isPresented: $model.showBatchExport) { BatchExportSheet() }
        .sheet(isPresented: $showApproval) {
            if let row = model.selectedNote, let control = row.control, let sha = meta.summarySha {
                ApprovalSheet(row: row, control: control, summarySha: sha, report: report) {
                    Task { await load(row); await model.loadNotes() }
                }
            }
        }
        .onChange(of: model.selectedNote) { _, row in
            if let row { Task { await load(row) } } else { payload = nil }
        }
        .task { if let row = model.selectedNote, payload == nil { await load(row) } }
    }

    // MARK: list

    private var filtered: [NoteRow] {
        model.noteRows.filter { r in
            (query.isEmpty || r.courseTitle.localizedCaseInsensitiveContains(query) || r.date.contains(query) || r.title.contains(query))
        }
    }

    /// Term labels newest first.
    private var termLabels: [String] {
        var seen: [String] = []
        for r in model.noteRows.sorted(by: { $0.term > $1.term }) where !seen.contains(r.termLabel) { seen.append(r.termLabel) }
        return seen
    }
    /// The chosen term, defaulting to the newest (current) one.
    private var activeTerm: String { termLabels.contains(chosenTerm) ? chosenTerm : (termLabels.first ?? "") }

    private var list: some View {
        @Bindable var model = model
        return List(selection: $model.selectedNote) {
            let rows = filtered
            let local = rows.filter(\.isLocal)
            if !local.isEmpty {
                Section("本机新结果") { ForEach(local) { NoteListRow(row: $0, showCourse: true).tag($0) } }
            }
            let inTerm = rows.filter { !$0.isLocal && (!query.isEmpty || $0.termLabel == activeTerm) }
            ForEach(terms(inTerm), id: \.0) { term, courses in
                Section(query.isEmpty ? "课程" : term) {
                    ForEach(courses, id: \.0) { course, items in
                        DisclosureGroup(isExpanded: expansion(term + course)) {
                            ForEach(items) { NoteListRow(row: $0, showCourse: false).tag($0) }
                        } label: {
                            HStack(spacing: 6) {
                                Circle().fill(courseTint(items[0].courseId)).frame(width: 6, height: 6)
                                Text(course).lineLimit(1)
                                Spacer()
                                Text("\(items.count)").font(.caption).foregroundStyle(.tertiary).monospacedDigit()
                            }
                        }
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .top, spacing: 0) {
            Menu {
                Picker("学期", selection: $chosenTerm) {
                    ForEach(termLabels, id: \.self) { t in
                        Text(t == termLabels.first ? "\(t)（本学期）" : t).tag(t)
                    }
                }
                .pickerStyle(.inline)
            } label: {
                HStack(spacing: 4) {
                    Text(activeTerm.isEmpty ? "学期" : activeTerm)
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .semibold))
                }
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
            .help("切换学期")
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 18).padding(.vertical, 6)
        }
        .overlay {
            if model.notes == nil || (model.syncing && model.noteRows.isEmpty) { ProgressView() }
            else if filtered.isEmpty { EmptyState(icon: "magnifyingglass", title: "没有匹配的笔记") }
        }
    }

    /// Terms newest first; inside, courses in order of their most recent lecture.
    private func terms(_ rows: [NoteRow]) -> [(String, [(String, [NoteRow])])] {
        var order: [String] = []; var byTerm: [String: [NoteRow]] = [:]
        for r in rows.sorted(by: { $0.term == $1.term ? $0.date > $1.date : $0.term > $1.term }) {
            if byTerm[r.termLabel] == nil { order.append(r.termLabel) }
            byTerm[r.termLabel, default: []].append(r)
        }
        return order.map { term in
            var courses: [(String, [NoteRow])] = []
            for r in byTerm[term]! {
                if let i = courses.firstIndex(where: { $0.0 == r.courseTitle }) { courses[i].1.append(r) } else { courses.append((r.courseTitle, [r])) }
            }
            return (term, courses)
        }
    }

    private func expansion(_ key: String) -> Binding<Bool> {
        Binding(get: { !collapsed.contains(key) }, set: { if $0 { collapsed.remove(key) } else { collapsed.insert(key) } })
    }

    // MARK: reader

    private var reader: some View {
        VStack(spacing: 0) {
            if meta.isLocal, payload != nil { reviewBanner }
            ZStack {
                Color(nsColor: .textBackgroundColor)
                NoteWebView(payload: payload, scale: scale, report: $report)
                if loading { ProgressView().controlSize(.large) }
                if let loadError {
                    EmptyState(icon: "exclamationmark.triangle", title: "无法读取笔记", message: loadError)
                        .background(Color(nsColor: .textBackgroundColor))
                }
            }
        }
    }

    private var reviewBanner: some View {
        HStack(spacing: 12) {
            Image(systemName: meta.approved ? "checkmark.seal.fill" : "eye")
                .font(.title3).foregroundStyle(meta.approved ? Color.green : Color.accentColor)
            VStack(alignment: .leading, spacing: 1) {
                Text(meta.approved ? "已审核通过，等待推送" : meta.publishable ? "本机新结果 · 读完后请审核" : (meta.blockReason ?? Labels.status(meta.status)))
                    .font(.callout.weight(.semibold))
                Text(meta.approved ? "在“同步”页推送到 GitHub data" : meta.publishable ? "审核只记录在本机，推送前不会离开这台电脑" : "")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if report.katexErrors > 0 {
                Label("\(report.katexErrors) 处公式无法渲染", systemImage: "exclamationmark.triangle.fill").font(.caption).foregroundStyle(.orange)
            }
            if meta.approved {
                Button("撤回审核") { Task { await model.unapprove(meta.control ?? ""); if let r = model.selectedNote { await load(r) } } }
                Button("去同步") { model.section = .sync }.buttonStyle(.borderedProminent)
            } else if meta.publishable {
                Button("审核通过…") { showApproval = true }.buttonStyle(.borderedProminent)
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 10)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }

    private func exportCurrent(transcript: Bool) {
        guard let row = model.selectedNote, let payload else { return }
        Task { await model.exportCurrent(row, payload: payload, transcript: transcript) }
    }

    private func load(_ row: NoteRow) async {
        loading = true; loadError = nil
        defer { loading = false }
        do {
            let data = try await model.fetchNote(row)
            guard model.selectedNote == row else { return }
            payload = data
            meta = NoteMeta(data)
        } catch {
            loadError = error.localizedDescription
            payload = nil; meta = NoteMeta()
        }
    }
}

struct NoteListRow: View {
    let row: NoteRow
    var showCourse = true
    var body: some View {
        if showCourse {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Circle().fill(courseTint(row.courseId)).frame(width: 5, height: 5)
                    Text(row.courseTitle).lineLimit(1)
                    Spacer(minLength: 0)
                    badge
                }
                Text("\(shortDate(row.date)) \(weekday(row.date)) · \(sessionLabel(row.title, date: row.date))")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    .padding(.leading, 11)
            }
            .padding(.vertical, 2)
        } else {
            HStack(spacing: 8) {
                Text(shortDate(row.date)).monospacedDigit()
                Text("\(weekday(row.date)) \(sessionLabel(row.title, date: row.date))").font(.caption).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                badge
            }
        }
    }

    @ViewBuilder private var badge: some View {
        if row.isLocal {
            Text(row.approved ? "待推送" : "待审核").font(.caption2).foregroundStyle(.secondary)
        }
    }
}

func courseTint(_ id: String) -> Color {
    let palette: [Color] = [
        Color(red: 0.36, green: 0.55, blue: 0.85), Color(red: 0.36, green: 0.68, blue: 0.62),
        Color(red: 0.84, green: 0.55, blue: 0.36), Color(red: 0.62, green: 0.50, blue: 0.82),
        Color(red: 0.80, green: 0.45, blue: 0.52), Color(red: 0.55, green: 0.66, blue: 0.40),
    ]
    return palette[(Int(id) ?? 0) % palette.count]
}

struct ApprovalSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let row: NoteRow
    let control: String
    let summarySha: String
    let report: RenderReport
    let done: () -> Void

    static let checklist = [
        "通读摘要，概念、公式与推导没有发现明显错误",
        "逐张看过配图，图注与图中内容相符",
        "作业与课程事项的题号、页码与转录或板书一致",
        "识别覆盖完整，转录没有明显缺段",
    ]
    @State private var checked: Set<Int> = []
    @State private var notes = ""
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("审核通过").font(.title2.weight(.semibold))
                Text("\(row.courseTitle) · \(shortDate(row.date))").foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(Self.checklist.enumerated()), id: \.offset) { i, text in
                    Toggle(text, isOn: Binding(get: { checked.contains(i) }, set: { if $0 { checked.insert(i) } else { checked.remove(i) } }))
                        .toggleStyle(.checkbox)
                }
            }
            TextField("备注（可选）", text: $notes, axis: .vertical).lineLimit(2...4).textFieldStyle(.roundedBorder)
            Text("审核绑定你刚读过的这份内容（\(summarySha.prefix(10))），并只保存在本机；推送在“同步”页单独进行。")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                if report.katex > 0 { Text("\(report.katex) 处公式 · \(report.katexErrors) 处错误 · \(report.figures) 张图").font(.caption).foregroundStyle(.tertiary) }
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("审核通过") {
                    working = true
                    Task {
                        let checks = checked.sorted().map { Self.checklist[$0] }
                        if await model.approve(control: control, summarySha: summarySha, checks: checks, notes: notes) { done(); dismiss() }
                        working = false
                    }
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(checked.count < Self.checklist.count || working)
            }
        }
        .padding(24)
        .frame(width: 460)
    }
}
