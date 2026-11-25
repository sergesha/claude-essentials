import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {GraphExplorer} from '../src/knowledge_atlas/viewer/static/graph.mjs';
import {MutationQueue} from '../src/knowledge_atlas/viewer/static/requests.mjs';
const source=readFileSync(new URL('../src/knowledge_atlas/viewer/static/app.js',import.meta.url),'utf8').replace(/^import .*\n/gm,'');
function app(fetch,{hash='',confirm=()=>true}={}){
 const elements=new Map();
 function element(){return {value:'',textContent:'',attributes:{},setAttribute(k,v){this.attributes[k]=String(v);},focus(){this.focused=true;},hidden:false,children:[],dataset:{},classList:{toggle(){}},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},querySelectorAll(){return [];}};}
 const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element};
 const context=vm.createContext({MutationQueue,GraphExplorer,document,fetch,location:{hash,pathname:'/'},history:{replaceState(){}},window:{addEventListener(){},confirm},structuredClone,crypto:{randomUUID:()=> 'test-request'},setTimeout:fn=>{fn();},console});
 vm.runInContext(source,context);document.getElementById("evidence").hidden=true;return {context,elements,run:code=>vm.runInContext(code,context)};
}
const response=(data,status=200)=>({ok:status===200,status,json:async()=>data});
test('admitted mutation retains reconciliation when operation polling is busy',async()=>{
 const a=app(async(url)=>url.endsWith('operation.read')?response({error:{code:'busy',message:'busy'}},409):response({operation_id:'admitted',state:'running'}));
 a.run("selected=ready='publication';");
 await assert.rejects(a.run("mutate('candidate.decide',{candidate:'candidate',action:'accept'})"));
 assert.equal(a.run('queue.get(project.atlasId)?.operation_id'),'admitted');
});
test('refresh cannot silently move a historical question to READY',async()=>{
 let historyStarted;const started=new Promise(resolve=>historyStarted=resolve);let asked;
 const a=app(async(url,options)=>{
  if(url.endsWith('atlas.status'))return response({publication:'new-ready',data:{}});
  if(url.endsWith('history')){historyStarted();return new Promise(()=>{});}
  if(url.endsWith('question.ask')){asked=JSON.parse(options.body).publication;return response({});}
 });
 a.run("project={atlasId:'atlas',generation:1};selected='historical';ready='old-ready';refresh();");await started;
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

const atlasA='10000000-0000-4000-8000-000000000001';
const atlasB='10000000-0000-4000-8000-000000000002';
const aReady='a'.repeat(40),aOld='c'.repeat(40),bReady='b'.repeat(40);
const aPage='Record/20000000-0000-4000-8000-000000000001';
const oldPage='Record/20000000-0000-4000-8000-000000000002';
const bPage='Record/20000000-0000-4000-8000-000000000003';
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){
 let resolve;
 const promise=new Promise(done=>{resolve=done;});
 return {promise,resolve};
}
function atlasFixture({bPublication=bReady,confirm,onRequest}={}){
 const calls=[];
 const a=app(async(url,options)=>{
  const action=url.slice('/api/'.length),body=JSON.parse(options.body),id=body.atlas_id;
  calls.push({action,body});
  const override=onRequest?.(action,body);
  if(override!==undefined)return override;
  if(action==='atlas.list')return response({launch_atlas_id:atlasA,data:{projects:[
   {atlas_id:atlasA,name:'Notes',publication:aReady,status:'ready'},
   {atlas_id:atlasB,name:'Notes',publication:bPublication,status:bPublication?'ready':'unpublished'},
  ]}});
  if(![atlasA,atlasB].includes(id))return response({error:{message:'Project UUID required'}},400);
  const ready=id===atlasA?aReady:bPublication;
  const publication=body.publication??ready;
  const page=publication===aOld?oldPage:id===atlasA?aPage:bPage;
  const data={
   'atlas.open':{atlas_id:id,path:'/fixture/notes'},
   'atlas.status':{candidates:id===atlasA?['proposal-a']:['proposal-b']},
   history:{history:(id===atlasA?[aReady,aOld]:[bReady]).map(publication_commit=>({publication_commit}))},
   'wiki.pages':{pages:[{id:page,title:'Page '+page,path:'/wiki/page.md'}],sources:[{name:'Source '+id}]},
   'wiki.read':{page:{id:page,title:'Page '+page,html:'<p>'+page+'</p>'}},
   search:{hits:[{record:{'@id':page,kind:'wiki',text:page}}]},
   'question.ask':{answer:'Answer '+id,unknowns:[],bounds:{truncated:false},citations:[page]},
  }[action];
  if(!data)throw Error('Unexpected viewer request '+action);
  return response({publication,data});
 },{hash:'#test-token',confirm});
 const get=id=>a.elements.get(id);
 const select=id=>{get('atlas-project').value=id;return get('atlas-project').onchange();};
 const displayed=(id,publication,page)=>{
  assert.equal(get('atlas-project')?.value,id);
  assert.equal(get('publication-id').textContent,publication??'No published version');
  assert.equal(get('page-id').textContent,page??'');
 };
 return {...a,get,select,displayed,calls};
}

test('same-name projects show their UUIDs and switch from historical A to B READY',async()=>{
 const a=atlasFixture();
 await turn();
 a.displayed(atlasA,aReady,aPage);
 assert.deepEqual(a.get('atlas-project').children.map(option=>option.textContent),[
  'Choose a project…','Notes · '+atlasA,'Notes · '+atlasB,
 ]);
 a.get('publication').value=aOld;await a.get('publication').onchange();
 a.displayed(atlasA,aOld,oldPage);
 a.get('page-filter').value='old filter';a.get('page-filter').oninput();
 a.get('graph-query').value='old search';
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 assert.equal(a.get('page-filter').value,'');
 assert.equal(a.get('graph-query').value,'');
 assert.equal(a.get('return-current').hidden,true);
 assert.equal(a.get('candidate').value,'proposal-b');
 assert.equal(a.get('timeline').children.length,1);
 assert.equal(a.get('sources').children[0].textContent.startsWith('Source '+atlasB),true);
 assert.equal(a.get('panel-read').hidden,false);
 assert.deepEqual(a.calls.filter(call=>call.body.atlas_id===atlasB).map(call=>call.action),[
  'atlas.open','atlas.status','history','wiki.pages','wiki.read',
 ]);
});

test('switching to an unpublished project clears previously displayed records and controls',async()=>{
 const a=atlasFixture({bPublication:null});
 await turn();
 await a.select(atlasB);
 a.displayed(atlasB,null,null);
 assert.equal(a.get('timeline').children.length,0);
 assert.equal(a.get('sources').children.length,0);
 assert.equal(a.get('pages').children.length,0);
 assert.equal(a.get('graph-page').disabled,true);
 assert.equal(a.get('propose').disabled,true);
 assert.match(a.get('page').textContent,/No accepted publication/);
 assert.equal(a.calls.some(call=>call.action==='wiki.read'&&call.body.atlas_id===atlasB),false);
});

test('a rejected or mismatched project verification preserves the current view',async()=>{
 for(const rejection of [response({error:{message:'Project unavailable'}},404),response({data:{atlas_id:atlasA}})]){
  const a=atlasFixture({onRequest:(action,body)=>action==='atlas.open'&&body.atlas_id===atlasB?rejection:undefined});
  await turn();
  a.get('publication').value=aOld;await a.get('publication').onchange();
  a.get('clarification').value='Unsent correction';
  a.get('nav-history').onclick();
  await a.select(atlasB);
  a.displayed(atlasA,aOld,oldPage);
  assert.equal(a.get('clarification').value,'Unsent correction');
  assert.equal(a.get('panel-history').hidden,false);
  assert.match(a.get('notice').textContent,/project/i);
  assert.equal(a.calls.some(call=>call.action==='atlas.status'&&call.body.atlas_id===atlasB),false);
 }
});

test('cancel preserves unsent form text, then confirmation clears it during the switch',async()=>{
 const confirmations=[];
 const a=atlasFixture({confirm:message=>{confirmations.push(message);return confirmations.length===2;}});
 await turn();
 a.get('clarification').value='Unsent correction';
 a.get('reason').value='Unsent decision';
 a.get('question').value='Unsent question';
 a.get('actor').value='Reviewer';
 a.get('nav-review').onclick();
 const previousNotice=a.get('notice').textContent;
 await a.select(atlasB);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(a.get('clarification').value,'Unsent correction');
 assert.equal(a.get('reason').value,'Unsent decision');
 assert.equal(a.get('question').value,'Unsent question');
 assert.equal(a.get('actor').value,'Reviewer');
 assert.equal(a.get('notice').textContent,previousNotice);
 assert.equal(a.get('panel-review').hidden,false);
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 assert.equal(confirmations.length,2);
 assert.match(confirmations[0],/discard/i);
 for(const id of ['clarification','reason','question','actor'])assert.equal(a.get(id).value,'');
});

for(const returnToA of [false,true])test('late work from A cannot affect '+(returnToA?'a new A generation':'B'),async()=>{
 const delayed=Object.fromEntries(['atlas.status','wiki.read','question.ask','search'].map(action=>[action,deferred()]));
 let delaying=false;
 const a=atlasFixture({onRequest:(action,body)=>delaying&&body.atlas_id===atlasA?delayed[action]?.promise:undefined});
 await turn();
 delaying=true;
 const refresh=a.get('refresh').onclick();
 const page=a.get('pages').children[0].onclick();
 a.get('question').value='Submitted old question';
 const ask=a.get('ask-form').onsubmit();
 a.get('graph-query').value='Old search';
 const search=a.get('graph-search-form').onsubmit({preventDefault(){}});
 delaying=false;
 await a.select(atlasB);
 if(returnToA)await a.select(atlasA);
 const currentAtlas=returnToA?atlasA:atlasB;
 const newAsk=deferred();
 const current=returnToA?aReady:bReady,shown=returnToA?aPage:bPage;
 // Hold a new question in flight so the old finally block cannot reset its controls.
 const currentFetch=a.context.fetch;
 a.context.fetch=(url,options)=>url.endsWith('question.ask')?newAsk.promise:currentFetch(url,options);
 a.get('question').value='Current question';
 const newQuestion=a.get('ask-form').onsubmit();
 a.get('nav-history').onclick();
 const notice=a.get('notice').textContent;
 const graphStatus=a.get('graph-status').textContent;
 const answer=a.get('answer').textContent;
 delayed['atlas.status'].resolve(response({publication:'obsolete',data:{candidates:['obsolete proposal']}}));
 delayed['wiki.read'].resolve(response({data:{page:{id:oldPage,title:'Stale page',html:'<p>'+oldPage+'</p>'}}}));
 delayed['question.ask'].resolve(returnToA
  ?response({error:{message:'Stale question failure'}},503)
  :response({publication:aReady,data:{answer:'Stale answer',unknowns:[],bounds:{truncated:false},citations:[oldPage]}}));
 delayed.search.resolve(response({publication:aReady,data:{hits:[{record:{'@id':oldPage,kind:'wiki',text:'Stale record'}}]}}));
 await Promise.all([refresh,page,ask,search]);
 a.displayed(currentAtlas,current,shown);
 assert.equal(a.get('panel-history').hidden,false);
 assert.equal(a.get('notice').textContent,notice);
 assert.equal(a.get('answer').textContent,answer);
 assert.equal(a.get('citations').children.length,0);
 assert.equal(a.get('ask-submit').disabled,true);
 assert.equal(a.get('ask-form').attributes['aria-busy'],'true');
 assert.equal(a.get('graph-status').textContent,graphStatus);
 assert.equal(a.get('graph-results').children.length,0);
 assert.equal(a.calls.some(call=>call.action==='history'&&call.body.publication==='obsolete'),false);
 newAsk.resolve(response({publication:current,data:{answer:'Current answer',unknowns:[],bounds:{truncated:false},citations:[shown]}}));
 await newQuestion;
 assert.match(a.get('answer').textContent,/Current answer/);
 assert.equal(a.get('ask-submit').disabled,false);
});

for(const uncertain of [false,true])test((uncertain?'uncertain':'admitted')+' A mutation retains its identity while B is open',async()=>{
 const first=deferred();
 let submitted=0;
 const a=atlasFixture({onRequest:(action)=>{
  if(action==='knowledge.revise'){
   submitted++;
   return submitted===1?first.promise:response({operation_id:'operation-a',state:'succeeded',result:{data:{status:'published'}}});
  }
  if(action==='operation.read')return response({operation_id:'operation-a',state:'succeeded',result:{data:{status:'published'}}});
 }});
 await turn();
 a.get('actor').value='Reviewer';
 a.get('clarification').value='Original evidence correction';
 const submitting=a.get('clarify-form').onsubmit();
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 const bNotice=a.get('notice').textContent;
 first.resolve(uncertain?response({error:{message:'Connection lost'}},503):response({operation_id:'operation-a',state:'running'}));
 await submitting;
 assert.equal(a.get('notice').textContent,bNotice);
 assert.equal(a.get('reconcile').hidden,true);
 assert.equal(a.get('operation').hidden,true);
 assert.equal(a.calls.some(call=>call.action==='operation.read'&&call.body.atlas_id===atlasB),false);
 await a.select(atlasA);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(a.get('reconcile').hidden,false);
 await a.get('reconcile').onclick();
 const mutations=a.calls.filter(call=>call.action==='knowledge.revise');
 assert.equal(mutations.length,uncertain?2:1);
 const original=mutations[0].body;
 assert.equal(original.atlas_id,atlasA);
 assert.match(original.request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
 assert.equal(original.expected_publication,aReady);
 assert.equal(original.payload.record_id,aPage);
 assert.equal(original.payload.instructions,'Original evidence correction');
 if(uncertain)assert.deepEqual(mutations[1].body,original);
 else assert.deepEqual(a.calls.filter(call=>call.action==='operation.read').map(call=>call.body),[
  {atlas_id:atlasA,payload:{operation_id:'operation-a'}},
 ]);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(a.get('reconcile').hidden,true);
});

test('two browser contexts open different projects independently',async()=>{
 const first=atlasFixture(),second=atlasFixture();
 await turn();
 await first.select(atlasB);
 first.displayed(atlasB,bReady,bPage);
 second.displayed(atlasA,aReady,aPage);
 await second.get('refresh').onclick();
 second.displayed(atlasA,aReady,aPage);
 assert.equal(second.calls.some(call=>call.body.atlas_id===atlasB),false);
});

test('discard confirmation protects a manually entered proposal identifier',async()=>{
 const confirmations=[];
 const a=atlasFixture({confirm:message=>{confirmations.push(message);return confirmations.length===2;}});
 await turn();
 assert.equal(a.get('candidate').value,'proposal-a');
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 await a.select(atlasA);
 assert.equal(confirmations.length,0);
 a.get('candidate').value='Unsent proposal identifier';
 a.get('candidate').oninput();
 await a.select(atlasB);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(a.get('candidate').value,'Unsent proposal identifier');
 assert.equal(confirmations.length,1);
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 assert.equal(a.get('candidate').value,'proposal-b');
 assert.equal(confirmations.length,2);
});

test('a definitive replay rejection after switching projects allows a fresh decision',async()=>{
 const newReady='d'.repeat(40);
 let advanced=false,submissions=0;
 const a=atlasFixture({onRequest:(action,body)=>{
  if(action==='knowledge.revise'){
   submissions++;
   if(submissions===1)return response({error:{message:'Connection lost'}},503);
   if(submissions===2)return response({error:{code:'stale_publication',message:'Publication changed'}},409);
   return response({operation_id:'new-operation',state:'succeeded',result:{data:{status:'published'}}});
  }
  if(advanced&&body.atlas_id===atlasA){
   if(action==='atlas.status')return response({publication:newReady,data:{candidates:['proposal-a']}});
   if(action==='history')return response({publication:newReady,data:{history:[{publication_commit:newReady}]}});
  }
 }});
 await turn();
 a.get('actor').value='Reviewer';
 a.get('clarification').value='Original evidence correction';
 await a.get('clarify-form').onsubmit();
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 assert.equal(a.get('reconcile').hidden,true);
 advanced=true;
 await a.select(atlasA);
 a.displayed(atlasA,newReady,aPage);
 assert.equal(a.get('reconcile').hidden,false);
 await a.get('reconcile').onclick();
 assert.match(a.get('notice').textContent,/publication changed/i);
 const mutations=()=>a.calls.filter(call=>call.action==='knowledge.revise').map(call=>call.body);
 const [first,replay]=mutations();
 assert.deepEqual(replay,first);
 assert.equal(first.atlas_id,atlasA);
 assert.equal(first.expected_publication,aReady);
 assert.equal(a.get('reconcile').hidden,true);

 a.get('actor').value='Reviewer';
 a.get('clarification').value='New correction against the updated publication';
 await a.get('clarify-form').onsubmit();
 const fresh=mutations()[2];
 assert.equal(fresh.atlas_id,atlasA);
 assert.notEqual(fresh.request_id,first.request_id);
 assert.equal(fresh.expected_publication,newReady);
 assert.equal(fresh.payload.instructions,'New correction against the updated publication');
 a.displayed(atlasA,newReady,aPage);
});

test('candidate inspection displays exact changed child receipts for review',async()=>{
 const changes={added:[],removed:[],changed:[{before:{mount:'field',publication:'old-receipt'},after:{mount:'field',publication:'new-receipt'}}]};
 const a=app(async()=>response({publication:'current',data:{before:{},snapshot:{},composition_diff:changes}}));
 a.run("selected=ready='current';");
 await a.run("inspect('layout-candidate')");
 const section=a.elements.get('review').children.find(item=>item.children[0]?.textContent==='Child layout changes');
 assert.ok(section);
 assert.deepEqual(JSON.parse(section.children[1].textContent),changes);
});

for(const editAfterSubmission of [false,true])test(
 'same-project replay rejection protects '+(editAfterSubmission?'newer edits':'the original form text'),
 async()=>{
  const confirmations=[];
  const original='Original clarification';
  const edited='Newer clarification typed during recovery';
  let submissions=0;
  const a=atlasFixture({
   confirm:message=>{confirmations.push(message);return confirmations.length===2;},
   onRequest:action=>{
    if(action!=='knowledge.revise')return;
    submissions++;
    if(submissions===1)return response({error:{message:'Connection lost'}},503);
    return response({error:{code:'stale_publication',message:'Publication changed'}},409);
   },
  });
  await turn();
  a.get('actor').value='Reviewer';
  a.get('clarification').value=original;
  await a.get('clarify-form').onsubmit();
  assert.equal(a.get('reconcile').hidden,false);
  if(editAfterSubmission)a.get('clarification').value=edited;
  await a.get('reconcile').onclick();
  assert.equal(a.get('reconcile').hidden,true);
  assert.match(a.get('notice').textContent,/publication changed/i);
  assert.equal(a.get('clarification').value,editAfterSubmission?edited:original);
  await a.select(atlasB);
  a.displayed(atlasA,aReady,aPage);
  assert.equal(a.get('clarification').value,editAfterSubmission?edited:original);
  assert.equal(confirmations.length,1);
  await a.select(atlasB);
  a.displayed(atlasB,bReady,bPage);
  assert.equal(a.get('clarification').value,'');
  assert.equal(confirmations.length,2);
 },
);

test('a replay rejection from an earlier A generation preserves new A edits',async()=>{
 const replay=deferred(),confirmations=[];
 let submissions=0;
 const a=atlasFixture({
  confirm:message=>{confirmations.push(message);return false;},
  onRequest:action=>{
   if(action!=='knowledge.revise')return;
   submissions++;
   if(submissions===1)return response({error:{message:'Connection lost'}},503);
   return replay.promise;
  },
 });
 await turn();
 a.get('actor').value='Original reviewer';
 a.get('clarification').value='Original clarification';
 await a.get('clarify-form').onsubmit();
 const recovery=a.get('reconcile').onclick();
 await a.select(atlasB);
 a.displayed(atlasB,bReady,bPage);
 await a.select(atlasA);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(confirmations.length,0);
 a.get('actor').value='Current reviewer';
 a.get('clarification').value='Current clarification';
 const notice=a.get('notice').textContent;
 replay.resolve(response({error:{code:'stale_publication',message:'Publication changed'}},409));
 await recovery;
 assert.equal(a.get('notice').textContent,notice);
 assert.equal(a.get('actor').value,'Current reviewer');
 assert.equal(a.get('clarification').value,'Current clarification');
 await a.select(atlasB);
 a.displayed(atlasA,aReady,aPage);
 assert.equal(confirmations.length,1);
 assert.equal(a.get('clarification').value,'Current clarification');
});
