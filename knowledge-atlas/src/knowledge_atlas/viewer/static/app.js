import {MutationQueue} from './requests.mjs';
import {GraphExplorer} from './graph.mjs';
const $=id=>document.getElementById(id);
const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
let selected=null,ready=null,pageId=null,targetId=null,inspected=null;
let viewEpoch=0,pageEpoch=0,inspectEpoch=0;
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
async function send(action,body={payload:{}}){
  const response=await fetch('/api/'+action,{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error?.message||'Request failed');error.code=result.error?.code;error.status=response.status;throw error;}
  return result;
}
const queue=new MutationQueue(send);
const graph=new GraphExplorer({document,send,onOpenPage:async id=>{const epoch=viewEpoch;if(await loadPage(id)&&epoch===viewEpoch)showPanel('read',true);}});
const read=(action,payload={})=>send(action,{payload,...(selected?{publication:selected}:{})});
function handle(fn){return async event=>{event?.preventDefault();try{await fn();}catch(error){notice(error.message,true);$('reconcile').hidden=!queue.pending;}};}
function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=handle(fn);return b;}
function resetReview(){inspectEpoch++;inspected=null;$('accept').disabled=true;$('reject').disabled=true;text('review-summary','Inspect a proposal against the current version before deciding.');}
function target(id){targetId=id;text('target',id?'About: '+(pageNames.get(id)||'Selected evidence record'):'Select a page in the current version to suggest a clarification.');}
function chooseOption(select,value,label){const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);}
function bindPageLinks(){
 const publication=selected;
 for(const link of $('page').querySelectorAll('a')){
  const href=link.getAttribute('href')||'';const id=pageLinks.get(href);
  if(id){link.onclick=handle(async()=>{if(publication!==selected)throw new Error('The selected version changed. Wait for its page to load.');await loadPage(id);showPanel('read',true);});}
  else if(href&&!/^(https?:|mailto:|#)/i.test(href)){
   link.onclick=handle(()=>{notice('This reference belongs to retained source material. See Sources & publication details for its provenance.');$('source-details').open=true;$('source-details').setAttribute('tabindex','-1');$('source-details').focus();});
  }else{link.rel='noopener noreferrer';link.target='_blank';}
 }
}
async function loadPage(id){
 const publication=selected,epoch=viewEpoch,request=++pageEpoch;const result=await read('wiki.read',{page_id:id});if(publication!==selected||epoch!==viewEpoch||request!==pageEpoch)return false;
 pageId=id;$('graph-page').disabled=false;target(id);text('page-title',result.data.page.title);text('page-id',id);$('page').innerHTML=result.data.page.html;
 bindPageLinks();
 for(const b of $('pages').children){b.classList.toggle('selected',b.dataset.id===id);b.setAttribute('aria-current',String(b.dataset.id===id));}
 return true;
}
async function loadPublication(){
 const epoch=++viewEpoch;graph.reset(selected);$('graph-page').disabled=true;resetReview();text('publication-id',selected||'No published version');for(const entry of $('timeline').children)entry.setAttribute('aria-current',String(entry.dataset.publication===selected));target(null);$('answer').replaceChildren();$('citations').replaceChildren();$('evidence').hidden=true;
 text('mode',selected===ready?'Latest version · changes can be proposed':'Earlier version · read only');$('return-current').hidden=!ready||selected===ready;text('compare-target','To: '+(versionNames.get(selected)||'selected version'));text('answer','Ask a question to explore this version.');text('diff','Choose an earlier version to compare.');
 $('propose').disabled=!selected||selected!==ready; $('pages').replaceChildren();
 if(!selected){text('page','No accepted publication yet. Import and review sources through your agent.');return;}
 const publication=selected,result=await read('wiki.pages');if(publication!==selected||epoch!==viewEpoch)return;
 const data=result.data;$('dates').replaceChildren();
 for(const value of ['Imported '+date(data.recorded_at),'Published '+date(data.accepted_at),data.acceptance_kind==='synthetic'?'Synthetic demo acceptance':(data.acceptance_kind||'Unknown acceptance')])$('dates').append(node('span',value));
 $('sources').replaceChildren();for(const source of data.sources||[]){$('sources').append(node('p',(source.name||'Source')+' · Source date: '+date(source.source_time)+' · Imported: '+date(source.recorded_at)),details('Source provenance',source));}
 pageNames=new Map(data.pages.map(page=>[page.id,page.title]));pageLinks=new Map();for(const page of data.pages){if(pageLinks.has(page.path))pageLinks.set(page.path,null);else pageLinks.set(page.path,page.id);}text('page-count',String(data.pages.length));
 for(const page of data.pages){const b=button(page.title,async()=>{await loadPage(page.id);showPanel('read');});b.dataset.id=page.id;$('pages').append(b);}
 filterPages();
 if(data.pages.length)await loadPage(data.pages.some(p=>p.id===pageId)?pageId:data.pages[0].id);
 else text('page','This publication has no visible wiki pages.');
 notice((versionNames.get(selected)||'Selected version')+' · questions and pages use this version.');
}
async function refresh(){
 const epoch=viewEpoch,previous=selected;const status=await send('atlas.status');
 const freshReady=status.publication;
 const history=freshReady?(await send('history',{payload:{},publication:freshReady})).data.history:[];
 if(epoch!==viewEpoch)return;ready=freshReady;
 $('publication').replaceChildren();$('before').replaceChildren();$('timeline').replaceChildren();
 versionNames=new Map();
 for(const [index,entry] of history.entries()){const oid=entry.publication_commit;const label='Version '+(history.length-index)+(oid===freshReady?' · Latest':'')+' · '+date(entry.accepted_at);versionNames.set(oid,label);
  chooseOption($('publication'),oid,label);chooseOption($('before'),oid,label);
  const b=button(label+'\n'+(entry.acceptance_kind==='synthetic'?'Synthetic demo acceptance':entry.acceptance_kind||'Unknown acceptance'),async()=>{selected=oid;$('publication').value=oid;await loadPublication();showPanel('read',true);});b.dataset.publication=oid;b.setAttribute('aria-current',String(oid===selected));$('timeline').append(b);}

 selected=history.some(x=>x.publication_commit===previous)?previous:ready;$('publication').value=selected||'';
 if(history.length>1)$('before').value=history[1].publication_commit;
 await loadPublication();
 const candidates=status.data.candidates||[];$('proposal-list').replaceChildren();chooseOption($('proposal-list'),'','Choose a saved proposal…');
 for(const [index,candidate] of candidates.entries())chooseOption($('proposal-list'),candidate,'Proposal '+(index+1)+' · '+candidate.slice(0,8));
 if(!$('candidate').value&&candidates.length)$('candidate').value=candidates.at(-1);
 $('proposal-list').value=candidates.includes($('candidate').value)?$('candidate').value:'';
}
async function inspect(candidate){
 resetReview();const epoch=viewEpoch,request=++inspectEpoch;const result=await send('candidate.inspect',{payload:{candidate}});
 if(epoch!==viewEpoch||request!==inspectEpoch)return;
 if(result.publication!==ready){await refresh();throw new Error('Publication changed. Inspect again against the refreshed current version.');}
 inspected={candidate,base:result.publication};$('candidate').value=candidate;$('proposal-list').value=candidate;
 showPanel('review');const box=$('review');renderChanges(box,wikiChanges(result.data.before,result.data.snapshot),'wiki');
 text('review-summary',(result.data.unresolved?.length||0)+' unresolved assertions · '+(result.data.type_questions?.length||0)+' type questions. Review these uncertainties before accepting.');
 box.append(details('Complete before snapshot & evidence',result.data.before),details('Complete proposed snapshot & evidence',result.data.snapshot),details('Unresolved assertions & decisions',{unresolved:result.data.unresolved,decisions:result.data.decisions}));
 $('types').replaceChildren();
 for(const question of result.data.type_questions||[]){
  const group=document.createElement('div');const pre=document.createElement('pre');pre.textContent=JSON.stringify(question,null,2);group.append(pre);
  const reuse=document.createElement('input');reuse.placeholder='Existing type ID for reuse';group.append(reuse);
  for(const action of ['accept','reuse','reject'])group.append(button(action+' type',()=>{
   const payload={candidate:inspected.candidate,decision_id:question.id,action,actor:actor(),reason:reason(),recorded_at:new Date().toISOString()};
   if(action==='reuse')payload.type_id=reuse.value;
   return mutate('type.decide',payload);
  }));$('types').append(group);
 }
 $('accept').disabled=selected!==ready;$('reject').disabled=selected!==ready;
 notice('Candidate inspected. Check proposed content, evidence and unresolved types before deciding.');
}
function actor(){const value=$('actor').value.trim();if(!value)throw new Error('Enter your name for recorded attribution.');return value;}
function reason(){const value=$('reason').value.trim();if(!value)throw new Error('Enter a decision reason.');return value;}
async function observe(operation){
 $('operation').hidden=false;$('reconcile').hidden=false;
 while(operation.operation_id&&['queued','running'].includes(operation.state)){
  text('operation',operation);notice('Your change is '+operation.state+'. You can keep reading; publication changes only after explicit acceptance.');
  await new Promise(resolve=>setTimeout(resolve,1000));operation=await queue.reconcile();
 }
 text('operation',operation);
 if(operation.state==='failed'||operation.state==='interrupted'){
  queue.finish();resetReview();$('reconcile').hidden=true;throw new Error(JSON.stringify(operation.error||{state:operation.state}));
 }
 const result=operation.result||operation;queue.finish();$('reconcile').hidden=true;resetReview();
 if(result.data?.status==='accepted_pending_publication'){
  notice('Accepted, but publication failed. Previous READY remains active. Inspect operation details; use the agent to resume publication explicitly.',true);return;
 }
 if(result.publication)selected=result.publication;
 await refresh();
 if(result.data?.candidate&&result.data?.status!=='published'&&result.data?.status!=='rejected')await inspect(result.data.candidate);
 else notice('Operation completed: '+(result.data?.status||'success'));
}
async function mutate(action,payload){
 if(selected!==ready)throw new Error('Historical publications are read-only.');
 const expected=inspected?.base??ready;
 let operation;
 try{operation=await queue.begin(action,payload,expected);}
 catch(error){
  if(error.status>=400&&error.status<500){queue.finish();resetReview();}
  throw error;
 }
 await observe(operation);
}
for(const panel of panels)$('nav-'+panel).onclick=()=>showPanel(panel);
$('page-filter').oninput=filterPages;
$('graph-page').onclick=handle(async()=>{if(!pageId||!selected)return;showPanel('graph');await graph.open(pageId);});
$('ask-page').onclick=()=>{showPanel('ask');$('question').focus();};
$('clarify-page').onclick=()=>{showPanel('review');$('clarification').focus();};
$('return-current').onclick=handle(async()=>{selected=ready;$('publication').value=ready;await loadPublication();});
$('proposal-list').onchange=()=>{$('candidate').value=$('proposal-list').value;resetReview();};
$('candidate').oninput=resetReview;
$('refresh').onclick=handle(refresh);
$('publication').onchange=handle(async()=>{selected=$('publication').value;await loadPublication();});
$('compare').onclick=handle(async()=>{const publication=selected,epoch=viewEpoch;const result=await read('diff',{before:$('before').value,after:publication});if(publication===selected&&epoch===viewEpoch)renderChanges($('diff'),result.data.changes);});
$('ask-form').onsubmit=handle(async()=>{
 const publication=selected,epoch=viewEpoch;if(!publication)throw new Error('Choose a published version before asking a question.');notice('Finding evidence and preparing your answer…');$('ask-submit').disabled=true;$('ask-form').setAttribute('aria-busy','true');text('ask-submit','Preparing answer…');
 let result;try{result=await read('question.ask',{question:$('question').value,count:5});}finally{$('ask-submit').disabled=false;$('ask-form').setAttribute('aria-busy','false');text('ask-submit','Ask with evidence ↗');}if(publication!==selected||epoch!==viewEpoch)return;
 text('answer',result.data.answer+'\n\nUnknowns: '+(result.data.unknowns.join('; ')||'None reported')+'\nEvidence truncated: '+result.data.bounds.truncated);
 $('citations').replaceChildren();
 for(const [index,id] of result.data.citations.entries())$('citations').append(button('Source '+(index+1),async()=>{
  if(publication!==selected||epoch!==viewEpoch)return;
  const result=await send('evidence.read',{payload:{record_id:id},publication});
  if(publication!==selected||epoch!==viewEpoch)return;
  $('evidence').replaceChildren(node('h3','Source '+(index+1)),node('div',recordText(result.data.record||result.data),'change-text'),details('Complete evidence & provenance',result.data),button('Show in graph',async()=>{if(publication!==selected||epoch!==viewEpoch)return;showPanel('graph',true);await graph.open(id);}));$('evidence').hidden=false;target(id);$('evidence').focus();
 }));notice('Answer grounded in '+(versionNames.get(result.publication)||'the selected version')+'.');
});
$('clarify-form').onsubmit=handle(async()=>{
 if(!targetId)throw new Error('Select a page or evidence record.');
 await mutate('knowledge.revise',{record_id:targetId,actor:actor(),kind:'human',reason:$('clarification').value,
 instructions:$('clarification').value,recorded_at:new Date().toISOString()});
});
$('inspect').onclick=handle(()=>inspect($('candidate').value.trim()));
for(const action of ['accept','reject'])$(action).onclick=handle(async()=>{
 if(!inspected)throw new Error('Inspect the candidate first.');
 await mutate('candidate.decide',{candidate:inspected.candidate,action,actor:actor(),kind:'human',reason:reason(),decided_at:new Date().toISOString()});
});
$('reconcile').onclick=handle(async()=>{await observe(await queue.reconcile());});
window.addEventListener('beforeunload',event=>{if(queue.pending){event.preventDefault();event.returnValue='';}});
if(!token)notice('Open the complete launch URL printed by the viewer. Reloading clears the in-memory session secret.',true);
else handle(refresh)();
