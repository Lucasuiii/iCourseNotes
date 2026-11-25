import Foundation

/// Exports notes as portable Markdown folders with decoded classroom figures in `figures/`.
/// Preserves original Markdown and math delimiters without modifying formula syntax.
enum MarkdownExporter {
    struct Item {
        let row: NoteRow
        let payload: Data
    }

    struct ExportResult {
        let folder: URL
        let files: [URL]
        let figures: Int
    }

    enum Scope: String {
        case term, course, all
    }

    private static let figureRefRegex = try! NSRegularExpression(
        pattern: #"!\[([^\n\]]*)\]\(#icourse-figure-([0-9a-f]{64})\)"#
    )
    private static let hex64Regex = try! NSRegularExpression(pattern: #"^[0-9a-f]{64}$"#)

    static func sanitizePathComponent(_ raw: String) -> String {
        let replaced = raw
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let cleaned = replaced.drop(while: { $0 == "." })
        return cleaned.isEmpty ? "未命名" : String(cleaned)
    }

    static func baseName(_ row: NoteRow) -> String {
        let session = sessionLabel(row.title, date: row.date)
        let raw = "\(row.courseTitle) \(row.date)\(session.isEmpty ? "" : " " + session)"
        return sanitizePathComponent(raw)
    }

    private static func parsePayload(_ payload: Data) throws -> (summary: String, figures: [String: Data]) {
        guard let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else {
            throw BridgeError(message: "笔记数据无效")
        }
        let summary = obj["summary"] as? String ?? ""
        var assets: [String: Data] = [:]
        if let figuresObj = obj["figures"] as? [String: Any],
           let list = figuresObj["figures"] as? [[String: Any]] {
            for item in list {
                guard let id = item["id"] as? String,
                      let mime = item["mime"] as? String, mime == "image/jpeg",
                      let b64 = item["data"] as? String else { continue }
                let idRange = NSRange(id.startIndex..<id.endIndex, in: id)
                guard hex64Regex.firstMatch(in: id, range: idRange) != nil,
                      let raw = Data(base64Encoded: b64, options: .ignoreUnknownCharacters),
                      raw.count >= 2, raw[0] == 0xFF, raw[1] == 0xD8 else { continue }
                assets[id] = raw
            }
        }
        return (summary, assets)
    }

    private static func renderDocument(row: NoteRow, summary: String, assets: [String: Data]) -> (markdown: String, figures: [(id: String, data: Data)]) {
        let nsSummary = summary as NSString
        let matches = figureRefRegex.matches(in: summary, range: NSRange(location: 0, length: nsSummary.length))
        var referenced: [(id: String, data: Data)] = []
        var seen: Set<String> = []
        var rewritten = ""
        var cursor = 0

        for match in matches {
            let fullRange = match.range
            if fullRange.location > cursor {
                rewritten += nsSummary.substring(with: NSRange(location: cursor, length: fullRange.location - cursor))
            }
            let alt = nsSummary.substring(with: match.range(at: 1))
            let id = nsSummary.substring(with: match.range(at: 2))
            if let data = assets[id] {
                rewritten += "![\(alt)](figures/\(id).jpg)"
                if seen.insert(id).inserted {
                    referenced.append((id: id, data: data))
                }
            } else {
                rewritten += "（课堂配图不可用）"
            }
            cursor = fullRange.location + fullRange.length
        }
        if cursor < nsSummary.length {
            rewritten += nsSummary.substring(from: cursor)
        }

        let trimmedBody = rewritten.trimmingCharacters(in: .whitespacesAndNewlines)
        let markdown: String
        if trimmedBody.hasPrefix("# ") {
            markdown = trimmedBody + "\n"
        } else {
            let session = sessionLabel(row.title, date: row.date)
            let heading = "# \(row.courseTitle) · \(row.date)\(session.isEmpty ? "" : " \(session)")"
            let wd = weekday(row.date)
            let datePart = wd.isEmpty ? row.date : "\(row.date) \(wd)"
            let metaLine = "> 学期：\(row.termLabel) · 日期：\(datePart)"
            markdown = trimmedBody.isEmpty
                ? "\(heading)\n\n\(metaLine)\n"
                : "\(heading)\n\n\(metaLine)\n\n\(trimmedBody)\n"
        }
        return (markdown, referenced)
    }

    /// Exports one note into `<parentFolder>/<course date session>/` with optional `figures/`.
    static func exportSingle(_ row: NoteRow, payload: Data, into parentFolder: URL) throws -> ExportResult {
        let (summary, assets) = try parsePayload(payload)
        let rendered = renderDocument(row: row, summary: summary, assets: assets)
        let name = baseName(row)
        let noteFolder = parentFolder.appending(path: name, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: noteFolder, withIntermediateDirectories: true)

        let mdURL = noteFolder.appending(path: "\(name).md")
        try Data(rendered.markdown.utf8).write(to: mdURL, options: .atomic)

        if !rendered.figures.isEmpty {
            let figDir = noteFolder.appending(path: "figures", directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: figDir, withIntermediateDirectories: true)
            for fig in rendered.figures {
                try fig.data.write(to: figDir.appending(path: "\(fig.id).jpg"), options: .atomic)
            }
        }
        return ExportResult(folder: noteFolder, files: [mdURL], figures: rendered.figures.count)
    }

    /// Exports a batch of notes into `<parentFolder>/<title>/` with automatic subdirectories:
    /// - `.all`:    `<title>/<term>/<course>/<note>.md` + `<course>/figures/`
    /// - `.term`:   `<title>/<course>/<note>.md` + `<course>/figures/`
    /// - `.course`: `<title>/<note>.md` + `<title>/figures/`
    static func exportBatch(_ items: [Item], title: String, scope: Scope, into parentFolder: URL,
                            progress: (Int) -> Void) throws -> ExportResult {
        let fm = FileManager.default
        let exportRoot = parentFolder.appending(path: sanitizePathComponent(title), directoryHint: .isDirectory)
        try fm.createDirectory(at: exportRoot, withIntermediateDirectories: true)

        var writtenFiles: [URL] = []
        var usedPaths: Set<String> = []
        var writtenFigures: Set<URL> = []

        for (i, item) in items.enumerated() {
            progress(i)
            let (summary, assets) = try parsePayload(item.payload)
            let rendered = renderDocument(row: item.row, summary: summary, assets: assets)

            let targetDir: URL
            switch scope {
            case .all:
                targetDir = exportRoot
                    .appending(path: sanitizePathComponent(item.row.termLabel), directoryHint: .isDirectory)
                    .appending(path: sanitizePathComponent(item.row.courseTitle), directoryHint: .isDirectory)
            case .term:
                targetDir = exportRoot
                    .appending(path: sanitizePathComponent(item.row.courseTitle), directoryHint: .isDirectory)
            case .course:
                targetDir = exportRoot
            }
            try fm.createDirectory(at: targetDir, withIntermediateDirectories: true)

            var stem = baseName(item.row)
            var mdURL = targetDir.appending(path: "\(stem).md")
            if !usedPaths.insert(mdURL.path).inserted {
                stem = sanitizePathComponent("\(stem) (\(item.row.subId))")
                mdURL = targetDir.appending(path: "\(stem).md")
                usedPaths.insert(mdURL.path)
            }
            try Data(rendered.markdown.utf8).write(to: mdURL, options: .atomic)
            writtenFiles.append(mdURL)

            if !rendered.figures.isEmpty {
                let figDir = targetDir.appending(path: "figures", directoryHint: .isDirectory)
                try fm.createDirectory(at: figDir, withIntermediateDirectories: true)
                for fig in rendered.figures {
                    let figURL = figDir.appending(path: "\(fig.id).jpg")
                    if writtenFigures.insert(figURL).inserted {
                        try fig.data.write(to: figURL, options: .atomic)
                    }
                }
            }
        }
        progress(items.count)
        return ExportResult(folder: exportRoot, files: writtenFiles, figures: writtenFigures.count)
    }
}
