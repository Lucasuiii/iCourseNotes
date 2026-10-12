/* Local-only exports: no GitHub token, workflow dispatch, email or upload. */
window.ICS = window.ICS || {};
(function () {
  const utf8 = text => new TextEncoder().encode(text);
  const esc = text => ICS.render.escapeHtmlText(text);
  const filename = text => String(text || '课程笔记').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').replace(/^\.+/, '').slice(0, 100) || '课程笔记';
  function selected(lectures) {
    const rows = lectures.filter(x => !x.deleted_at && x.summary?.trim());
    if (!rows.length) throw new Error('请至少选择一节有摘要的课程');
    return rows;
  }
  function lectureHeading(lec) {
    const title = String(lec.sub_title || '课堂笔记'), date = String(lec.date || '');
    return date && !title.startsWith(date) ? date+' '+title : title;
  }
  function imageBytes(image) {
    if (!image || !/^[a-f0-9]{64}$/.test(image.id) || image.mime !== 'image/jpeg' ||
        typeof image.data !== 'string' || image.data.length > 1048576 || !/^[A-Za-z0-9+/]+=*$/.test(image.data)) throw new Error('课堂配图数据无效');
    const raw = atob(image.data), bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
    if (bytes[0] !== 255 || bytes[1] !== 216) throw new Error('课堂配图数据无效');
    return bytes;
  }
  function markdownEntries(course, lectures) {
    const lines = ['# '+(course?.title || '课程笔记'), ''], images = new Map();
    for (const lec of selected(lectures)) {
      const assets = new Map((lec.summary_figures || []).map(x => [x.id, x]));
      const summary = lec.summary.replace(/!\[([^\]\n]*)\]\(#icourse-figure-([a-f0-9]{64})\)/g, (_, alt, id) => {
        const asset = assets.get(id);
        if (!asset) throw new Error('部分课堂配图缺失，请重新加载课程后再导出');
        images.set(id, imageBytes(asset));
        return '!['+alt+'](figures/'+id+'.jpg)';
      });
      lines.push('## '+lectureHeading(lec), '', summary, '', '---', '');
    }
    const entries = [{name: filename(course?.title)+'.md', bytes: utf8(lines.join('\n'))}];
    for (const [id, bytes] of images) entries.push({name: 'figures/'+id+'.jpg', bytes});
    return entries;
  }
  function crc32(bytes) {
    let c = 0xffffffff;
    for (const byte of bytes) { c ^= byte; for (let k = 0; k < 8; k++) c = (c >>> 1) ^ ((c & 1) ? 0xedb88320 : 0); }
    return (c ^ 0xffffffff) >>> 0;
  }
  // Stored ZIP entries; UTF-8 names and actual CRCs, no dependency or ZIP64.
  function zip(entries) {
    if (entries.length > 65535) throw new Error('导出文件过多，请分批导出');
    const locals = [], central = []; let offset = 0;
    for (const entry of entries) {
      const name = utf8(entry.name), size = entry.bytes.length;
      if (size > 0xffffffff || name.length > 65535) throw new Error('导出文件过大');
      const crc = crc32(entry.bytes), header = new Uint8Array(30), h = new DataView(header.buffer);
      h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true);
      h.setUint16(12, 33, true); h.setUint32(14, crc, true); h.setUint32(18, size, true); h.setUint32(22, size, true); h.setUint16(26, name.length, true);
      const record = new Uint8Array(46), r = new DataView(record.buffer);
      r.setUint32(0, 0x02014b50, true); r.setUint16(4, 20, true); r.setUint16(6, 20, true); r.setUint16(8, 0x0800, true); r.setUint16(14, 33, true);
      r.setUint32(16, crc, true); r.setUint32(20, size, true); r.setUint32(24, size, true); r.setUint16(28, name.length, true); r.setUint32(42, offset, true);
      locals.push(header, name, entry.bytes); central.push(record, name); offset += 30 + name.length + size;
      if (offset > 0xffffffff) throw new Error('导出文件过大，请分批导出');
    }
    const centralSize = central.reduce((n, x) => n + x.length, 0), end = new Uint8Array(22), e = new DataView(end.buffer);
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, entries.length, true); e.setUint16(10, entries.length, true); e.setUint32(12, centralSize, true); e.setUint32(16, offset, true);
    return new Blob([...locals, ...central, end], {type: 'application/zip'});
  }
  function downloadMarkdown(course, lectures) {
    const entries = markdownEntries(course, lectures), packaged = entries.length > 1;
    const blob = packaged ? zip(entries) : new Blob([entries[0].bytes], {type: 'text/markdown;charset=utf-8'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = filename(course?.title)+(packaged ? '.zip' : '.md'); document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  const css = `@page{size:A4;margin:18mm}body{font:16px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#202c3a;max-width:850px;margin:40px auto;padding:0 24px}h1{font-size:28px}h2{font-size:23px;border-bottom:1px solid #ddd;padding-bottom:8px}h3{font-size:20px;margin-top:28px}h4{font-size:17px;margin-top:22px}p{margin:12px 0}img{max-width:100%;height:auto;display:block;margin:16px auto}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{border:1px solid #ddd;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px;border-radius:6px}blockquote{border-left:3px solid #cbd5e1;padding-left:14px;color:#526174}.katex-display{overflow-x:auto;overflow-y:hidden}article+article{break-before:page;margin-top:40px}h1,h2,h3,h4{break-after:avoid}img,pre,tr{break-inside:avoid}.print-tools{position:sticky;top:0;background:#fff;padding:12px 0;border-bottom:1px solid #ddd}button{font:inherit;cursor:pointer;padding:8px 16px;border:1px solid #bbb;border-radius:6px;background:#fff}@media print{.print-tools{display:none}body{max-width:none;margin:0;padding:0;font-size:11pt}.katex-display{overflow:visible}a{color:inherit;text-decoration:none}}`;
  function buildPrintHtml(course, lectures) {
    const title = course?.title || '课程笔记';
    const body = selected(lectures).map(lec => {
      // Validate every referenced image before opening a partial print document.
      markdownEntries(course, [lec]);
      const content = document.createElement('div');
      content.innerHTML = ICS.render.renderMarkdown(lec.summary, lec.summary_figures || [], {exportImages: true});
      ICS.render.activateKaTeX(content);
      return '<article><h2>'+esc(lectureHeading(lec))+'</h2>'+content.innerHTML+'</article>';
    }).join('');
    // Copy only the trusted, pinned KaTeX stylesheet already used by this page.
    const styles = Array.from(document.querySelectorAll('link[rel="stylesheet"]')).filter(x => /\/katex(?:\.min)?\.css(?:\?|$)/.test(x.href)).map(x => x.outerHTML).join('');
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title>'+styles+'<style>'+css+'</style></head><body><div class="print-tools"><button id="save-pdf" disabled>正在准备排版…</button></div><h1>'+esc(title)+'</h1>'+body+'</body></html>';
  }
  function openPrint(course, lectures) {
    const html = buildPrintHtml(course, lectures);
    const dialog = document.createElement('dialog'); dialog.className = 'summary-export-preview';
    dialog.setAttribute('aria-label', 'PDF 预览');
    const bar = document.createElement('div'); bar.className = 'summary-export-toolbar';
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '返回笔记';
    close.addEventListener('click', () => dialog.close()); bar.appendChild(close);
    const frame = document.createElement('iframe'); frame.title = '课程笔记 PDF 排版预览';
    frame.setAttribute('sandbox', 'allow-same-origin allow-modals');
    frame.addEventListener('load', async () => {
      const page = frame.contentWindow, button = page.document.getElementById('save-pdf');
      if (!button) return;
      const waitImages = Promise.all(Array.from(page.document.images).map(img => img.decode?.().catch(() => {}) || Promise.resolve()));
      await Promise.race([Promise.all([page.document.fonts?.ready || Promise.resolve(), waitImages]), new Promise(resolve => setTimeout(resolve, 15000))]);
      if (!dialog.isConnected) return;
      button.disabled = false; button.textContent = '保存为 PDF'; button.addEventListener('click', () => { page.focus(); page.print(); });
    });
    frame.srcdoc = html; dialog.append(bar, frame); document.body.appendChild(dialog);
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    dialog.showModal(); return dialog;
  }
  ICS.exports = {markdownEntries, zip, downloadMarkdown, buildPrintHtml, openPrint};
})();
