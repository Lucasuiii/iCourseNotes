const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname,'..');

async function scenario({state='active',secretStatus=204,dispatchStatus=204,networkFailure=false,secretNetwork=false}) {
  const dom = new JSDOM('',{url:'https://fixture.invalid/',runScripts:'outside-only'});
  const w = dom.window;
  w.eval(fs.readFileSync(path.join(root,'frontend/js/github.js'),'utf8'));
  w._newRequestId=()=> '12345678-1234-4234-8234-123456789abc';
  w.sodium={ready:Promise.resolve(),base64_variants:{ORIGINAL:1},
    from_base64:()=>new Uint8Array(32),from_string:text=>text,
    crypto_box_seal:()=>new Uint8Array([1]),to_base64:()=> 'encrypted-fixture'};
  const requests=[];
  w.fetch=async (url,options={})=>{
    const method=options.method || 'GET';requests.push({url,method,body:options.body});
    if(method==='GET' && url.includes('/workflows/')) return {ok:true,json:async()=>({state})};
    if(method==='GET' && url.endsWith('/public-key')) return {ok:secretStatus!==403,status:secretStatus===403?403:200,
      text:async()=> 'Resource not accessible by personal access token',json:async()=>({key:'fixture',key_id:'1'})};
    if(method==='PUT') {
      if(secretNetwork) throw Error('secret PUT response lost');
      return {status:secretStatus,text:async()=> 'Secrets permission rejected'};
    }
    if(method==='POST') {
      if(networkFailure) throw Error('POST response lost');
      return {status:dispatchStatus,text:async()=>{throw Error('optional response body unreadable');}};
    }
    throw Error('unexpected request');
  };
  let error;
  try { await w.ICS.github.triggerDeleteWorkflow('example','notes','main','synthetic-token',['10'],[]); }
  catch(e){error=e;}
  dom.window.close();return {error,requests};
}

(async()=>{
  let r=await scenario({state:'disabled_manually'});
  assert.equal(r.error.dispatchOutcome,'not_sent');assert.match(r.error.message,/已停用/);
  assert.equal(r.requests.length,1,'Disabled workflow must not write a secret or dispatch');
  r=await scenario({secretStatus:403});
  assert.equal(r.error.dispatchOutcome,'not_sent');assert.match(r.error.message,/Secrets/);
  assert.ok(!r.requests.some(x=>x.method==='POST'));
  r=await scenario({secretStatus:422});
  assert.equal(r.error.dispatchOutcome,'not_sent');assert.ok(!r.requests.some(x=>x.method==='POST'));
  r=await scenario({secretNetwork:true});
  assert.equal(r.error.dispatchOutcome,'not_sent','Unknown secret PUT cannot have dispatched a deletion');
  for(const dispatchStatus of [400,401,403,404,409,422]){
    r=await scenario({dispatchStatus});
    assert.equal(r.error.dispatchOutcome,'rejected');assert.equal(r.error.httpStatus,dispatchStatus);
    assert.equal(r.requests.filter(x=>x.method==='POST').length,1);
  }
  for(const options of [{networkFailure:true},{dispatchStatus:500},{dispatchStatus:503},{dispatchStatus:429}]){
    r=await scenario(options);assert.equal(r.error.dispatchOutcome,'unknown');
    assert.equal(r.requests.filter(x=>x.method==='POST').length,1,'Unknown POST is never retried');
  }
  r=await scenario({});assert.equal(r.error,undefined);
  const post=r.requests.find(x=>x.method==='POST');
  assert.deepEqual(JSON.parse(post.body),{ref:'main',inputs:{request_id:'12345678-1234-4234-8234-123456789abc'}});
  assert.ok(!post.body.includes('10'),'Only opaque request identity is public');
  console.log('Secret preparation, disabled workflow, rejected dispatch and unknown POST classifications passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
