/* Local PDF generation. Only pinned local libraries load; notes are never uploaded. */
window.ICS = window.ICS || {};
(function () {
  const width = 680, pageHeight = 1008;
  const safeName = text => String(text).replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').replace(/^\.+/, '').slice(0, 130);
  let libraries;
  async function bounded(promise, message) {
    let timer;
    try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), 60000); })]); }
    finally { clearTimeout(timer); }
  }
  function loadLibraries() {
    if (!libraries) libraries = Promise.all(['html2canvas-1.4.1.min.js', 'jspdf-4.2.1.umd.min.js'].map(name => new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = new URL('js/vendor/' + name, document.baseURI).href;
      script.onload = resolve; script.onerror = () => { script.remove(); reject(new Error('PDF 组件加载失败，请刷新后重试')); };
      document.head.appendChild(script);
    }))).catch(error => { libraries = null; throw error; });
    return libraries;
  }
  function parts(body) {
    const nodes = Array.from(body.children).flatMap(node => node.tagName === 'ARTICLE' ? Array.from(node.children) : [node]);
    return nodes.flatMap(node => {
      if (!['OL', 'UL'].includes(node.tagName)) return [node];
      return Array.from(node.children).map((item, index) => {
        const list = node.cloneNode(false);
        if (node.tagName === 'OL') list.start = (Number(node.getAttribute('start')) || 1) + index;
        list.appendChild(item); return list;
      });
    });
  }
  // Each canvas is bounded to a single page, avoiding blank oversized canvases.
  function paginate(body) {
    const blocks = parts(body), pages = []; body.replaceChildren();
    let page;
    const newPage = () => { page = body.ownerDocument.createElement('div'); page.className = 'pdf-download-page'; body.appendChild(page); pages.push(page); };
    newPage();
    for (let i = 0; i < blocks.length; i++) {
      const block = body.ownerDocument.createElement('div'); block.style.display = 'flow-root'; block.appendChild(blocks[i]);
      // Keep a heading with the following block when they fit on one page.
      if (/^H[1-4]$/.test(blocks[i].tagName) && blocks[i + 1]) block.appendChild(blocks[++i]);
      page.appendChild(block);
      if (page.scrollHeight > pageHeight && page.children.length > 1) { block.remove(); newPage(); page.appendChild(block); }
      if (page.scrollHeight > pageHeight) {
        // Oversized groups can separate; a single giant block cannot be safely clipped.
        if (block.children.length > 1) {
          const last = block.lastElementChild; last.remove(); blocks.splice(i + 1, 0, last);
        }
        if (page.scrollHeight > pageHeight) throw new Error('单段内容超过一页，无法安全生成独立 PDF；请使用合并打印');
      }
    }
    return pages;
  }
  function canvasHasContent(canvas) {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let dark = 0;
    for (let i = 0; i < data.length; i += 16) if (data[i + 3] && Math.min(data[i], data[i + 1], data[i + 2]) < 230 && ++dark > 40) return true;
    return false;
  }
  async function renderPdf(html, title) {
    // Render already-sanitized export HTML in the trusted parent document.
    // Safari can suppress the renderer's cloned-window load callbacks when
    // their source is a script-disabled iframe. The print preview stays sandboxed.
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const surface = document.createElement('div'); surface.className = 'pdf-download-document';
    surface.setAttribute('aria-hidden', 'true'); surface.innerHTML = parsed.body.innerHTML;
    surface.style.cssText = 'position:fixed;left:0;top:0;width:680px;z-index:-1;pointer-events:none;background:white';
    const style = document.createElement('style');
    style.textContent = `.pdf-download-document{font:16px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#202c3a}
.pdf-download-document .pdf-download-page{display:flow-root;width:680px;background:white}
.pdf-download-document h1{font-size:28px;font-weight:bold;margin:18px 0}.pdf-download-document h2{font-size:23px;font-weight:bold;border-bottom:1px solid #ddd;padding-bottom:8px;margin:18px 0}
.pdf-download-document h3{font-size:20px;font-weight:bold;margin:28px 0 16px}.pdf-download-document h4{font-size:17px;font-weight:bold;margin:22px 0 12px}
.pdf-download-document p{margin:12px 0}.pdf-download-document img{max-width:100%;max-height:720px;height:auto;object-fit:contain;display:block;margin:16px auto}
.pdf-download-document table{border-collapse:collapse;width:100%;margin:16px 0}.pdf-download-document td,.pdf-download-document th{border:1px solid #ddd;padding:8px;text-align:left}
.pdf-download-document pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px;border-radius:6px}
.pdf-download-document blockquote{border-left:3px solid #cbd5e1;padding-left:14px;color:#526174}.pdf-download-document .katex-display{overflow:visible!important}
.pdf-download-document ul{list-style:disc;padding-left:24px}.pdf-download-document ol{list-style:decimal;padding-left:24px}`;
    document.head.appendChild(style); document.body.appendChild(surface);
    try {
      await bounded(Promise.all([document.fonts.ready, ...Array.from(surface.querySelectorAll('img')).map(image => image.decode())]), 'PDF 字体或图片加载超时，已停止下载');
      const pages = paginate(surface);
      const pdf = new window.jspdf.jsPDF({unit: 'mm', format: 'a4', compress: true}); pdf.setProperties({title});
      for (let i = 0; i < pages.length; i++) {
        if (pages[i].scrollWidth > width + 1) throw new Error('公式或表格超出页面宽度，已停止下载；请使用合并打印');
        const canvas = await bounded(window.html2canvas(pages[i], {width, height: pageHeight, scale: 2, backgroundColor: '#fff', logging: false, useCORS: false, allowTaint: false, windowWidth: 700, windowHeight: 1100,
          ignoreElements: element => element.tagName === 'SCRIPT' || (element.parentElement === document.body && element !== surface)}), 'PDF 页面生成超时，已停止下载');
        try {
          if (!canvasHasContent(canvas)) throw new Error('PDF 页面为空，已停止下载；请使用合并打印');
          if (i) pdf.addPage();
          pdf.addImage(canvas.toDataURL('image/jpeg', .96), 'JPEG', 15, 15, 180, pageHeight * 180 / width);
        } finally { canvas.width = 0; canvas.height = 0; }
      }
      const bytes = new Uint8Array(pdf.output('arraybuffer'));
      if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('PDF 生成失败，已停止下载');
      return new Blob([bytes], {type: 'application/pdf'});
    } finally { surface.remove(); style.remove(); }
  }
  async function showDownloads(files) {
    const dialog = document.createElement('dialog'); dialog.className = 'pdf-download-results'; dialog.setAttribute('aria-label', '独立 PDF 下载');
    const heading = document.createElement('h2'); heading.textContent = '已生成 ' + files.length + ' 个独立 PDF';
    const help = document.createElement('p'); help.textContent = '已请求分别下载。如果浏览器拦截多个下载，请允许下载，或点击下方单个文件。';
    const links = files.map(file => { const link = document.createElement('a'); link.href = URL.createObjectURL(file.blob); link.download = file.name; link.textContent = file.name; dialog.appendChild(link); return link; });
    dialog.prepend(heading, help);
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '完成'; close.addEventListener('click', () => dialog.close()); dialog.appendChild(close);
    dialog.addEventListener('close', () => { setTimeout(() => links.forEach(link => URL.revokeObjectURL(link.href)), 60000); dialog.remove(); }, {once: true});
    document.body.appendChild(dialog); dialog.showModal();
    // Safari can replace a pending blob download when clicks happen in the
    // same task. Give each independent download time to start.
    for (const link of links) { link.click(); await new Promise(resolve => setTimeout(resolve, 750)); }
    return dialog;
  }
  async function download(course, lectures, progress = () => {}) {
    // Render/validate all source records before generation or download starts.
    const documents = lectures.map((lecture, index) => ({html: ICS.exports.buildPrintHtml(course, [lecture]),
      name: safeName(course?.title || '课程笔记') + ' - ' + safeName(lecture.sub_title || lecture.date || '课堂笔记') + ' - ' + safeName(lecture.sub_id) + ' - ' + (index + 1) + '.pdf'}));
    if (!documents.length) throw new Error('请至少选择一节有摘要的课程');
    progress('正在准备 PDF…'); await bounded(loadLibraries(), 'PDF 组件加载超时，请刷新后重试');
    const files = [];
    for (let i = 0; i < documents.length; i++) {
      progress('正在生成 ' + (i + 1) + '/' + documents.length);
      files.push({name: documents[i].name, blob: await renderPdf(documents[i].html, documents[i].name)});
    }
    return showDownloads(files);
  }
  ICS.pdfDownloads = {download, paginate, canvasHasContent};
})();
