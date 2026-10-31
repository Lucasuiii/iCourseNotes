import AppKit
import PDFKit
import WebKit

/// Renders notes off screen with the reader page in print mode and saves
/// paginated A4 PDFs. Batch export can merge them with term/course/lecture bookmarks.
@MainActor
final class PDFExporter: NSObject, WKScriptMessageHandler {
    struct Item { let row: NoteRow; let payload: Data }

    private var window: NSWindow!
    private var web: WKWebView!
    private var readyContinuation: CheckedContinuation<Void, Never>?
    private var printContinuation: CheckedContinuation<Void, Never>?
    private var isReady = false

    override init() {
        super.init()
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "ics")
        config.websiteDataStore = .nonPersistent()
        // A4 width at 96 dpi minus margins, so screen layout matches the printed column.
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 680, height: 960), configuration: config)
        window = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = web
        window.appearance = NSAppearance(named: .aqua)
        window.setFrameOrigin(NSPoint(x: -10000, y: -10000))
        if let dir = Bundle.main.resourceURL?.appending(path: "web") {
            web.loadFileURL(dir.appending(path: "note.html"), allowingReadAccessTo: dir)
        }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        switch body["type"] as? String {
        case "ready":
            isReady = true; readyContinuation?.resume(); readyContinuation = nil
        case "printReady":
            printContinuation?.resume(); printContinuation = nil
        default: break
        }
    }

    private func waitReady() async {
        if isReady { return }
        await withCheckedContinuation { readyContinuation = $0 }
    }

    // A4 in points; the reader column is 680 CSS px wide and is scaled into the text block.
    private let page = CGSize(width: 595.2, height: 841.8)
    private let margin = (top: 44.0, bottom: 52.0, side: 42.0)

    /// Writes one note to `url` as an A4 PDF. The page is captured slice by slice with
    /// WebKit's vector PDF output; slices end at paragraph/formula/figure boundaries.
    func export(_ payload: Data, to url: URL, transcript: Bool) async throws {
        await waitReady()
        guard let json = String(data: payload, encoding: .utf8) else { throw BridgeError(message: "笔记数据无效") }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            printContinuation = c
            web.evaluateJavaScript("ICSApp.load(\(json), {print: true, transcript: \(transcript)})")
        }
        try? await Task.sleep(for: .milliseconds(120))
        let scale = (page.width - 2 * margin.side) / web.frame.width
        let sliceHeight = (page.height - margin.top - margin.bottom) / scale
        let raw = try await web.callAsyncJavaScript("return ICSApp.pageBreaks(h)", arguments: ["h": sliceHeight], contentWorld: .page)
        guard let cuts = (raw as? [NSNumber])?.map(\.doubleValue), cuts.count >= 2 else { throw BridgeError(message: "无法分页") }

        let out = NSMutableData()
        var box = CGRect(origin: .zero, size: page)
        guard let consumer = CGDataConsumer(data: out as CFMutableData),
              let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { throw BridgeError(message: "无法创建 PDF") }
        let footer = footerText(payload)
        let total = cuts.count - 1
        for i in 0..<total {
            let config = WKPDFConfiguration()
            config.rect = CGRect(x: 0, y: cuts[i], width: web.frame.width, height: max(1, cuts[i + 1] - cuts[i]))
            let data = try await web.pdf(configuration: config)
            guard let provider = CGDataProvider(data: data as CFData), let doc = CGPDFDocument(provider),
                  let slice = doc.page(at: 1) else { continue }
            let media = slice.getBoxRect(.mediaBox)
            let s = (page.width - 2 * margin.side) / media.width
            ctx.beginPDFPage(nil)
            ctx.saveGState()
            ctx.translateBy(x: margin.side, y: page.height - margin.top - media.height * s)
            ctx.scaleBy(x: s, y: s)
            ctx.drawPDFPage(slice)
            ctx.restoreGState()
            drawFooter(ctx, left: footer, right: "\(i + 1) / \(total)")
            ctx.endPDFPage()
        }
        ctx.closePDF()
        try (out as Data).write(to: url, options: .atomic)
    }

    private func footerText(_ payload: Data) -> String {
        guard let o = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else { return "" }
        return "\(o["course_title"] as? String ?? "") · \(o["date"] as? String ?? "")"
    }

    private func drawFooter(_ ctx: CGContext, left: String, right: String) {
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 7.5), .foregroundColor: NSColor(white: 0.55, alpha: 1)]
        (left as NSString).draw(at: NSPoint(x: margin.side, y: 26), withAttributes: attrs)
        let size = (right as NSString).size(withAttributes: attrs)
        (right as NSString).draw(at: NSPoint(x: page.width - margin.side - size.width, y: 26), withAttributes: attrs)
        NSGraphicsContext.restoreGraphicsState()
    }

    /// Exports many notes; `merged` produces one PDF with bookmarks, otherwise one file per lecture in `folder`.
    func exportBatch(_ items: [Item], folder: URL?, merged: URL?, transcript: Bool,
                     progress: (Int) -> Void) async throws -> [URL] {
        var written: [URL] = []
        let tmp = FileManager.default.temporaryDirectory.appending(path: "icoursenotes-export-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }
        for (i, item) in items.enumerated() {
            progress(i)
            let name = Self.fileName(item.row)
            let target = merged == nil ? folder!.appending(path: name) : tmp.appending(path: "\(i).pdf")
            try await export(item.payload, to: target, transcript: transcript)
            written.append(target)
        }
        progress(items.count)
        guard let merged else { return written }
        let doc = PDFDocument()
        let root = PDFOutline()
        var termNode: PDFOutline?, courseNode: PDFOutline?
        var lastTerm = "", lastCourse = ""
        for (item, file) in zip(items, written) {
            guard let part = PDFDocument(url: file), let firstPage = part.page(at: 0) else { continue }
            let start = doc.pageCount
            for p in 0..<part.pageCount { if let page = part.page(at: p) { doc.insert(page, at: doc.pageCount) } }
            _ = firstPage
            let dest = PDFDestination(page: doc.page(at: start)!, at: NSPoint(x: 0, y: 842))
            if item.row.termLabel != lastTerm {
                termNode = PDFOutline(); termNode!.label = item.row.termLabel; termNode!.destination = dest
                root.insertChild(termNode!, at: root.numberOfChildren); lastTerm = item.row.termLabel; lastCourse = ""
            }
            if item.row.courseTitle != lastCourse {
                courseNode = PDFOutline(); courseNode!.label = item.row.courseTitle; courseNode!.destination = dest
                termNode!.insertChild(courseNode!, at: termNode!.numberOfChildren); lastCourse = item.row.courseTitle
            }
            let leaf = PDFOutline()
            leaf.label = "\(shortDate(item.row.date)) \(sessionLabel(item.row.title, date: item.row.date))"
            leaf.destination = dest
            courseNode!.insertChild(leaf, at: courseNode!.numberOfChildren)
        }
        doc.outlineRoot = root
        doc.documentAttributes = [PDFDocumentAttribute.titleAttribute: merged.deletingPathExtension().lastPathComponent,
                                  PDFDocumentAttribute.creatorAttribute: "iCourseNotes"]
        guard doc.write(to: merged) else { throw BridgeError(message: "合并 PDF 写入失败") }
        return [merged]
    }

    static func fileName(_ row: NoteRow) -> String {
        let session = sessionLabel(row.title, date: row.date)
        let raw = "\(row.courseTitle) \(row.date)\(session.isEmpty ? "" : " " + session).pdf"
        return raw.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
    }
}
