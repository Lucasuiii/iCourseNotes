import SwiftUI

extension Color {
    static func status(_ s: String?) -> Color {
        switch s ?? "" {
        case "running", "starting", "publishing": return .blue
        case "paused": return .orange
        case "failed", "publication_failed", "coordinator_failed", "interrupted", "finished_with_failures", "blocked_resource": return .red
        case "awaiting_content_review", "awaiting_review", "awaiting_publication": return .purple
        case "completed", "completed_editorial", "delivered", "published", "complete": return .green
        default: return .secondary
        }
    }
}

struct StatusPill: View {
    let status: String?
    var label: String? = nil
    var body: some View {
        let c = Color.status(status)
        HStack(spacing: 5) {
            Circle().fill(c).frame(width: 6, height: 6)
                .overlay {
                    if status == "running" {
                        Circle().stroke(c.opacity(0.5), lineWidth: 2).scaleEffect(1.9)
                            .phaseAnimator([0.3, 1]) { v, p in v.opacity(1.2 - p) }
                    }
                }
            Text(label ?? Labels.status(status)).font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 9).padding(.vertical, 4)
        .background(c.opacity(0.12), in: Capsule())
        .foregroundStyle(c == .secondary ? Color.secondary : c)
    }
}

struct Card<Content: View>: View {
    var padding: CGFloat = 16
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5))
    }
}

struct SectionHeader: View {
    let title: String
    var subtitle: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.headline)
            if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    var message: String? = nil
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 38, weight: .light)).foregroundStyle(.tertiary)
            Text(title).font(.title3.weight(.medium)).foregroundStyle(.secondary)
            if let message {
                Text(message).font(.callout).foregroundStyle(.tertiary).multilineTextAlignment(.center).frame(maxWidth: 380)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}

struct ToastView: View {
    let toast: Toast
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: toast.isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .foregroundStyle(toast.isError ? .orange : .green)
            Text(toast.text).font(.callout)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(.regularMaterial, in: Capsule())
        .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
        .padding(.bottom, 20)
    }
}

func relative(_ date: Date?) -> String {
    guard let date else { return "—" }
    let f = RelativeDateTimeFormatter()
    f.locale = Locale(identifier: "zh_CN")
    f.unitsStyle = .short
    return f.localizedString(for: date, relativeTo: Date())
}

func shortTime(_ date: Date?) -> String {
    guard let date else { return "—" }
    let f = DateFormatter()
    f.locale = Locale(identifier: "zh_CN")
    f.dateFormat = Calendar.current.isDateInToday(date) ? "今天 HH:mm" : "M月d日 HH:mm"
    return f.string(from: date)
}

func weekday(_ iso: String) -> String {
    let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
    guard let d = f.date(from: iso) else { return "" }
    return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][Calendar.current.component(.weekday, from: d) - 1]
}

func shortDate(_ iso: String) -> String {
    let parts = iso.split(separator: "-")
    guard parts.count == 3, let m = Int(parts[1]), let d = Int(parts[2]) else { return iso }
    return "\(m)月\(d)日"
}

/// "2026-10-30第2-5节" → "第2-5节"
func sessionLabel(_ title: String?, date: String?) -> String {
    guard var t = title else { return "" }
    if let date, t.hasPrefix(date) { t.removeFirst(date.count) }
    return t.trimmingCharacters(in: .whitespaces)
}
