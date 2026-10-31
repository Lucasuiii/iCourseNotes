// Draws the app icon (waveform over a note card) into an .iconset folder.
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func draw(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let inset = s * 0.09
    let body = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let shape = NSBezierPath(roundedRect: body, xRadius: s * 0.2, yRadius: s * 0.2)
    NSGraphicsContext.current?.saveGraphicsState()
    let shadow = NSShadow(); shadow.shadowBlurRadius = s * 0.03; shadow.shadowOffset = NSSize(width: 0, height: -s * 0.012)
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.3); shadow.set()
    NSGradient(colors: [NSColor(red: 0.20, green: 0.42, blue: 0.98, alpha: 1), NSColor(red: 0.36, green: 0.22, blue: 0.86, alpha: 1)])!
        .draw(in: shape, angle: -60)
    NSGraphicsContext.current?.restoreGraphicsState()
    // note card
    let card = NSRect(x: s * 0.27, y: s * 0.22, width: s * 0.46, height: s * 0.56)
    NSColor.white.withAlphaComponent(0.95).setFill()
    NSBezierPath(roundedRect: card, xRadius: s * 0.05, yRadius: s * 0.05).fill()
    // waveform bars
    let heights: [CGFloat] = [0.25, 0.55, 0.85, 0.45, 1.0, 0.6, 0.3]
    let barW = s * 0.034, gap = s * 0.022
    let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
    var x = card.midX - total / 2
    for h in heights {
        let bh = s * 0.2 * h
        let r = NSRect(x: x, y: card.maxY - s * 0.17 - bh / 2, width: barW, height: bh)
        NSColor(red: 0.27, green: 0.33, blue: 0.93, alpha: 1).setFill()
        NSBezierPath(roundedRect: r, xRadius: barW / 2, yRadius: barW / 2).fill()
        x += barW + gap
    }
    // text lines
    for (i, w) in [0.32, 0.26, 0.3].enumerated() {
        let r = NSRect(x: card.minX + s * 0.07, y: card.minY + s * 0.07 + CGFloat(i) * s * 0.065, width: s * CGFloat(w), height: s * 0.022)
        NSColor(white: 0.75, alpha: 1).setFill()
        NSBezierPath(roundedRect: r, xRadius: s * 0.011, yRadius: s * 0.011).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for size in [16, 32, 128, 256, 512] {
    try! draw(size).write(to: out.appendingPathComponent("icon_\(size)x\(size).png"))
    try! draw(size * 2).write(to: out.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
