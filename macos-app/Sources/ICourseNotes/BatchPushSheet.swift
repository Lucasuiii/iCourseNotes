import SwiftUI

struct BatchPushSheet: View {
    @Environment(AppModel.self) private var model
    let items: [LocalNoteIndex]
    let close: () -> Void
    @State private var comparisons: [String: CompareResult] = [:]
    @State private var errors: Set<String> = []
    @State private var overwrites: Set<String> = []
    @State private var loading = true
    @State private var email = false

    private var requests: [BatchPushItem] {
        items.compactMap { item in
            guard let comparison = comparisons[item.control], comparison.remote?.deleted != true,
                  let sha = comparison.local?.summarySha256,
                  !comparison.conflict || overwrites.contains(item.control) else { return nil }
            return BatchPushItem(control: item.control, summarySha256: sha,
                                 overwriteRemoteSha256: comparison.conflict ? comparison.remote?.summarySha256 : nil)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("批量推送").font(.title2.weight(.semibold))
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(items) { item in
                        VStack(alignment: .leading, spacing: 6) {
                            Text("\(item.courseTitle) · \(shortDate(item.date ?? ""))").font(.headline)
                            if let result = comparisons[item.control] {
                                if result.remote?.deleted == true {
                                    Label("云端已删除，跳过这堂课", systemImage: "nosign").foregroundStyle(.red)
                                } else if result.conflict {
                                    Text("云端已有 \(result.remote?.summaryChars ?? 0) 字、\(result.remote?.figures ?? 0) 张图；本机 \(result.local?.summaryChars ?? 0) 字、\(result.local?.figures ?? 0) 张图")
                                        .font(.caption).foregroundStyle(.secondary)
                                    Toggle("用本机版本覆盖云端", isOn: Binding(get: { overwrites.contains(item.control) }, set: { value in
                                        if value { overwrites.insert(item.control) } else { overwrites.remove(item.control) }
                                    })).toggleStyle(.checkbox)
                                } else {
                                    Label("待推送 · \(result.local?.figures ?? 0) 张图", systemImage: "checkmark.circle").foregroundStyle(.secondary)
                                }
                            } else if errors.contains(item.control) {
                                Label("云端读取失败，请稍后重新打开", systemImage: "wifi.exclamationmark").foregroundStyle(.red)
                            } else {
                                HStack { ProgressView().controlSize(.small); Text("正在核对云端…").foregroundStyle(.secondary) }
                            }
                        }
                        Divider()
                    }
                }
            }.frame(maxHeight: 340)
            Text("未勾选覆盖的已有笔记会跳过。每堂推送后核对云端，成功后清理中间文件，保留摘要、转录和精选图。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("同时发送邮件", isOn: $email).toggleStyle(.switch)
            HStack {
                Spacer()
                Button("取消") { close() }.keyboardShortcut(.cancelAction)
                Button("推送 \(requests.count) 堂") {
                    let pending = requests, mail = email
                    Task { if await model.pushMany(pending, email: mail) { close() } }
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(loading || !errors.isEmpty || requests.isEmpty || model.startingPush || model.publicationBusy)
            }
        }
        .padding(24).frame(width: 560)
        .task {
            for item in items {
                if let result = await model.compare(control: item.control) { comparisons[item.control] = result }
                else { errors.insert(item.control) }
            }
            loading = false
        }
    }
}
