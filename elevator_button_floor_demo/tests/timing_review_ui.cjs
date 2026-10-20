// Generated synthetic timing page only; no hardware, browser service or API.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const page=fs.readFileSync(process.argv[2],'utf8');
const script=page.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements={};
function element(id){return elements[id]??=({value:'',textContent:'',removeAttribute(name){delete this[name];},click(){this.onclick?.();}});}
const context={document:{getElementById:element,createElement:()=>({click(){}})},
 URL:{createObjectURL:()=>'',revokeObjectURL(){}},Blob:class{},setTimeout:()=>1,clearTimeout(){},console};
vm.createContext(context);vm.runInContext(script,context);
function mark(opened,previous){context.opened=opened;context.previous=previous;
 vm.runInContext("trial.actual_button_lit_ros_stamp=opened;trial.actual_button_lit_prev_ros_stamp=previous;show()",context);
 return element('selection').textContent;}
assert.match(mark(1001,null),/起点未被完整记录/);
assert.doesNotMatch(element('selection').textContent,/识别确认通过/);
assert.match(mark(1003,1002.9),/早于未亮识别就绪/);
assert.match(mark(1004,1003),/跨过就绪锚点/);
assert.match(mark(1004,1003.9),/均晚于就绪锚点/);
assert.match(mark(1004,null),/缺少前边界/);
console.log('Timing review UI: all checks passed');
