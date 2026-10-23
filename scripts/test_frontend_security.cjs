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
for (const [markdown, latex] of [
  ['$i<j$', '\\(i<j\\)'],
  ['$\\sum_{i<j}P(A_i\\cap A_j)$', '\\(\\sum_{i<j}P(A_i\\cap A_j)\\)'],
  ['$$x<y$$', '\\[x<y\\]'],
  ['\\(i<j\\)', '\\(i<j\\)'],
  ['\\[\\begin{matrix}a&b\\\\c&d\\end{matrix}\\]', '\\[\\begin{matrix}a&b\\\\c&d\\end{matrix}\\]'],
  ['$x&copy;>y$', '\\(x&copy;>y\\)'],
  ['$<img src=x onerror=alert(1)>$', '\\(<img src=x onerror=alert(1)>\\)'],
]) {
  target.innerHTML = window.ICS.render.renderMarkdown(markdown);
  assert.equal(target.textContent.trim(), latex, 'LaTeX must survive HTML parsing verbatim');
  assert.equal(target.querySelectorAll('img,script,svg,[onerror]').length, 0);
  let renderedInput;
  window.renderMathInElement = element => { renderedInput = element.textContent.trim(); };
  window.ICS.render.activateKaTeX(target);
  assert.equal(renderedInput, latex, 'KaTeX must receive the complete LaTeX text');
}
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
const bracketCaption = 'K&#91;x&#93; 与 C&#91;a,b&#93;';
target.innerHTML = window.ICS.render.renderMarkdown(
  `![${bracketCaption}](#icourse-figure-${figureId})\n\n*课堂原图：${bracketCaption}*`, [figure]);
assert.equal(target.querySelector('img').alt, 'K[x] 与 C[a,b]');
assert.equal(target.querySelector('button').getAttribute('aria-label'), '查看大图：K[x] 与 C[a,b]');
assert.ok(target.textContent.includes('课堂原图：K[x] 与 C[a,b]'));
target.innerHTML = window.ICS.render.renderMarkdown(
  `![&#91;说明&#93;(javascript:alert(1))](#icourse-figure-${figureId})\n\n*课堂原图：&#91;说明&#93;(javascript:alert(1))*`, [figure]);
assert.equal(target.querySelectorAll('img').length, 1);
assert.equal(target.querySelectorAll('a,script,[onerror]').length, 0);
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
async function checkSubscriptions() {
  let factory;
  window.Alpine = {data: (name, fn) => { assert.equal(name, 'app'); factory = fn; }};
  window.document.dispatchEvent(new window.Event('alpine:init'));
  const app = factory();
  app.repoOwner = 'Owner'; app.repoName = 'Repository';
  let metadataReads = 0;
  window.ICS.db = { getAllCoursesTerms: () => [], getMeta: () => { metadataReads++; return 'old-1,old-2'; } };
  for (const name of ['_go','_refreshSubscribedCache','_refreshSingleRunCache','_refreshDeptCache','rebuildSubsFiltered','_toast']) app[name] = () => {};
  window.sessionStorage.setItem('ics_creds', JSON.stringify({token:'synthetic-token'}));
  const writes = [];
  window.ICS.github = {setCourseIdsSecret: async (owner, repo, token, ids) => { writes.push([...ids]); return ids.join(','); }};
  app.openSubscriptions();
  assert.deepEqual(Array.from(app.subscribedIds), ['old-1','old-2']);
  assert.equal(metadataReads, 1);
  app.subscribedIds = [];
  await app.saveSubscriptions();
  app.openSubscriptions();
  assert.deepEqual(Array.from(app.subscribedIds), [], 'a successfully saved empty subscription is authoritative');
  assert.equal(metadataReads, 1, 'empty cached list must not reload stale database metadata');
  assert.deepEqual(writes, [[]]);
  app.repoName = 'Another';
  app.openSubscriptions();
  assert.deepEqual(Array.from(app.subscribedIds), ['old-1','old-2'], 'repository caches must not leak into another repository');
  app.repoName = 'repository'; app.repoOwner = 'owner';
  app.openSubscriptions();
  assert.deepEqual(Array.from(app.subscribedIds), [], 'repository identity is case insensitive');
  const key = Object.keys(window.sessionStorage).find(k => k.endsWith('owner%2Frepository'));
  assert.ok(key, 'the cache key must identify the repository');
  window.sessionStorage.setItem(key, JSON.stringify([{}]));
  app.openSubscriptions();
  assert.deepEqual(Array.from(app.subscribedIds), ['old-1','old-2'], 'invalid cache must fall back to metadata');
  window.confirm = () => true;
  app.clearAllData();
  assert.equal(Object.keys(window.sessionStorage).filter(k => k.startsWith('ics_lastSubscribed:')).length, 0);
}
checkSubscriptions().then(() => {
  dom.window.close();
  console.log('Frontend DOM security, subscription cache, and timestamp compatibility passed.');
}).catch(error => { console.error(error); process.exitCode = 1; dom.window.close(); });
