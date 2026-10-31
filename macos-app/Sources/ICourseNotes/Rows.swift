import SwiftUI

/// Quiet date stamp: large day number over a small weekday, no colored blocks.
struct DateBadge: View {
    let date: String
    var tint: Color = .secondary
    var body: some View {
        let parts = date.split(separator: "-")
        VStack(spacing: -1) {
            Text(parts.count == 3 ? "\(Int(parts[2]) ?? 0)" : "–")
                .font(.system(size: 19, weight: .medium, design: .rounded)).monospacedDigit()
            Text(weekday(date)).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
        }
        .frame(width: 34)
    }
}

struct ReadyRow: View {
    let lecture: InventoryLecture
    let checked: Bool
    let started: Bool
    let toggle: (Bool) -> Void

    var body: some View {
        let selectable = lecture.selectable == true
        HStack(spacing: 12) {
            DateBadge(date: lecture.date)
            VStack(alignment: .leading, spacing: 2) {
                Text(lecture.courseTitle).font(.body.weight(.medium))
                HStack(spacing: 6) {
                    Text("\(weekday(lecture.date)) \(sessionLabel(lecture.title, date: lecture.date))")
                    if lecture.localExisting == true {
                        Text("· 已有本地任务（\(Labels.status(lecture.localControls?.first?.status))），请在进度页续跑").foregroundStyle(.orange)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Toggle("", isOn: Binding(get: { checked && selectable }, set: toggle))
                .toggleStyle(.checkbox).labelsHidden().disabled(!selectable)
        }
        .padding(10)
        .contentShape(Rectangle())
        .onTapGesture { if selectable { toggle(!checked) } }
        .opacity(selectable || started ? 1 : 0.6)
    }
}
