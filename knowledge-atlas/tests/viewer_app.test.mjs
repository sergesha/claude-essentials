import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {GraphExplorer} from '../src/knowledge_atlas/viewer/static/graph.mjs';
import {MutationQueue} from '../src/knowledge_atlas/viewer/static/requests.mjs';
const source=readFileSync(new URL('../src/knowledge_atlas/viewer/static/app.js',import.meta.url),'utf8').replace(/^import .*\n/gm,'');
function app(fetch){
 const elements=new Map();
 function element(){return {value:'',textContent:'',attributes:{},setAttribute(k,v){this.attributes[k]=String(v);},focus(){this.focused=true;},hidden:false,children:[],dataset:{},classList:{toggle(){}},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},querySelectorAll(){return [];}};}
 const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element};
 const context=vm.createContext({MutationQueue,GraphExplorer,document,fetch,location:{hash:'',pathname:'/'},history:{replaceState(){}},window:{addEventListener(){}},structuredClone,crypto:{randomUUID:()=> 'test-request'},setTimeout:fn=>{fn();},console});
 vm.runInContext(source,context);document.getElementById("evidence").hidden=true;return {context,elements,run:code=>vm.runInContext(code,context)};
}
const response=(data,status=200)=>({ok:status===200,status,json:async()=>data});
test('admitted mutation retains reconciliation when operation polling is busy',async()=>{
 const a=app(async(url)=>url.endsWith('operation.read')?response({error:{code:'busy',message:'busy'}},409):response({operation_id:'admitted',state:'running'}));
 a.run("selected=ready='publication';");
 await assert.rejects(a.run("mutate('candidate.decide',{candidate:'candidate',action:'accept'})"));
 assert.equal(a.run('queue.pending?.operation_id'),'admitted');
});
test('refresh cannot silently move a historical question to READY',async()=>{
 let historyStarted;const started=new Promise(resolve=>historyStarted=resolve);let asked;
 const a=app(async(url,options)=>{
  if(url.endsWith('atlas.status'))return response({publication:'new-ready',data:{}});
  if(url.endsWith('history')){historyStarted();return new Promise(()=>{});}
  if(url.endsWith('question.ask')){asked=JSON.parse(options.body).publication;return response({});}
 });
 a.run("selected='historical';ready='old-ready';refresh();");await started;
 await a.run("read('question.ask',{question:'What was known?'})");
 assert.equal(asked,'historical');
});
test('late historical citation cannot change the current clarification target',async()=>{
 let finish;
 const a=app(async(url)=>{
  if(url.endsWith('question.ask'))return response({publication:'historical',data:{answer:'Answer',unknowns:[],bounds:{truncated:false},citations:['old-record']}});
  if(url.endsWith('evidence.read'))return new Promise(resolve=>finish=()=>resolve(response({data:{record:{'@id':'old-record'}}})));
 });
 a.run("selected='historical';ready='current';");
 await a.elements.get('ask-form').onsubmit();
 const click=a.elements.get('citations').children[0].onclick();
 a.run("selected='current';target(null);");finish();await click;
 assert.equal(a.run('targetId'),null);
 assert.notEqual(a.elements.get('evidence').hidden,false);
});

test('task navigation changes visible panel without changing the publication',()=>{
 const a=app(async()=>response({}));a.run("selected='historical';showPanel('ask');");
 assert.equal(a.elements.get('panel-read').hidden,true);
 assert.equal(a.elements.get('panel-ask').hidden,false);
 assert.equal(a.elements.get('nav-ask').attributes['aria-pressed'],'true');
 assert.equal(a.run('selected'),'historical');
});
test('page filter hides nonmatches and clearing it restores navigation',()=>{
 const a=app(async()=>response({}));
 a.run("$('pages').append(button('Harness Bench',()=>{}),button('Execution Alignment',()=>{}));$('page-filter').value='harness';filterPages();");
 assert.equal(a.elements.get('pages').children[0].hidden,false);
 assert.equal(a.elements.get('pages').children[1].hidden,true);
 a.run("$('page-filter').value='';filterPages();");assert.equal(a.elements.get('pages').children[1].hidden,false);
});
test('readable changes retain unsafe source text as text and include removals',()=>{
 const a=app(async()=>response({}));
 a.run("renderChanges($('diff'),[{id:'Record/a',before:{kind:'wiki',text:'old'},after:{kind:'wiki',text:'<img src=x onerror=alert(1)>'}},{id:'Record/b',before:{text:'removed'},after:null}]);");
 const all=[];function collect(n){all.push(n);for(const c of n.children||[])collect(c);}collect(a.elements.get('diff'));
 assert.ok(all.some(n=>n.textContent==='<img src=x onerror=alert(1)>'));
 assert.ok(all.some(n=>n.textContent==='removed'));
 assert.ok(all.every(n=>!n.innerHTML));
});
test('switching panels never enables acceptance without inspection',()=>{
 const a=app(async()=>response({}));a.run("resetReview();showPanel('review');showPanel('read');showPanel('review');");
 assert.equal(a.elements.get('accept').disabled,true);assert.equal(a.run('inspected'),null);
});
test('proposal summary compares wiki content by path and retains truncated warning',()=>{
 const a=app(async()=>response({}));
 const result=a.run("wikiChanges({wiki:{pages:[{path:'/wiki/',filename:'a.md',content:'old'}]}},{wiki:{pages:[{path:'/wiki/',filename:'a.md',content:'new',content_truncated:true}]}})");
 assert.equal(result.length,1);assert.equal(result[0].before.text,'old');assert.equal(result[0].after.text,'new');assert.equal(result[0].after.content_truncated,true);
});
test('editing proposal selection invalidates an inspection already in flight',async()=>{
 let finish;const a=app(async()=>new Promise(resolve=>finish=()=>resolve(response({publication:'current',data:{before:{},snapshot:{},unresolved:[],type_questions:[]}}))));
 a.run("selected=ready='current';");const inspecting=a.run("inspect('proposal-A')");
 a.run("$('proposal-list').value='proposal-B';$('proposal-list').onchange();");finish();await inspecting;
 assert.equal(a.run('inspected'),null);assert.equal(a.elements.get('candidate').value,'proposal-B');assert.equal(a.elements.get('accept').disabled,true);
});
test('navigation from a disappearing panel places focus in the destination',()=>{
 const a=app(async()=>response({}));a.run("showPanel('history');showPanel('read',true);");
 assert.equal(a.elements.get('panel-read').focused,true);assert.equal(a.elements.get('panel-history').hidden,true);
});
test('wiki-only comparison never claims the complete proposal is unchanged',()=>{
 const a=app(async()=>response({}));a.run("renderChanges($('review'),[],'wiki');");
 assert.match(a.elements.get('review').children[0].textContent,/wiki/i);
 assert.match(a.elements.get('review').children[0].textContent,/snapshot/i);
});
test('publication context exposes exact ID and updates timeline selection',async()=>{
 const a=app(async()=>response({data:{pages:[],sources:[],acceptance_kind:'synthetic'}}));
 a.run("selected='historical';ready='current';const old=button('Old',()=>{});old.dataset.publication='historical';const current=button('Current',()=>{});current.dataset.publication='current';$('timeline').append(current,old);");
 await a.run('loadPublication()');
 assert.equal(a.elements.get('publication-id').textContent,'historical');assert.equal(a.elements.get('timeline').children[1].attributes['aria-current'],'true');assert.equal(a.elements.get('timeline').children[0].attributes['aria-current'],'false');
});
test('internal wiki links read canonical pages within the selected version',async()=>{
 let requested;const a=app(async(url,options)=>{requested=JSON.parse(options.body);return response({data:{page:{title:'Linked page',html:'<p>Evidence</p>'}}});});
 const link={getAttribute:()=>'/wiki/linked.md'};a.run("$('page');");a.elements.get('page').querySelectorAll=()=>[link];
 a.run("selected='historical';pageLinks=new Map([['/wiki/linked.md','Record/linked']]);bindPageLinks();");
 let prevented=false;await link.onclick({preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.equal(requested.publication,'historical');assert.equal(requested.payload.page_id,'Record/linked');
});
test('wiki entry opens its canonical record in the graph panel',async()=>{
 const a=app(async()=>response({}));a.run("selected='historical';pageId='Record/wiki';");
 assert.equal(typeof a.elements.get('graph-page')?.onclick,'function');
 await a.elements.get('graph-page').onclick();assert.equal(a.elements.get('panel-graph').hidden,false);
});
test('late graph wiki navigation does not leave the new publication panel',async()=>{
 let finish;const a=app(async()=>new Promise(resolve=>finish=()=>resolve(response({data:{page:{title:'Old',html:'old'}}}))));
 a.run("selected='old';showPanel('graph');");const pending=a.run("graph.onOpenPage('Record/old')");a.run("selected='new';viewEpoch++;showPanel('history');");finish();await pending;
 assert.equal(a.elements.get('panel-history').hidden,false);
});

test('acceptance attribution identifies the channel and unsigned receipt separately',()=>{
 const a=app(async()=>response({}));
 const label=a.run("attribution({acceptance_kind:'human',acceptance_channel:'viewer',matching_decision_receipt:true})");
 assert.match(label,/human \(caller-supplied\)/);
 assert.match(label,/Channel: viewer/);
 assert.match(label,/Matching decision receipt present/);
 const old=a.run("attribution({acceptance_kind:'synthetic'})");
 assert.match(old,/unrecorded/);
 assert.match(old,/No matching decision receipt/);
});
