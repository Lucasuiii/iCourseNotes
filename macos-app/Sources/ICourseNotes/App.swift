import SwiftUI

@main
struct ICourseLocalApp: App {
    @State private var model = AppModel()
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    var body: some Scene {
        Window("iCourseNotes", id: "main") {
            RootView()
                .environment(model)
                .frame(minWidth: 1040, minHeight: 680)
                .onAppear { model.startup() }
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("识别") {
                Button("检查新回放") { model.section = .recognize; Task { await model.scan() } }.keyboardShortcut("r", modifiers: [.command, .shift])
                Button("拉取 data 笔记") { Task { await model.syncNotes() } }.keyboardShortcut("s", modifiers: [.command, .shift])
                Divider()
                Button("批量导出 PDF…") { model.section = .notes; model.showBatchExport = true }.keyboardShortcut("e", modifiers: [.command, .shift])
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Group {
            if model.bridge == nil {
                ProjectPicker()
            } else {
                switch model.section {
                case .recognize: RecognizeScreen()
                case .notes: NotesScreen()
                case .sync: SyncScreen()
                }
            }
        }
        .toolbar {
            // Sections live in the toolbar instead of a sidebar, so reading keeps the full width.
            ToolbarItem(placement: .principal) {
                Picker("", selection: $model.section) {
                    ForEach(AppSection.allCases) { s in
                        Text(title(for: s)).tag(s)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 240)
            }
            ToolbarItem(placement: .primaryAction) {
                if model.currentQueue?.coordinatorAlive == true { RunningChip() }
            }
        }
        .overlay(alignment: .bottom) {
            if let e = model.exporting {
                ExportProgress(done: e.done, total: e.total)
            } else if let t = model.toast {
                ToastView(toast: t).transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.spring(duration: 0.35), value: model.toast)
    }

    private func title(for s: AppSection) -> String {
        let n: Int
        switch s {
        case .recognize: n = model.inventory?.ready?.filter { $0.selectable == true }.count ?? 0
        case .notes: n = model.notes?.local?.filter { $0.status == "awaiting_content_review" && $0.approved != true }.count ?? 0
        case .sync: n = model.notes?.local?.filter { $0.approved == true && $0.publicationStatus == nil }.count ?? 0
        }
        return n > 0 ? "\(s.title) \(n)" : s.title
    }
}

/// Compact toolbar status while a queue is recognising; click to open 识别.
struct RunningChip: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        Button { model.section = .recognize } label: {
            HStack(spacing: 6) {
                Circle().fill(.blue).frame(width: 6, height: 6)
                if let l = model.activeLecture {
                    Text("\(l.courseTitle ?? "") · \(Labels.stage(l.stage ?? l.phase))")
                    if let f = l.fraction { Text(String(format: "%.0f%%", f * 100)).monospacedDigit().foregroundStyle(.secondary) }
                } else {
                    Text("识别中")
                }
                if let s = model.macmon.latest {
                    Text(String(format: "GPU %.0f%%", s.gpuPercent)).monospacedDigit().foregroundStyle(.secondary)
                }
            }
            .font(.caption)
        }
        .help("识别进行中，点击查看进度")
    }
}

struct ProjectPicker: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "folder.badge.questionmark").font(.system(size: 44, weight: .light)).foregroundStyle(.secondary)
            Text("找不到 iCourse 项目目录").font(.title2.weight(.semibold))
            Text("请选择包含 work/local-runtime 的项目根目录。").foregroundStyle(.secondary)
            Button("选择目录…") {
                let panel = NSOpenPanel()
                panel.canChooseDirectories = true; panel.canChooseFiles = false
                if panel.runModal() == .OK, let url = panel.url { model.setProject(url) }
            }
            .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
