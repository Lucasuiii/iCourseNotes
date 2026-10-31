import SwiftUI
import WebKit

struct RenderReport: Equatable { var katex = 0; var katexErrors = 0; var figures = 0 }

/// Renders notes with the production render.js pipeline (marked → DOMPurify → KaTeX)
/// from vendored, offline copies. Nothing is loaded from the network.
struct NoteWebView: NSViewRepresentable {
    let payload: Data?
    let scale: Double
    @Binding var report: RenderReport

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(context.coordinator, name: "ics")
        config.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.setValue(false, forKey: "drawsBackground")
        if let dir = Bundle.main.resourceURL?.appending(path: "web") {
            web.loadFileURL(dir.appending(path: "note.html"), allowingReadAccessTo: dir)
        }
        context.coordinator.web = web
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.apply()
    }

    final class Coordinator: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
        var parent: NoteWebView
        weak var web: WKWebView?
        var ready = false
        var shownPayload: Data?
        var shownScale: Double?

        init(_ parent: NoteWebView) { self.parent = parent }

        func apply() {
            guard ready, let web else { return }
            if parent.scale != shownScale {
                shownScale = parent.scale
                web.evaluateJavaScript("ICSApp.setScale(\(parent.scale))")
            }
            if parent.payload != shownPayload {
                shownPayload = parent.payload
                if let data = parent.payload, let json = String(data: data, encoding: .utf8) {
                    web.evaluateJavaScript("ICSApp.load(\(json))")
                } else {
                    web.evaluateJavaScript("ICSApp.clear()")
                }
            }
        }

        func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any] else { return }
            switch body["type"] as? String {
            case "ready":
                ready = true; shownPayload = nil; shownScale = nil; apply()
            case "rendered":
                let r = RenderReport(katex: body["katex"] as? Int ?? 0, katexErrors: body["katexErrors"] as? Int ?? 0,
                                     figures: body["figures"] as? Int ?? 0)
                DispatchQueue.main.async { self.parent.report = r }
            default: break
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                if url.fragment != nil && url.path == webView.url?.path { decisionHandler(.allow); return }
                if url.scheme == "https" { NSWorkspace.shared.open(url) }
                decisionHandler(.cancel); return
            }
            decisionHandler(.allow)
        }
    }
}
