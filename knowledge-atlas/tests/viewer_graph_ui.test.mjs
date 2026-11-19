import {test} from 'node:test';
import assert from 'node:assert/strict';
let GraphExplorer;try{({GraphExplorer}=await import('../src/knowledge_atlas/viewer/static/graph.mjs'));}catch(e){if(e.code!=='ERR_MODULE_NOT_FOUND')throw e;}
function dom(){
 const elements=new Map();const createElement=tag=>({tag,children:[],value:'',checked:true,hidden:false,dataset:{},attributes:{},textContent:'',append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;},setAttribute(k,v){this.attributes[k]=v;},focus(){this.focused=true;},classList:{toggle(){}}});
 return {createElement,getElementById(id){if(!elements.has(id))elements.set(id,createElement('div'));return elements.get(id);}};
}
const record=(id,kind='entity')=>({'@id':id,kind,text:id,payload:'{}',links:[],status:'accepted'});
const result=(id,p='p')=>({publication:p,data:{publication_commit:p,record:record(id),expansion:{publication_commit:p,graph_commit:'g',records:[record(id)],edges:[]}}});
function setup(send){assert.ok(GraphExplorer,'graph controller must exist');const document=dom();const g=new GraphExplorer({document,send,onOpenPage:()=>{}});g.reset('p');return {g,document};}
test('late neighborhood cannot populate a different publication',async()=>{
 let finish;const {g}=setup(()=>new Promise(r=>finish=r));const pending=g.open('old');g.reset('new');finish(result('old'));await pending;
 assert.equal(g.model.records.size,0);assert.equal(g.model.publication,'new');
});
test('late search cannot populate results after version changes',async()=>{
 let finish;const {g,document}=setup(()=>new Promise(r=>finish=r));const pending=g.search('term');g.reset('new');finish({publication:'p',data:{hits:[{record:record('old')}]}});await pending;
 assert.equal(document.getElementById('graph-results').children.length,0);
});
test('edge inspector fetches original evidence ID and ignores older selections',async()=>{
 const calls=[];let finish;const {g,document}=setup((action,body)=>{calls.push(body);return new Promise(r=>finish=r);});
 const edge={id:'Record/claim',from:'a',to:'b',type:'uses',representation:'controlled_relation'};
 const pending=g.inspect(edge.id,edge);g.reset('new');finish(result('Record/claim'));await pending;
 assert.equal(calls[0].payload.record_id,'Record/claim');assert.equal(calls[0].publication,'p');
 assert.equal(document.getElementById('graph-inspector').children.some(x=>x.textContent==='Record/claim'),false);
});
test('missing evidence is reported and unsafe labels remain plain text',async()=>{
 const {g,document}=setup(async()=>({publication:'p',data:null}));await g.inspect('missing');
 assert.match(document.getElementById('graph-inspector').children.map(x=>x.textContent).join(' '),/unavailable/i);
});
test('failed wiki navigation reports an error instead of rejecting an event',async()=>{
 const {g,document}=setup(async()=>result('wiki'));const b=g.button('Read wiki',async()=>{throw Error('Backend offline');});
 await b.onclick();assert.match(document.getElementById('graph-status').textContent,/Backend offline/);
});
test('relationship inspector leads with readable meaning rather than serialized type metadata',async()=>{
 const {g,document}=setup(async()=>result('relation'));
 await g.inspect('relation',{id:'relation',from:'a',to:'b',type:'uses',representation:'controlled_relation',definition:{id:'uses',definition:'Employs a resource.',subject_role:'actor',object_role:'resource'}});
 assert.ok(document.getElementById('graph-inspector').children.some(x=>x.textContent==='Employs a resource.'));
});
