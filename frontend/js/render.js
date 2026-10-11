/**
 * Markdown + KaTeX rendering pipeline.
 * Sets window.ICS.render global.
 *
 * Depends on CDN globals: marked, DOMPurify, renderMathInElement
 *
 * Pipeline: stash formulas → marked → restore formulas → DOMPurify → KaTeX
 * We stash formula delimiters before marked.parse() because characters like
 * * and _ inside $...$ LaTeX (e.g. D^*, P_n, \sum_{i=1}) would otherwise
 * be treated as markdown emphasis and break the formula structure.
 */

window.ICS = window.ICS || {};

var _FORMULA_PLACEHOLDER_PREFIX = "";
var _FORMULA_PLACEHOLDER_SUFFIX = "";

function _escapeHtmlText(text) {
  return String(text).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function _sanitizeHighlight(html, plainText) {
  if (!window.DOMPurify) return _escapeHtmlText(plainText);
  return DOMPurify.sanitize(html, { ALLOWED_TAGS: ["mark"], ALLOWED_ATTR: [] });
}

/** Replace $...$ and $$...$$ with placeholders so marked won't touch them. */
function _stashFormulas(mdText) {
  var formulas = [];
  function stash(replacement) {
    var key = _FORMULA_PLACEHOLDER_PREFIX + formulas.length + _FORMULA_PLACEHOLDER_SUFFIX;
    formulas.push(replacement);
    return key;
  }

  var text = mdText;

  // 1. Stash existing \(...\) and \[...\] (already-LaTeX, protect from double-processing)
  text = text.replace(/(\\\([\s\S]*?\\\))|(\\\[[\s\S]*?\\\])/g, function (m) {
    return stash(m);
  });

  // 2. Stash $$...$$ (must run before $ → to avoid consuming individual $ chars of $$)
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, function (_, f) {
    return stash("\\[" + f + "\\]");
  });

  // 3. Stash $...$ (inline math — non-greedy to pair nearest closing $)
  text = text.replace(/\$([\s\S]+?)\$/g, function (_, f) {
    return stash("\\(" + f + "\\)");
  });

  return { text: text, formulas: formulas };
}

/** Restore stashed formulas in the HTML output after marked.parse(). */
function _restoreFormulas(html, formulas) {
  for (var i = 0; i < formulas.length; i++) {
    html = html.split(_FORMULA_PLACEHOLDER_PREFIX + i + _FORMULA_PLACEHOLDER_SUFFIX).join(_escapeHtmlText(formulas[i]));
  }
  return html;
}

var _figureBlobUrls = new Map();
var _figureBoundElements = new WeakSet();
var _figureDialog = null;
function _bindFigurePreview(element) {
  if (_figureBoundElements.has(element)) return;
  _figureBoundElements.add(element);
  element.addEventListener('click', function (event) {
    var button = event.target.closest('[data-classroom-preview]');
    if (!button || !element.contains(button)) return;
    var source = button.querySelector('img');
    if (!source || !Array.from(_figureBlobUrls.values()).includes(source.getAttribute('src'))) return;
    if (_figureDialog) { _figureDialog.close(); _figureDialog.remove(); }
    var dialog = document.createElement('dialog'); dialog.className = 'summary-figure-modal';
    var close = document.createElement('button'); close.type = 'button'; close.textContent = '关闭';
    close.className = 'summary-figure-close'; close.addEventListener('click', () => dialog.close());
    var image = document.createElement('img'); image.src = source.src; image.alt = source.alt;
    dialog.append(close, image); document.body.appendChild(dialog); _figureDialog = dialog;
    dialog.addEventListener('close', function () { dialog.remove(); if (_figureDialog === dialog) _figureDialog = null; button.focus(); });
    dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
    dialog.showModal();
  });
}
function _releaseFigureUrls(keep) {
  for (var [id, url] of _figureBlobUrls) {
    if (!keep.has(id)) {
      if (_figureDialog && _figureDialog.querySelector('img').getAttribute('src') === url) _figureDialog.close();
      URL.revokeObjectURL(url); _figureBlobUrls.delete(id);
    }
  }
}

function _renderMarkdown(mdText, figures) {
  if (!mdText) return "";
  var stashed = _stashFormulas(mdText);
  var rawHtml = marked.parse(stashed.text, { breaks: true });
  var restored = _restoreFormulas(rawHtml, stashed.formulas);
  var html = DOMPurify.sanitize(restored, { USE_PROFILES: { html: true } });
  var wrapper = document.createElement('div'); wrapper.innerHTML = html;
  var accepted = new Map();
  if (Array.isArray(figures) && figures.length <= 6) {
    for (var figure of figures) {
      if (figure && /^[0-9a-f]{64}$/.test(figure.id) && figure.mime === 'image/jpeg' &&
          typeof figure.data === 'string' && figure.data.length <= 1048576 &&
          /^[A-Za-z0-9+/]+=*$/.test(figure.data)) accepted.set(figure.id, figure);
    }
  }
  var active = new Set();
  for (var img of wrapper.querySelectorAll('img')) {
    var match = /^#icourse-figure-([0-9a-f]{64})$/.exec(img.getAttribute('src') || '');
    if (!match) continue;
    var id = match[1], image = accepted.get(id);
    if (!image) { img.replaceWith(document.createTextNode('（课堂配图不可用）')); continue; }
    try {
      var url = _figureBlobUrls.get(id);
      if (!url) {
        var raw = atob(image.data), bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
        if (bytes[0] !== 255 || bytes[1] !== 216) throw new Error('Invalid JPEG');
        url = URL.createObjectURL(new Blob([bytes], {type: 'image/jpeg'}));
        _figureBlobUrls.set(id, url);
      }
      active.add(id); img.src = url; img.setAttribute('loading', 'lazy'); img.setAttribute('decoding', 'async');
      img.className = 'summary-classroom-figure';
      var link = document.createElement('button');
      link.type = 'button'; link.className = 'summary-figure-button'; link.setAttribute('data-classroom-preview', '');
      link.setAttribute('aria-label', '查看大图：' + img.alt); link.title = '查看课堂原图';
      img.replaceWith(link); link.appendChild(img);
    } catch (e) { img.replaceWith(document.createTextNode('（课堂配图不可用）')); }
  }
  if (Array.isArray(figures)) _releaseFigureUrls(active);
  return wrapper.innerHTML;
}

function _activateKaTeX(element) {
  _bindFigurePreview(element);
  if (typeof renderMathInElement !== "function") return;
  renderMathInElement(element, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      // NOTE: $...$ intentionally omitted from KaTeX — converted to \(...\) in _stashFormulas
    ],
    throwOnError: false,
  });
}

function _plainSnippet(mdText, maxLen) {
  maxLen = maxLen || 100;
  if (!mdText) return "";
  var text = mdText
    .replace(/\$\$.+?\$\$/gs, "...")
    .replace(/\\\[.+?\\\]/gs, "...")
    .replace(/\$[^$]+?\$/g, "...")
    .replace(/\\\(.+?\\\)/g, "...")
    .replace(/#{1,6}\s+/g, "")
    .replace(/\*{1,3}(.+?)\*{1,3}/g, "$1")
    .replace(/`{1,3}[^`]*`{1,3}/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[|:\-]+/g, " ")
    .replace(/\n+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
}

window.ICS.render = {
  renderMarkdown: _renderMarkdown,
  activateKaTeX: _activateKaTeX,
  plainSnippet: _plainSnippet,
  escapeHtmlText: _escapeHtmlText,
  sanitizeHighlight: _sanitizeHighlight,
};
