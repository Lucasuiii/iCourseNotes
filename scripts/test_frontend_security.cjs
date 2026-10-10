const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const createDOMPurify = require('dompurify');
const { marked } = require('marked');
const root = path.resolve(__dirname, '..');
const dom = new JSDOM('<main id="result"></main>', {
  url: 'https://frontend.invalid/', runScripts: 'outside-only',
});
const { window } = dom;
window.DOMPurify = createDOMPurify(window);
window.marked = marked;
for (const file of ['render.js', 'app.js']) {
  window.eval(fs.readFileSync(path.join(root, 'frontend/js', file), 'utf8'));
}
const target = window.document.getElementById('result');
const payloads = ['<img src=x onerror=alert(1)>', '<svg onload=alert(1)>',
  '<script>alert(1)</script>', '</mark><img src=x onerror=alert(1)>',
  '<a href="javascript:alert(1)">作业</a>', '&lt;img src=x onerror=alert(1)&gt;'];
for (const payload of payloads) {
  for (const query of ['alert', '不存在', payload, '<', '&']) {
    target.innerHTML = window._highlightSnippet(payload, query);
    for (const element of target.querySelectorAll('*')) {
      assert.equal(element.tagName, 'MARK', `${payload}: unexpected element`);
      assert.equal(element.attributes.length, 0);
    }
    assert.ok(target.textContent.includes(window.ICS.render.plainSnippet(payload, 99999)),
      'normalized snippet must stay literal readable text');
  }
}
target.innerHTML = window._highlightSnippet('矩阵 & A < B；矩阵 +', '矩阵');
assert.equal(target.querySelectorAll('mark').length, 2);
assert.equal(target.textContent, '矩阵 & A < B；矩阵 +');
for (const query of ['&', '<', '+']) {
  target.innerHTML = window._highlightSnippet('A & B < C + D', query);
  assert.equal(target.querySelector('mark').textContent, query);
}
window.DOMPurify = undefined;
target.innerHTML = window._highlightSnippet(payloads[0], 'alert');
assert.equal(target.childElementCount, 0);
assert.equal(target.textContent, payloads[0]);
window.DOMPurify = createDOMPurify(window);
for (const payload of payloads) {
  target.innerHTML = window.ICS.render.renderMarkdown(payload);
  assert.equal(target.querySelectorAll('script,svg,[onerror],[onload]').length, 0);
  assert.equal(target.querySelectorAll('[href^="javascript:"]').length, 0);
}
target.innerHTML = window.ICS.render.renderMarkdown('## 矩阵\n\n**结论**：$A < B$\n\n| 项 | 值 |\n| --- | --- |\n| 秩 | 2 |');
assert.equal(target.querySelector('h2').textContent, '矩阵');
assert.equal(target.querySelector('strong').textContent, '结论');
assert.ok(target.querySelector('table'));
assert.ok(target.textContent.includes('\\(A < B\\)'));
const revoked = [];
window.URL.createObjectURL = () => 'blob:https://frontend.invalid/test-figure';
window.URL.revokeObjectURL = url => revoked.push(url);
const figureId = 'a'.repeat(64);
const figure = {id: figureId, mime: 'image/jpeg', data: '/9j/2Q=='};
const figureMd = `## 事件\n\n![可见原图](#icourse-figure-${figureId})\n\n$P(A)$`;
target.innerHTML = window.ICS.render.renderMarkdown(figureMd, [figure]);
assert.equal(target.querySelector('img').getAttribute('loading'), 'lazy');
assert.equal(target.querySelector('img').getAttribute('src'), 'blob:https://frontend.invalid/test-figure');
assert.equal(target.querySelector('button').getAttribute('aria-label'), '查看大图：可见原图');
assert.equal(target.querySelectorAll('a').length, 0);
window.ICS.render.renderMarkdown('另一张PPT文字');
assert.equal(revoked.length, 0, 'hidden PPT rendering must not revoke visible summary pictures');
target.innerHTML = window.ICS.render.renderMarkdown(figureMd, []);
assert.equal(target.querySelectorAll('img').length, 0);
assert.ok(target.textContent.includes('课堂配图不可用'));
assert.equal(revoked.length, 1);
target.innerHTML = window.ICS.render.renderMarkdown(figureMd, [{...figure, data: 'aW52YWxpZA=='}]);
assert.equal(target.querySelectorAll('img').length, 0);
// Production dependencies use complete versions. Tailwind's Play CDN lacks
// CORS, so it cannot use anonymous SRI without blocking the stylesheet runtime.
const index = new JSDOM(fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8'));
const remote = [...index.window.document.querySelectorAll('script[src^="https:"],link[href^="https:"]')];
assert.equal(remote.length, 10);
for (const element of remote) {
  const url = element.getAttribute('src') || element.getAttribute('href');
  assert.match(url, /(?:@|\/)(\d+\.\d+\.\d+)(?:\/|$)/);
  if (!url.startsWith('https://cdn.tailwindcss.com/')) {
    assert.match(element.getAttribute('integrity'), /^sha384-[A-Za-z0-9+/]{64}$/);
    assert.equal(element.getAttribute('crossorigin'), 'anonymous');
  }
}
index.window.close();
// Aware timestamps remain stable across host/browser timezone choices.
assert.equal(new window.Date('2026-10-06T12:00:00+00:00').getTime(), 1791288000000);
assert.ok(Number.isFinite(new window.Date('2026-10-06T12:00:00').getTime()));
dom.window.close();
console.log('Frontend DOM security payloads and timestamp compatibility passed.');
