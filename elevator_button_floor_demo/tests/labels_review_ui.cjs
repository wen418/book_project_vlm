// Offline browser logic verification: generated synthetic HTML, no ROS or network.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements={}, handlers={};let download;
function element(id){return elements[id]??=({value:'',checked:false,disabled:false,hidden:false,style:{},textContent:'',
 removeAttribute(name){delete this[name];},click(){if(!this.disabled)this.onclick?.();}});}
const context={document:{getElementById:element,createElement:()=>({click(){}}),addEventListener:(event,fn)=>{handlers[event]=fn;}},
 window:{addEventListener(){}},Blob:class{constructor(parts){download=parts.join('');}},
 URL:{createObjectURL:()=>'',revokeObjectURL(){}},setTimeout:()=>1,console};
element('autoNext').checked=true;
vm.createContext(context);vm.runInContext(script,context);
function loaded(){if(element('frame').src)element('frame').onload?.();}
loaded();
// Door true prediction, actual false: show the error and advance.
element('truthB').click();assert.match(element('notice').textContent,/识别错误/);
loaded();
assert.match(element('meta').textContent,/obs_00002/);
// Button lighting true. Note must survive CSV quotes, commas and embedded newlines.
element('note').value='反光,"双引号"\n下一行';element('note').oninput();element('truthA').click();
loaded();
assert.match(element('meta').textContent,/obs_00003/);
element('floor').value='15.5';element('saveFloor').click();assert.match(element('notice').textContent,/整数/);
element('floor').value='15';element('saveFloor').click();
// Fourth image missing: known truth disabled, unknown allowed.
assert.equal(element('truthA').disabled,true);element('truthA').click();
assert.match(element('meta').textContent,/obs_00004/);element('unknown').click();
element('export').click();assert.ok(download.startsWith('\uFEFFid,'));
const exported=download;fs.writeFileSync(process.argv[3],exported,'utf8');
const table=vm.runInContext('parseCSV(exportCSV())',context);
assert.equal(table.length,5);assert.equal(table[2][6],'反光,"双引号"\n下一行');
assert.equal(table[1][5],'false');assert.equal(table[2][5],'true');assert.equal(table[3][5],'15');assert.equal(table[4][5],'null');
// Import with a changed immutable image must fail before any existing labels change.
context.bad=exported.replace('images/qa_0.png','images/foreign.png');
assert.throws(()=>vm.runInContext('importCSV(bad)',context),/不匹配/);
assert.equal(vm.runInContext('label(rows[0]).ground_truth',context),'false');
assert.throws(()=>vm.runInContext('importCSV(exportCSV().replace("false,", "17,"))',context),/true、false/);
context.good=exported;assert.equal(vm.runInContext('importCSV(good)',context),4);
// Filters keep annotated and unannotated states separate.
element('filter').value='incorrect';element('filter').onchange();assert.match(element('meta').textContent,/obs_00001/);
element('filter').value='unreviewed';element('filter').onchange();assert.match(element('imageNotice').textContent,/没有结果/);
element('filter').value='all';element('filter').onchange();element('autoNext').checked=false;
element('frame').onerror();assert.equal(element('truthA').disabled,true);
// Ignoring edit-field shortcuts prevents accidental changes while typing notes.
handlers.keydown({key:'1',target:{tagName:'TEXTAREA'},preventDefault(){throw Error('shortcut fired while typing');}});
// A previously hand-edited invalid label is repairable rather than crashing the page.
vm.runInContext("labels[rows[0].id].ground_truth='FALSE';draw();",context);
assert.match(element('judgment').textContent,/格式无效/);
assert.throws(()=>vm.runInContext('exportCSV()',context));
console.log('Visual labels: truth types, auto-next, filters, missing/broken images, CSV round trip and foreign-run rejection passed.');
