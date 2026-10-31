/* Single-page reader. Swift calls ICSApp.load(note) per lecture and
 * ICSApp.setScale(x) for the font size. Homework stays inside the article as a
 * callout; transcripts are folded at the end. */
(function () {
  var note = null, opts = {};
  var content = document.getElementById('content');
  var empty = document.getElementById('empty');
  var toc = document.getElementById('toc');
  var bar = document.getElementById('progress');
  var esc = function (s) { return window.ICS.render.escapeHtmlText(s == null ? '' : s); };

  function post(message) {
    try { window.webkit.messageHandlers.ics.postMessage(message); } catch (e) {}
  }

  var WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  function weekday(iso) { var d = new Date(iso + 'T12:00:00'); return isNaN(d) ? '' : WEEK[d.getDay()]; }
  function chineseDate(iso) { var p = String(iso || '').split('-'); return p.length === 3 ? Number(p[1]) + ' 月 ' + Number(p[2]) + ' 日' : iso; }

  function header() {
    var figs = (note.figures && note.figures.figures) || [];
    var session = (note.title || '').replace(note.date || '', '').trim();
    var meta = [esc(note.date) + ' ' + weekday(note.date)];
    if (figs.length) meta.push(figs.length + ' 张课堂配图');
    if (note.figures && note.figures.status && note.figures.status !== 'complete')
      meta.push('<span class="warn">配图' + (note.figures.status === 'failed' ? '未生成' : '状态 ' + esc(note.figures.status)) + '</span>');
    if (note.review && note.review.status === 'passed') meta.push('<span class="good">理论审核通过</span>');
    else if (note.review && note.review.status === 'revised') meta.push('<span class="warn">按审核意见修订一次</span>');
    if (note.coverage && note.coverage.missing_seconds) meta.push('<span class="warn">识别缺口 ' + note.coverage.missing_seconds + ' 秒</span>');
    meta.push(note.source === 'local' ? '本机结果' : '已发布');
    return '<header class="note-head"><div class="note-kicker">' + esc(note.course_title) + '</div>' +
      '<h1 class="note-title">' + chineseDate(note.date) + (session ? '　' + esc(session) : '') + '</h1>' +
      '<div class="note-meta">' + meta.map(function (m) { return '<span>' + m + '</span>'; }).join('') + '</div></header>';
  }

  /* Wrap "课程事项提醒" and "尚需确认的内容" sections (h3 + following siblings) in callouts. */
  function callouts(root) {
    Array.from(root.querySelectorAll(':scope > h3')).forEach(function (h) {
      var kind = /课程事项提醒/.test(h.textContent) ? 'callout' : /尚需确认/.test(h.textContent) ? 'callout pending' : null;
      if (!kind) return;
      if (/课程事项提醒/.test(h.textContent)) h.textContent = '作业与课程事项';
      var box = document.createElement('section'); box.className = kind;
      h.parentNode.insertBefore(box, h);
      var node = h;
      while (node && !(node !== h && node.tagName === 'H3')) { var next = node.nextSibling; box.appendChild(node); node = next; }
    });
  }

  function buildToc(root) {
    var heads = Array.from(root.querySelectorAll('h3'));
    if (heads.length < 3) { toc.hidden = true; return; }
    toc.innerHTML = '<div class="toc-title">目录</div>' + heads.map(function (h, i) {
      h.id = 'sec-' + i;
      var copy = h.cloneNode(true); copy.querySelectorAll('.katex-mathml').forEach(function (n) { n.remove(); });
      return '<a href="#sec-' + i + '">' + esc(copy.textContent.trim()) + '</a>';
    }).join('');
    toc.hidden = false;
  }

  function transcriptFold(text) {
    var paras = String(text || '').replace(/^# 课堂整理转录\s*/, '').split(/\n+/).filter(function (x) { return x.trim(); });
    if (!paras.length) return '';
    var chars = paras.join('').length;
    return '<details class="fold"><summary>课堂整理转录<span class="count">' + chars.toLocaleString() + ' 字</span></summary>' +
      '<div class="fold-body">' + paras.map(function (p) { return '<p>' + esc(p) + '</p>'; }).join('') + '</div></details>';
  }

  function clock(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
  }

  function asrFold(text) {
    if (!text) return '';
    var re = /^## 块 (\d+) · ([\d.]+)–([\d.]+) 秒\s*$/gm, blocks = [], m, last = null;
    while ((m = re.exec(text))) {
      if (last) last.text = text.slice(last.end, m.index).trim();
      last = { a: m[2], b: m[3], end: re.lastIndex }; blocks.push(last);
    }
    if (last) last.text = text.slice(last.end).trim();
    if (!blocks.length) return '';
    return '<details class="fold"><summary>本机识别原文（按时间分块）<span class="count">' + blocks.length + ' 块</span></summary><div class="fold-body">' +
      blocks.map(function (b) {
        return '<div class="asr-block"><div class="asr-time">' + clock(b.a) + '</div>' +
          (b.text ? '<p class="asr-text">' + esc(b.text) + '</p>' : '<p class="asr-text asr-empty">无语音</p>') + '</div>';
      }).join('') + '</div></details>';
  }

  function printTranscript(text) {
    var paras = String(text || '').replace(/^# 课堂整理转录\s*/, '').split(/\n+/).filter(function (x) { return x.trim(); });
    if (!paras.length) return '';
    return '<section class="print-transcript"><h2>课堂整理转录</h2>' + paras.map(function (p) { return '<p>' + esc(p) + '</p>'; }).join('') + '</section>';
  }

  function draw() {
    var figs = (note.figures && note.figures.figures) || [];
    var printing = !!opts.print;
    document.documentElement.classList.toggle('print', printing);
    var body = window.ICS.render.renderMarkdown(note.summary || '', figs, printing ? { exportImages: true } : undefined);
    var appendix = printing ? (opts.transcript ? printTranscript(note.transcript) : '')
                            : '<div class="appendix">' + transcriptFold(note.transcript) + asrFold(note.asr_transcript) + '</div>';
    content.innerHTML = header() + '<div class="prose" id="prose">' + body + '</div>' + appendix;
    var prose = document.getElementById('prose');
    callouts(prose);
    content.hidden = false; empty.hidden = true;
    window.ICS.render.activateKaTeX(prose);
    buildToc(prose);
    // Lazy figures and fonts change layout after load; stay at the top until the reader scrolls.
    pinTop = true;
    var top = function () { if (pinTop) window.scrollTo(0, 0); };
    top(); requestAnimationFrame(top); setTimeout(top, 120); setTimeout(top, 600);
    content.querySelectorAll('img').forEach(function (img) { img.addEventListener('load', top, { once: true }); });
    onScroll();
    var images = Array.from(content.querySelectorAll('img'));
    if (printing) {
      // Print only after every embedded figure has decoded.
      Promise.all(images.map(function (i) { return i.decode().catch(function () {}); })).then(function () {
        post({ type: 'printReady', katexErrors: content.querySelectorAll('.katex-error').length, figures: images.length });
      });
      return;
    }
    post({ type: 'rendered', katexErrors: content.querySelectorAll('.katex-error').length,
           katex: content.querySelectorAll('.katex').length, figures: content.querySelectorAll('.summary-classroom-figure').length,
           homework: !!content.querySelector('.callout:not(.pending)') });
  }

  function onScroll() {
    var max = document.documentElement.scrollHeight - innerHeight;
    bar.style.width = (max > 0 ? Math.min(100, scrollY / max * 100) : 0) + '%';
    var links = toc.querySelectorAll('a'), current = null;
    Array.from(document.querySelectorAll('#prose h3')).forEach(function (h) { if (h.getBoundingClientRect().top < 120) current = h.id; });
    links.forEach(function (a) { a.classList.toggle('active', a.getAttribute('href') === '#' + current); });
  }
  var pinTop = false;
  addEventListener('scroll', onScroll, { passive: true });
  ['wheel', 'keydown', 'mousedown', 'touchstart'].forEach(function (t) { addEventListener(t, function () { pinTop = false; }, { passive: true }); });
  history.scrollRestoration = 'manual';

  // Enhance the production renderer's preview dialog: caption, click-to-zoom.
  new MutationObserver(function (records) {
    records.forEach(function (r) {
      r.addedNodes.forEach(function (node) {
        if (!(node instanceof HTMLDialogElement) || !node.classList.contains('summary-figure-modal')) return;
        var img = node.querySelector('img');
        if (img.alt) { var cap = document.createElement('div'); cap.className = 'modal-caption'; cap.textContent = img.alt; node.appendChild(cap); }
        var hint = document.createElement('div'); hint.className = 'modal-hint'; hint.textContent = '点击图片查看原始尺寸 · Esc 关闭';
        node.appendChild(hint);
        img.addEventListener('click', function (e) { e.stopPropagation(); node.classList.toggle('zoomed'); });
      });
    });
  }).observe(document.body, { childList: true });

  window.ICSApp = {
    load: function (payload, options) { note = payload; opts = options || {}; draw(); },
    /* Page cut offsets (CSS px from the top of the document) for slices of at most `pageHeight`,
       placed at block boundaries so lines, formulas and figures are not split. */
    pageBreaks: function (pageHeight) {
      var total = Math.ceil(document.documentElement.scrollHeight);
      var sel = '.prose p, .prose li, .prose pre, .prose table, .prose .katex-display, .prose blockquote, ' +
                '.summary-figure-button, .callout, .note-head, .print-transcript p, .print-transcript h2, .prose hr';
      var cuts = [];
      document.querySelectorAll(sel).forEach(function (el) {
        var r = el.getBoundingClientRect();
        cuts.push(Math.ceil(r.bottom + scrollY) + 2);
      });
      // Headings stay with what follows: never cut directly below one.
      var heads = Array.from(document.querySelectorAll('.prose h2, .prose h3, .prose h4, .callout > h3')).map(function (h) {
        return Math.floor(h.getBoundingClientRect().top + scrollY) - 4;
      });
      cuts = cuts.concat(heads).filter(function (c) { return c > 0 && c < total; }).sort(function (a, b) { return a - b; });
      var out = [0], start = 0;
      while (total - start > pageHeight) {
        var limit = start + pageHeight, best = null;
        for (var i = 0; i < cuts.length; i++) {
          if (cuts[i] > start + pageHeight * 0.45 && cuts[i] <= limit) best = cuts[i];
        }
        start = best || limit;
        out.push(start);
      }
      out.push(total);
      return out;
    },
    clear: function () { note = null; content.hidden = true; toc.hidden = true; empty.hidden = false; },
    setScale: function (x) { document.documentElement.style.setProperty('--scale', x); onScroll(); },
  };
  post({ type: 'ready' });
})();
