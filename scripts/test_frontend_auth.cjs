const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const markup = new JSDOM(fs.readFileSync(path.join(root,'frontend/index.html'),'utf8'));
const unlockForm = markup.window.document.getElementById('unlock-form');
const username = unlockForm.elements.namedItem('username');
const password = unlockForm.elements.namedItem('password');
assert.equal(username.autocomplete, 'username');
assert.equal(username.readOnly, false);
assert.equal(username.value, 'iCourseNotes');
assert.equal(username.hasAttribute('aria-hidden'), false);
assert.equal(password.type, 'password');
assert.equal(password.autocomplete, 'current-password');
assert.equal(unlockForm.querySelectorAll('input[type="password"]').length, 1,
  'Management PAT must not compete with the database key for password AutoFill');
assert.ok(!unlockForm.contains(markup.window.document.querySelector('[name="management-token"]')));
assert.ok(markup.window.document.body.textContent.includes('输入关键词，搜索已有课程笔记'));
markup.window.close();
const dom = new JSDOM('<body></body>', {url:'https://example.github.io/notes/',runScripts:'outside-only'});
const w = dom.window;
for (const name of ['github.js','app.js']) w.eval(fs.readFileSync(path.join(root,'frontend/js',name),'utf8'));

(async () => {
  const requests=[];
  w.fetch=async (url,options) => {
    requests.push({url,options});
    let json;
    if(url.includes('/git/ref/')) json={object:{sha:'commit'}};
    else if(url.includes('/git/commits/')) json={tree:{sha:'tree'}};
    else if(url.includes('/git/trees/')) json={tree:[
      {path:'data/icourse-index.enc',type:'blob',sha:'index'},
      {path:'data/shards/meta.enc',type:'blob',sha:'meta',size:32},
    ]};
    return {ok:true,status:200,json:async()=>json,arrayBuffer:async()=>new Uint8Array([1,2]).buffer};
  };
  const manifest=await w.ICS.github.fetchShardManifest('user','repo','data','');
  assert.equal(manifest.format,'sharded');
  await w.ICS.github.fetchBlobBytes('user','repo','index');
  assert.equal(requests.length,4);
  for(const item of requests) assert.ok(!('Authorization' in item.options.headers));
  await w.ICS.github.fetchBlobBytes('user','repo','index',' mock-token ');
  assert.equal(requests.at(-1).options.headers.Authorization,'token mock-token');

  const before=requests.length;
  for(const fn of [
    ()=>w.ICS.github.setCourseIdsSecret('user','repo','',['1']),
    ()=>w.ICS.github.triggerSingleRunWorkflow('user','repo','main','','1',true),
    ()=>w.ICS.github.triggerDeleteWorkflow('user','repo','main','','1','2'),
    ()=>w.ICS.github.triggerExportWorkflow('user','repo','main','','1','PDF','2'),
    ()=>w.ICS.github.getRepoPublicKey('user','repo',''),
    ()=>w.ICS.github.putRepoSecret('user','repo','','NAME','cipher','key'),
  ]) await assert.rejects(fn,/管理授权/);
  assert.equal(requests.length,before,'Unauthorized management must not make any request');

  let makeApp;
  w.Alpine={data:(name,factory)=>{assert.equal(name,'app');makeApp=factory;}};
  w.document.dispatchEvent(new w.Event('alpine:init'));
  const app=makeApp();
  const searches=[];
  w.ICS.db={searchSummaries:(...args)=>{
    searches.push(args);
    return {results:args[0]==='矩阵'?[{sub_id:'search-fixture',summary:'矩阵的秩'}]:[],page:args[2],hasMore:false};
  }};
  app.searchQuery='   ';app._loadSearchResults(1);
  assert.equal(searches.length,0,'An empty search is an input prompt, not a failed database query');
  assert.equal(app.searchResults.length,0);
  app.searchQuery='矩阵';app._loadSearchResults(1);
  assert.equal(searches.length,1);assert.equal(app.searchResults[0].sub_id,'search-fixture');
  app.searchQuery='没有匹配的关键词';app._loadSearchResults(1);
  assert.equal(app.searchResults.length,0);assert.equal(app.searchHasMore,false);
  app.lectures = ['ready', 'waiting', 'failed', 'novideo', 'skipped', 'processing'].map(
    (state, i) => ({sub_id: String(i + 1), state})
  );
  app.lectures.push({sub_id: 'deleted', state: 'ready', deleted_at: '2026-10-11'});
  app.lectures.push({state: 'waiting'});
  assert.equal(app.getDeletableLectures().length, 6, 'Unprocessed and failed lectures must be suppressible');
  app.openDeleteDialog();
  app.setDeleteAll(true);
  assert.equal(app.selectedDeleteCount(), 6);
  assert.equal(app.isDeleteAllSelected(), true);
  app.currentCourse = {course_id: '10'};
  w.sessionStorage.setItem('ics_creds', JSON.stringify({token: 'offline-delete-token'}));
  const deletions=[];
  w.ICS.github.triggerDeleteWorkflow = async (...args) => { deletions.push(args); };
  w.setTimeout = () => 0;
  app._toast = () => {};
  await app.confirmDelete();
  assert.equal(deletions.length, 1);
  assert.deepEqual(Array.from(deletions[0][4]), ['10']);
  assert.deepEqual(Array.from(deletions[0][5]), ['1', '2', '3', '4', '5', '6']);
  assert.equal(app.deleteDialogOpen, false);
  w.sessionStorage.removeItem('ics_creds');
  const key='a'.repeat(64);
  let unlocks=0;
  app.testAndSave=async()=>{unlocks++;};
  const form = w.document.createElement('form');
  const passwordInput = w.document.createElement('input');
  passwordInput.name = 'password'; passwordInput.type = 'password';
  form.append(passwordInput); w.document.body.append(form);
  app.setup.dbkey = 'stale-model-value';
  passwordInput.value = key; // Native AutoFill without an input/change event.
  await app.unlockFromForm({currentTarget:form});
  assert.equal(app.setup.dbkey,key);
  assert.equal(unlocks,1);
  app.setupTesting=true; passwordInput.value='must-not-replace-in-flight-key';
  await app.unlockFromForm({currentTarget:form});
  assert.equal(app.setup.dbkey,key); assert.equal(unlocks,1);
  app.setupTesting=false; unlocks=0;
  const input={value:'private-file',files:[{size:65,text:async()=>key+'\n'}]};
  await app.unlockWithKeyFile({target:input});
  assert.equal(app.setup.dbkey,key);
  assert.equal(input.value,'');
  assert.equal(unlocks,1);
  assert.equal(w.localStorage.getItem('ics_creds'),null);
  const originalKey=app.setup.dbkey;
  for(const file of [
    {size:5000,text:async()=>{throw new Error('must not read oversized file');}},
    {size:100,text:async()=>'DB_ENCRYPTION_KEY='+key},
    {size:100,text:async()=>key+'\nOTHER=private'},
    {size:2,text:async()=>'no'},
  ]) {
    await app.unlockWithKeyFile({target:{value:'x',files:[file]}});
    assert.equal(unlocks,1);
    assert.equal(app.setup.dbkey,originalKey);
    assert.ok(app.setupError);
    assert.ok(!app.setupError.includes(key));
  }
  app.setupTesting=true;
  await app.unlockWithKeyFile({target:{value:'x',files:[{size:64,text:async()=>{throw new Error('must not read while busy');}}]}});
  assert.equal(unlocks,1);
  assert.equal(w.sessionStorage.getItem('ics_creds'),null);
  dom.window.close();
  console.log('Public read, authenticated read, management guards and local-file privacy passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
