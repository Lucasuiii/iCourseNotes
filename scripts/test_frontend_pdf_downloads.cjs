const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
(async()=>{
 const root=path.join(__dirname,'..');
 for(const [vendor,installed] of [['html2canvas-1.4.1.min.js','html2canvas/dist/html2canvas.min.js'],['jspdf-4.2.1.umd.min.js','jspdf/dist/jspdf.umd.min.js']])
   assert.deepEqual(fs.readFileSync(path.join(root,'frontend/js/vendor',vendor)),fs.readFileSync(require.resolve(installed)));
 const dom=new JSDOM('<main>Viewer</main>',{url:'https://notes.invalid/iCourseNotes/',runScripts:'outside-only'}),w=dom.window;
 w.Blob=Blob;w.TextDecoder=TextDecoder;w.TextEncoder=TextEncoder;
 Object.defineProperty(w.document,'fonts',{value:{ready:Promise.resolve()}});
 const rendered=[],clicks=[],urls=[],revoked=[];let fail=false,blank=false;
 w.URL.createObjectURL=blob=>{const url='blob:pdf-'+urls.length;urls.push({url,blob});return url;};w.URL.revokeObjectURL=url=>revoked.push(url);
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};w.HTMLDialogElement.prototype.close=function(){this.dispatchEvent(new w.Event('close'));};
 w.HTMLAnchorElement.prototype.click=function(){clicks.push(this.download);};
 w.ICS={exports:{buildPrintHtml:(course,[row])=>{if(row.bad)throw Error('Missing image');return '<html><body data-icourse-print><h1>'+course.title+'</h1><article><p>'+row.summary+'</p></article></body></html>';}}};
 const append=w.document.head.appendChild.bind(w.document.head);const sources=[];
 w.document.head.appendChild=node=>{const result=append(node);if(node.tagName==='SCRIPT'){sources.push(node.src);queueMicrotask(()=>node.onload());}return result;};
 const bodyAppend=w.document.body.appendChild.bind(w.document.body);
 w.document.body.appendChild=node=>{const result=bodyAppend(node);if(node.tagName==='IFRAME'){
   assert.equal(node.getAttribute('sandbox'),'allow-same-origin');node.contentDocument.open();node.contentDocument.write(node.srcdoc);node.contentDocument.close();
   Object.defineProperty(node.contentDocument,'fonts',{value:{ready:Promise.resolve()}});
   queueMicrotask(()=>node.onload());
 }return result;};
 w.html2canvas=async element=>{if(fail&&element.textContent.includes('SECOND'))throw Error('Render failed');rendered.push(element.textContent);return {width:1360,height:2016,toDataURL:()=> 'data:image/jpeg;base64,fake',getContext:()=>({getImageData:()=>({data:new Uint8Array(4096).map((_,i)=>i%4===3?255:blank?255:0)})})};};
 class Pdf {setProperties(){}addPage(){}addImage(){}output(){return new TextEncoder().encode('%PDF-1.3\nSynthetic test only').buffer;}}
 w.jspdf={jsPDF:Pdf};w.eval(fs.readFileSync(path.join(root,'frontend/js/pdf-download.js'),'utf8'));
 const rows=[{sub_id:'1',sub_title:'<first>',summary:'FIRST'},{sub_id:'2',sub_title:'second/lesson',summary:'SECOND'},{sub_id:'3',sub_title:'third',summary:'THIRD'}],progress=[];
 const dialog=await w.ICS.pdfDownloads.download({title:'课程'},rows,x=>progress.push(x));
 assert.equal(sources.length,2);assert.ok(sources.every(src=>src.startsWith('https://notes.invalid/iCourseNotes/js/vendor/')));
 assert.equal(rendered.length,3);assert.ok(rendered[0].includes('FIRST')&&!rendered[0].includes('SECOND'));assert.ok(rendered[1].includes('SECOND')&&!rendered[1].includes('FIRST'));
 assert.equal(urls.length,3);assert.equal(clicks.length,3,'One batch requests separate PDF downloads, never a ZIP');
 assert.ok(clicks.every(name=>name.endsWith('.pdf')&&!/[<>/]/.test(name)));assert.equal(new Set(clicks).size,3);
 assert.equal(dialog.querySelectorAll('a[download]').length,3);assert.equal(w.document.querySelectorAll('iframe').length,0);
 let cleanup;const originalTimer=w.setTimeout;w.setTimeout=(callback,delay)=>{assert.equal(delay,60000);cleanup=callback;};
 dialog.close();assert.equal(revoked.length,0,'Downloads retain their URLs while the browser starts saving');cleanup();w.setTimeout=originalTimer;
 assert.equal(revoked.length,3);assert.equal(w.document.querySelectorAll('dialog').length,0);
 const prior=clicks.length;fail=true;
 await assert.rejects(()=>w.ICS.pdfDownloads.download({title:'课程'},rows),/Render failed/);
 assert.equal(clicks.length,prior,'A failed later lesson must not download a partial batch');assert.equal(w.document.querySelectorAll('iframe,dialog').length,0);
 fail=false;blank=true;
 await assert.rejects(()=>w.ICS.pdfDownloads.download({title:'课程'},rows),/为空/);assert.equal(clicks.length,prior);
 await assert.rejects(()=>w.ICS.pdfDownloads.download({title:'课程'},[rows[0],{bad:true}]),/Missing image/);assert.equal(clicks.length,prior);
 assert.equal(sources.length,2,'Pinned libraries load only once');
 const blocks=new JSDOM('<body><article><ol start="4"><li>A</li><li>B</li><li>C</li></ol></article></body>');
 w.ICS.pdfDownloads.paginate(blocks.window.document.body);assert.deepEqual(Array.from(blocks.window.document.querySelectorAll('ol'),x=>x.start),[4,5,6]);
 dom.window.close();blocks.window.close();console.log('Separate PDF selection, atomic batch generation, blank-page rejection, local pinned assets, names and cleanup verified.');
})().catch(error=>{console.error(error);process.exit(1);});
