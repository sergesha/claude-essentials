import {MutationQueue} from './requests.mjs';
import {GraphExplorer} from './graph.mjs';
const $=id=>document.getElementById(id);
const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
let selected=null,ready=null,pageId=null,targetId=null,inspected=null;
let project={atlasId:null,generation:0};
let viewEpoch=0,pageEpoch=0,inspectEpoch=0;
let fieldBaselines=new WeakMap();
const isCurrent=scope=>scope.atlasId===project.atlasId&&scope.generation===project.generation;
let pageNames=new Map(),versionNames=new Map(),pageLinks=new Map();
const text=(id,value)=>{$(id).textContent=typeof value==='string'?value:JSON.stringify(value,null,2);};
const notice=(value,error=false)=>{text('notice',value);$('notice').classList.toggle('error',error);};
const panels=['read','ask','graph','history','review'];
function showPanel(name,moveFocus=false){
 if(!panels.includes(name))return;
 for(const panel of panels){$('panel-'+panel).hidden=panel!==name;$('nav-'+panel).setAttribute('aria-pressed',String(panel===name));}
 if(name==='graph')graph.resize();
 if(moveFocus){$('panel-'+name).setAttribute('tabindex','-1');$('panel-'+name).focus();}
}
function filterPages(){
 const query=$('page-filter').value.trim().toLocaleLowerCase();let visible=0;
 for(const node of $('pages').children){node.hidden=!node.textContent.toLocaleLowerCase().includes(query);if(!node.hidden)visible++;}
 $('page-empty').hidden=visible>0||!query;
}
function node(tag,content,className){const element=document.createElement(tag);if(content!==undefined)element.textContent=content;if(className)element.className=className;return element;}
function date(value){if(!value)return 'Unknown date';const parsed=new Date(value);return Number.isNaN(parsed.getTime())?String(value):parsed.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});}
function details(label,value){const element=node('details',undefined,'technical');element.append(node('summary',label),node('pre',JSON.stringify(value,null,2)));return element;}
function recordText(record){return record?.text??record?.content??(record?JSON.stringify(record,null,2):'Not present in this version');}
function renderChanges(box,changes,scope='records'){
 box.replaceChildren();
 if(!changes?.length){box.append(node('p',scope==='wiki'?'No wiki page text changes. Other knowledge or evidence may still differ; inspect the complete snapshots below.':'No record changes between these versions.','empty-note'));return;}
 box.append(node('p',changes.length+(scope==='wiki'?' changed wiki pages · other snapshot material remains below':' changed records · expand an item to compare'),'help-text'));
 const otherRecords=node('details',undefined,'technical');otherRecords.append(node('summary','Other structured records & provenance changes'));let otherCount=0;
 const ordered=[...changes].sort((a,b)=>Number(b.after?.kind==='wiki'||b.before?.kind==='wiki')-Number(a.after?.kind==='wiki'||a.before?.kind==='wiki'));
 for(const change of ordered){
  const item=node('details',undefined,'change-card');const record=change.after||change.before;const kind=!change.before?'Added':!change.after?'Removed':'Updated';
  item.append(node('summary',kind+' · '+(record?.title||record?.kind||'Record')+' · '+change.id));
  const columns=node('div',undefined,'change-columns');
  for(const [label,value] of [['Before',change.before],['After',change.after]]){const column=node('div',undefined,'change-column');column.append(node('h4',label),node('div',recordText(value),'change-text'));if(value?.content_truncated)column.append(node('p','Content is truncated; inspect complete source evidence before deciding.','change-warning'));columns.append(column);}
  item.append(columns,details('Exact record & provenance',change));if(scope==='records'&&record?.kind!=='wiki'){otherRecords.append(item);otherCount++;}else box.append(item);
 }
 if(otherCount){otherRecords.children[0].textContent=otherCount+' other structured records & provenance changes';box.append(otherRecords);}
}
function wikiChanges(before,after){
 const rows=snapshot=>new Map((snapshot?.wiki?.pages||[]).map(p=>[(p.path||'')+(p.filename||p.id),{text:p.content,title:p.title,kind:'wiki',content_truncated:p.content_truncated}]));
 const old=rows(before),next=rows(after);return [...new Set([...old.keys(),...next.keys()])].filter(key=>JSON.stringify(old.get(key))!==JSON.stringify(next.get(key))).map(key=>({id:key,before:old.get(key)||null,after:next.get(key)||null}));
}
async function post(action,body){
  const response=await fetch('/api/'+action,{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error?.message||'Request failed');error.code=result.error?.code;error.status=response.status;throw error;}
  return result;
}
const send=(action,body={payload:{}},scope=project)=>post(action,{...body,atlas_id:scope.atlasId});
const queue=new MutationQueue(post);
const graph=new GraphExplorer({document,send,onOpenPage:async id=>{
 const scope=project,epoch=viewEpoch;
 if(await loadPage(id)&&isCurrent(scope)&&epoch===viewEpoch)showPanel('read',true);
}});
const read=(action,payload={},scope=project)=>send(action,{payload,...(selected?{publication:selected}:{})},scope);
function syncOperationControls(){$('reconcile').hidden=!queue.get(project.atlasId);}
function handle(fn){
 return async event=>{
  event?.preventDefault();
  const scope=project;
  try{await fn();}
  catch(error){
   if(isCurrent(scope)){notice(error.message,true);syncOperationControls();}
  }
 };
}
function button(label,fn){
 const b=document.createElement('button'),scope=project;
 b.textContent=label;
 b.onclick=handle(()=>{if(isCurrent(scope))return fn();});
 return b;
}
function resetReview(){inspectEpoch++;inspected=null;$('accept').disabled=true;$('reject').disabled=true;text('review-summary','Inspect a proposal against the current version before deciding.');}
function target(id){targetId=id;text('target',id?'About: '+(pageNames.get(id)||'Selected evidence record'):'Select a page in the current version to suggest a clarification.');}
function chooseOption(select,value,label){const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);}
function bindPageLinks(){
 const publication=selected,scope=project,epoch=viewEpoch;
 const active=()=>isCurrent(scope)&&publication===selected&&epoch===viewEpoch;
 for(const link of $('page').querySelectorAll('a')){
  const href=link.getAttribute('href')||'';const id=pageLinks.get(href);
  if(id){link.onclick=handle(async()=>{if(active()&&await loadPage(id)&&active())showPanel('read',true);});}
  else if(href&&!/^(https?:|mailto:|#)/i.test(href)){
   link.onclick=handle(()=>{if(!active())return;notice('This reference belongs to retained source material. See Sources & publication details for its provenance.');$('source-details').open=true;$('source-details').setAttribute('tabindex','-1');$('source-details').focus();});
  }else{link.rel='noopener noreferrer';link.target='_blank';}
 }
}
async function loadPage(id){
 const scope=project,publication=selected,epoch=viewEpoch,request=++pageEpoch;
 const result=await read('wiki.read',{page_id:id},scope);
 if(!isCurrent(scope)||publication!==selected||epoch!==viewEpoch||request!==pageEpoch)return false;
 pageId=id;$('graph-page').disabled=false;target(id);text('page-title',result.data.page.title);text('page-id',id);$('page').innerHTML=result.data.page.html;
 bindPageLinks();
 for(const b of $('pages').children){b.classList.toggle('selected',b.dataset.id===id);b.setAttribute('aria-current',String(b.dataset.id===id));}
 return true;
}
function attribution(data){
 const kind=data.acceptance_kind==='synthetic'?'Synthetic demo acceptance':(data.acceptance_kind||'Unknown acceptance');
 const receipt=data.matching_decision_receipt?'Matching decision receipt present':'No matching decision receipt';
 return kind+' (caller-supplied) · Channel: '+(data.acceptance_channel||'unrecorded')+' · '+receipt;
}
function setAskBusy(busy){
 $('ask-submit').disabled=busy;
 $('ask-form').setAttribute('aria-busy',String(busy));
 text('ask-submit',busy?'Preparing answer…':'Ask with evidence ↗');
}
function draftFields(){
 return [$('actor'),$('clarification'),$('reason'),$('question'),$('candidate'),...$('types').querySelectorAll('input')];
}
function setCandidate(value){
 const field=$('candidate');
 field.value=value;
 fieldBaselines.set(field,value);
}
function hasDraft(){
 return draftFields().some(field=>field.value.trim()&&field.value!==fieldBaselines.get(field));
}
function resetProjectView(){
 selected=ready=pageId=null;
 viewEpoch++;pageEpoch++;
 pageNames=new Map();versionNames=new Map();pageLinks=new Map();
 for(const id of ['actor','clarification','reason','question','candidate','page-filter'])$(id).value='';
 for(const id of ['publication','before','timeline','proposal-list','dates','sources','pages','review','types','citations','evidence','diff'])$(id).replaceChildren();
 fieldBaselines=new WeakMap();
 target(null);resetReview();graph.reset(null);setAskBusy(false);filterPages();
 $('graph-page').disabled=true;$('propose').disabled=true;$('compare').disabled=true;
 $('return-current').hidden=true;$('evidence').hidden=true;$('source-details').open=false;
 for(const id of ['operation','page-id','publication-id','compare-target','answer'])text(id,'');
 $('operation').hidden=true;$('reconcile').disabled=false;syncOperationControls();
 text('page-count','0');text('mode','Loading project…');text('page-title','Your published knowledge');text('page','Loading project…');
 showPanel('read');notice('Loading the selected project…');
}
async function selectProject(atlasId){
 const scope=project,chooser=$('atlas-project');
 chooser.value=scope.atlasId||'';
 if(!atlasId||atlasId===scope.atlasId||chooser.disabled)return;
 chooser.disabled=true;
 let opened;
 try{opened=await post('atlas.open',{atlas_id:atlasId});}
 finally{if(isCurrent(scope))chooser.disabled=false;}
 if(!isCurrent(scope))return;
 if(opened.data?.atlas_id!==atlasId)throw new Error('The service returned a different project UUID.');
 if(scope.atlasId&&hasDraft()&&!window.confirm('Discard unsent text and switch projects?'))return;
 project={atlasId,generation:scope.generation+1};
 chooser.value=atlasId;
 resetProjectView();
 await handle(refresh)();
}
async function initialize(){
 const scope=project,result=await post('atlas.list',{});
 if(!isCurrent(scope))return;
 const chooser=$('atlas-project');
 chooser.replaceChildren();
 chooseOption(chooser,'','Choose a project…');
 for(const row of result.data.projects)chooseOption(chooser,row.atlas_id,row.name+' · '+row.atlas_id+(row.status==='unavailable'?' · Unavailable':''));
 chooser.disabled=false;
 await selectProject(result.launch_atlas_id);
}
async function loadPublication(){
 const scope=project,epoch=++viewEpoch;
 const active=()=>isCurrent(scope)&&epoch===viewEpoch;
 graph.reset(selected);setAskBusy(false);$('graph-page').disabled=true;resetReview();text('publication-id',selected||'No published version');for(const entry of $('timeline').children)entry.setAttribute('aria-current',String(entry.dataset.publication===selected));target(null);$('answer').replaceChildren();$('citations').replaceChildren();$('evidence').hidden=true;
 text('mode',!selected?'No published version':selected===ready?'Latest version · changes can be proposed':'Earlier version · read only');$('return-current').hidden=!ready||selected===ready;text('compare-target','To: '+(versionNames.get(selected)||'selected version'));text('answer','Ask a question to explore this version.');text('diff','Choose an earlier version to compare.');
 $('propose').disabled=!selected||selected!==ready;
 $('compare').disabled=!selected;
 $('pages').replaceChildren();
 if(!selected){
  text('page','No accepted publication yet. Import and review sources through your agent.');
  notice('No accepted publication yet.');
  return true;
 }
 const result=await read('wiki.pages',{},scope);
 if(!active())return false;
 const data=result.data;$('dates').replaceChildren();
 const dates=['Imported '+date(data.recorded_at),'Accepted '+date(data.effective_accepted_at||data.accepted_at)];
 if(data.effective_accepted_at)dates.push('Caller-recorded acceptance '+date(data.accepted_at));
 for(const value of [...dates,attribution(data)])$('dates').append(node('span',value));
 $('sources').replaceChildren();for(const source of data.sources||[]){$('sources').append(node('p',(source.name||'Source')+' · Source date: '+date(source.source_time)+' · Imported: '+date(source.recorded_at)),details('Source provenance',source));}
 pageNames=new Map(data.pages.map(page=>[page.id,page.title]));pageLinks=new Map();for(const page of data.pages){if(pageLinks.has(page.path))pageLinks.set(page.path,null);else pageLinks.set(page.path,page.id);}text('page-count',String(data.pages.length));
 for(const page of data.pages){const b=button(page.title,async()=>{if(await loadPage(page.id)&&active())showPanel('read');});b.dataset.id=page.id;$('pages').append(b);}
 filterPages();
 if(data.pages.length)await loadPage(data.pages.some(p=>p.id===pageId)?pageId:data.pages[0].id);
 else text('page','This publication has no visible wiki pages.');
 if(!active())return false;
 notice((versionNames.get(selected)||'Selected version')+' · questions and pages use this version.');
 return true;
}
async function refresh(){
 const scope=project,epoch=viewEpoch,previous=selected;
 if(!scope.atlasId)return;
 const active=()=>isCurrent(scope)&&epoch===viewEpoch;
 const status=await send('atlas.status',{payload:{}},scope);
 if(!active())return;
 const freshReady=status.publication;
 const history=freshReady?(await send('history',{payload:{},publication:freshReady},scope)).data.history:[];
 if(!active())return;
 ready=freshReady;
 $('publication').replaceChildren();$('before').replaceChildren();$('timeline').replaceChildren();
 versionNames=new Map();
 for(const [index,entry] of history.entries()){const oid=entry.publication_commit;const label='Version '+(history.length-index)+(oid===freshReady?' · Latest':'')+' · '+date(entry.effective_accepted_at||entry.accepted_at);versionNames.set(oid,label);
  chooseOption($('publication'),oid,label);chooseOption($('before'),oid,label);
  const b=button(label+'\n'+attribution(entry),async()=>{selected=oid;$('publication').value=oid;if(await loadPublication()&&isCurrent(scope))showPanel('read',true);});b.dataset.publication=oid;b.setAttribute('aria-current',String(oid===selected));$('timeline').append(b);}

 selected=history.some(x=>x.publication_commit===previous)?previous:ready;$('publication').value=selected||'';
 if(history.length>1)$('before').value=history[1].publication_commit;
 const loaded=loadPublication(),publicationEpoch=viewEpoch;
 if(!await loaded||!isCurrent(scope)||publicationEpoch!==viewEpoch)return;
 const candidates=status.data.candidates||[];$('proposal-list').replaceChildren();chooseOption($('proposal-list'),'','Choose a saved proposal…');
 for(const [index,candidate] of candidates.entries())chooseOption($('proposal-list'),candidate,'Proposal '+(index+1)+' · '+candidate.slice(0,8)+' · '+(status.data.candidate_statuses?.[candidate]||'status unavailable'));
 if(!$('candidate').value&&candidates.length)setCandidate(candidates.at(-1));
 $('proposal-list').value=candidates.includes($('candidate').value)?$('candidate').value:'';
}
async function inspect(candidate){
 resetReview();const scope=project,epoch=viewEpoch,request=++inspectEpoch;
 const result=await send('candidate.inspect',{payload:{candidate}},scope);
 if(!isCurrent(scope)||epoch!==viewEpoch||request!==inspectEpoch)return;
 if(result.publication!==ready){
  await refresh();
  if(isCurrent(scope))throw new Error('Publication changed. Inspect again against the refreshed current version.');
  return;
 }
 inspected={candidate,base:result.publication};setCandidate(candidate);$('proposal-list').value=candidate;
 showPanel('review');const box=$('review');renderChanges(box,wikiChanges(result.data.before,result.data.snapshot),'wiki');
 text('review-summary',(result.data.unresolved?.length||0)+' unresolved assertions · '+(result.data.type_questions?.length||0)+' type questions. Review these uncertainties before accepting.');
 const layout=result.data.composition_diff;
 if(layout&&Object.values(layout).some(items=>items.length))box.append(details('Child layout changes',layout));
 box.append(details('Complete before snapshot & evidence',result.data.before),details('Complete proposed snapshot & evidence',result.data.snapshot),details('Unresolved assertions & decisions',{unresolved:result.data.unresolved,decisions:result.data.decisions}));
 $('types').replaceChildren();
 for(const question of result.data.type_questions||[]){
  const group=document.createElement('div');const pre=document.createElement('pre');pre.textContent=JSON.stringify(question,null,2);group.append(pre);
  const reuse=document.createElement('input');reuse.placeholder='Existing type ID for reuse';group.append(reuse);
  for(const action of ['accept','reuse','reject'])group.append(button(action+' type',()=>{
   const payload={candidate:inspected.candidate,decision_id:question.id,action,actor:actor(),reason:reason(),recorded_at:new Date().toISOString()};
   if(action==='reuse')payload.type_id=reuse.value;
   return mutate('type.decide',payload,[$('actor'),$('reason'),...(action==='reuse'?[reuse]:[])]);
  }));$('types').append(group);
 }
 $('accept').disabled=selected!==ready;$('reject').disabled=selected!==ready;
 notice('Candidate inspected. Check proposed content, evidence and unresolved types before deciding.');
}
function actor(){const value=$('actor').value.trim();if(!value)throw new Error('Enter your name for recorded attribution.');return value;}
function reason(){const value=$('reason').value.trim();if(!value)throw new Error('Enter a decision reason.');return value;}
async function observe(operation,scope){
 if(!isCurrent(scope))return;
 $('operation').hidden=false;
 while(operation.operation_id&&['queued','running'].includes(operation.state)){
  text('operation',operation);notice('Your change is '+operation.state+'. You can keep reading; publication changes only after explicit acceptance.');
  await new Promise(resolve=>setTimeout(resolve,1000));
  if(!isCurrent(scope))return;
  operation=await queue.reconcile(scope.atlasId);
  if(!isCurrent(scope))return;
 }
 text('operation',operation);
 queue.finish(scope.atlasId);syncOperationControls();resetReview();
 if(operation.state==='failed'||operation.state==='interrupted'){
  throw new Error(JSON.stringify(operation.error||{state:operation.state}));
 }
 const result=operation.result||operation;
 if(result.data?.status==='accepted_pending_publication'){
  notice('Accepted, but publication failed. Previous READY remains active. Inspect operation details; use the agent to resume publication explicitly.',true);return;
 }
 if(result.publication)selected=result.publication;
 await refresh();
 if(!isCurrent(scope))return;
 if(result.data?.candidate&&result.data?.status!=='published'&&result.data?.status!=='rejected')await inspect(result.data.candidate);
 else notice('Operation completed: '+(result.data?.status||'success'));
}
async function followOperation(request,scope){
 $('reconcile').disabled=true;
 syncOperationControls();
 try{await observe(await request,scope);}
 finally{
  if(isCurrent(scope)){$('reconcile').disabled=false;syncOperationControls();}
 }
}
async function mutate(action,payload,fields=[]){
 const scope=project;
 if(!ready||selected!==ready)throw new Error('Historical publications are read-only.');
 if(queue.get(scope.atlasId))throw new Error('Reconcile the pending operation before another decision.');
 const expected=inspected?.base??ready;
 const previous=fields.map(field=>fieldBaselines.get(field));
 for(const field of fields)fieldBaselines.set(field,field.value);
 try{
  await followOperation(queue.begin(scope.atlasId,action,payload,expected),scope);
 }catch(error){
  if(isCurrent(scope)&&!queue.get(scope.atlasId)&&error.status>=400&&error.status<500){
   for(const [index,field] of fields.entries())fieldBaselines.set(field,previous[index]);
   resetReview();
  }
  throw error;
 }
}
for(const panel of panels)$('nav-'+panel).onclick=()=>showPanel(panel);
$('atlas-project').onchange=handle(()=>selectProject($('atlas-project').value));
$('page-filter').oninput=filterPages;
$('graph-page').onclick=handle(async()=>{if(!pageId||!selected)return;showPanel('graph');await graph.open(pageId);});
$('ask-page').onclick=()=>{showPanel('ask');$('question').focus();};
$('clarify-page').onclick=()=>{showPanel('review');$('clarification').focus();};
$('return-current').onclick=handle(async()=>{selected=ready;$('publication').value=ready;await loadPublication();});
$('proposal-list').onchange=()=>{setCandidate($('proposal-list').value);resetReview();};
$('candidate').oninput=resetReview;
$('refresh').onclick=handle(refresh);
$('publication').onchange=handle(async()=>{selected=$('publication').value;await loadPublication();});
$('compare').onclick=handle(async()=>{
 const scope=project,publication=selected,epoch=viewEpoch;
 if(!publication)return;
 const result=await read('diff',{before:$('before').value,after:publication},scope);
 if(isCurrent(scope)&&publication===selected&&epoch===viewEpoch)renderChanges($('diff'),result.data.changes);
});
$('ask-form').onsubmit=handle(async()=>{
 const scope=project,publication=selected,epoch=viewEpoch;
 const active=()=>isCurrent(scope)&&publication===selected&&epoch===viewEpoch;
 if(!publication)throw new Error('Choose a published version before asking a question.');
 notice('Finding evidence and preparing your answer…');setAskBusy(true);
 const question=$('question');fieldBaselines.set(question,question.value);
 let result;
 try{result=await read('question.ask',{question:question.value,count:5},scope);}
 finally{if(active())setAskBusy(false);}
 if(!active())return;
 text('answer',(result.data.status==='insufficient_evidence'?'Insufficient evidence\n':result.data.coverage==='partial'?'Partial answer\n':'')+result.data.answer+'\n\nUnknowns: '+(result.data.unknowns.join('; ')||'None reported')+'\nEvidence truncated: '+result.data.bounds.truncated);
 $('citations').replaceChildren();
 for(const [index,id] of result.data.citations.entries())$('citations').append(button('Source '+(index+1),async()=>{
  if(!active())return;
  const result=await send('evidence.read',{payload:{record_id:id},publication},scope);
  if(!active())return;
  $('evidence').replaceChildren(node('h3','Source '+(index+1)),node('div',recordText(result.data.record||result.data),'change-text'),details('Complete evidence & provenance',result.data),button('Show in graph',async()=>{if(!active())return;showPanel('graph',true);await graph.open(id);}));$('evidence').hidden=false;target(id);$('evidence').focus();
 }));notice((result.data.status==='insufficient_evidence'?'Insufficient evidence in ':'Answer grounded in ')+(versionNames.get(result.publication)||'the selected version')+'.');
});
$('clarify-form').onsubmit=handle(async()=>{
 if(!targetId)throw new Error('Select a page or evidence record.');
 await mutate('knowledge.revise',{record_id:targetId,actor:actor(),kind:'human',reason:$('clarification').value,
 instructions:$('clarification').value,recorded_at:new Date().toISOString()},[$('actor'),$('clarification')]);
});
$('inspect').onclick=handle(()=>inspect($('candidate').value.trim()));
for(const action of ['accept','reject'])$(action).onclick=handle(async()=>{
 if(!inspected)throw new Error('Inspect the candidate first.');
 await mutate('candidate.decide',{candidate:inspected.candidate,action,actor:actor(),kind:'human',reason:reason(),decided_at:new Date().toISOString()},[$('actor'),$('reason')]);
});
$('reconcile').onclick=handle(async()=>{
 const scope=project;
 if($('reconcile').disabled)return;
 await followOperation(queue.reconcile(scope.atlasId),scope);
});
window.addEventListener('beforeunload',event=>{if(queue.hasPending){event.preventDefault();event.returnValue='';}});
if(!token)notice('Open the complete launch URL printed by the viewer. Reloading clears the in-memory session secret.',true);
else handle(initialize)();
