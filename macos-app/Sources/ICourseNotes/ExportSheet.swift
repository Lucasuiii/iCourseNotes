import AppKit
import SwiftUI

extension AppModel {
    func exportCurrent(_ row: NoteRow, payload: Data, transcript: Bool) async {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.pdf]
        panel.nameFieldStringValue = PDFExporter.fileName(row)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        exporting = (0, 1)
        defer { exporting = nil }
        do {
            try await exporter.export(payload, to: url, transcript: transcript)
            show("已导出 \(url.lastPathComponent)")
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } catch { show(error.localizedDescription, error: true) }
    }

    func exportBatch(_ rows: [NoteRow], title: String, merged: Bool, transcript: Bool) async {
        var folder: URL?, mergedURL: URL?
        if merged {
            let panel = NSSavePanel()
            panel.allowedContentTypes = [.pdf]
            panel.nameFieldStringValue = "\(title).pdf"
            guard panel.runModal() == .OK, let url = panel.url else { return }
            mergedURL = url
        } else {
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
            panel.prompt = "导出到这里"
            guard panel.runModal() == .OK, let url = panel.url else { return }
            folder = url
        }
        exporting = (0, rows.count)
        defer { exporting = nil }
        do {
            var items: [PDFExporter.Item] = []
            for row in rows { items.append(.init(row: row, payload: try await fetchNote(row))) }
            let files = try await exporter.exportBatch(items, folder: folder, merged: mergedURL, transcript: transcript) { done in
                self.exporting = (done, rows.count)
            }
            show(merged ? "已合并导出 \(rows.count) 堂" : "已导出 \(files.count) 个 PDF")
            NSWorkspace.shared.activateFileViewerSelecting(merged ? files : [folder!])
        } catch { show(error.localizedDescription, error: true) }
    }
}

struct BatchExportSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var scope = "term"
    @State private var term = ""
    @State private var course = ""
    @State private var merged = true
    @State private var transcript = false

    private var published: [NoteRow] { model.noteRows.filter { !$0.isLocal } }
    private var termLabels: [String] {
        var seen: [String] = []
        for r in published.sorted(by: { $0.term > $1.term }) where !seen.contains(r.termLabel) { seen.append(r.termLabel) }
        return seen
    }
    private var courseTitles: [String] {
        var seen: [String] = []
        for r in published where (term.isEmpty || r.termLabel == term) && !seen.contains(r.courseTitle) { seen.append(r.courseTitle) }
        return seen
    }

    /// Chronological within each course, courses grouped by term, so bookmarks read naturally.
    private var selection: [NoteRow] {
        let rows = published.filter { r in
            switch scope {
            case "term": return r.termLabel == term
            case "course": return r.termLabel == term && r.courseTitle == course
            default: return true
            }
        }
        return rows.sorted { ($0.term, $0.courseTitle, $0.date) < ($1.term, $1.courseTitle, $1.date) }
            .sorted { $0.term > $1.term }
    }

    private var title: String {
        switch scope {
        case "term": return "\(term) 课程笔记"
        case "course": return "\(course) \(term)"
        default: return "全部课程笔记"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("批量导出 PDF").font(.title2.weight(.semibold))
            Form {
                Picker("范围", selection: $scope) {
                    Text("一个学期").tag("term")
                    Text("一门课程").tag("course")
                    Text("全部").tag("all")
                }
                .pickerStyle(.segmented)
                if scope != "all" {
                    Picker("学期", selection: $term) { ForEach(termLabels, id: \.self) { Text($0).tag($0) } }
                }
                if scope == "course" {
                    Picker("课程", selection: $course) { ForEach(courseTitles, id: \.self) { Text($0).tag($0) } }
                }
                Toggle("合并为一个 PDF（按学期、课程、课次生成书签）", isOn: $merged)
                Toggle("每堂附上整理转录", isOn: $transcript)
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            HStack {
                Text("共 \(selection.count) 堂 · A4 · 浅色排版").font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("导出…") {
                    let rows = selection, name = title, m = merged, t = transcript
                    dismiss()
                    Task { await model.exportBatch(rows, title: name, merged: m, transcript: t) }
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(selection.isEmpty)
            }
        }
        .padding(22)
        .frame(width: 500)
        .onAppear {
            term = model.selectedNote?.termLabel ?? termLabels.first ?? ""
            course = model.selectedNote?.courseTitle ?? courseTitles.first ?? ""
        }
        .onChange(of: term) { _, _ in if !courseTitles.contains(course) { course = courseTitles.first ?? "" } }
    }
}

struct ExportProgress: View {
    let done: Int
    let total: Int
    var body: some View {
        HStack(spacing: 10) {
            ProgressView(value: Double(done), total: Double(max(total, 1))).frame(width: 120)
            Text(total > 1 ? "正在导出 \(min(done + 1, total)) / \(total)" : "正在导出 PDF").font(.callout)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(.regularMaterial, in: Capsule())
        .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
        .padding(.bottom, 20)
    }
}
