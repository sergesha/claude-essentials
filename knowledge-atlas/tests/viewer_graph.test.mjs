import {test} from 'node:test';
import assert from 'node:assert/strict';
const moduleURL=new URL('../src/knowledge_atlas/viewer/static/graph-model.mjs',import.meta.url);
let api={};try{api=await import(moduleURL);}catch(e){if(e.code!=='ERR_MODULE_NOT_FOUND')throw e;}
const record=id=>({'@id':id,kind:'entity',text:id,payload:'{}'});
const expansion=(records,edges=[],publication='p')=>({publication_commit:publication,graph_commit:'g',records:records.map(record),edges,truncated:false});
test('parallel claims and multiple native links retain separate evidence identities',()=>{
 assert.ok(api.GraphModel,'graph model must exist');const g=new api.GraphModel('p');
 const edges=[{id:'r1',from:'a',to:'b',type:'uses',representation:'controlled_relation'},{id:'r2',from:'a',to:'b',type:'uses',representation:'controlled_relation'},{id:'a',from:'a',to:'b',type:'links',representation:'native_link'},{id:'a',from:'a',to:'c',type:'links',representation:'native_link'}];
 g.merge(expansion(['a','b','c'],edges));g.merge(expansion(['a','b'],edges.slice(0,2)));
 assert.equal(g.records.size,3);assert.equal(g.edges.size,4);assert.deepEqual([...g.edges.values()].map(e=>e.id),['r1','r2','a','a']);
 assert.equal(g.visibleEdges(false).length,2);assert.equal(g.visibleEdges(true).length,4);
});
test('publication mismatch does not contaminate graph',()=>{
 assert.ok(api.GraphModel);const g=new api.GraphModel('p');g.merge(expansion(['a']));
 assert.throws(()=>g.merge(expansion(['b'],[],'other')),/publication/i);assert.deepEqual([...g.records.keys()],['a']);
});
test('bounded accumulation drops dangling edges and never claims complete atlas',()=>{
 assert.ok(api.GraphModel);const g=new api.GraphModel('p',{nodeLimit:2,edgeLimit:1});
 g.merge(expansion(['a','b','c'],[{id:'r1',from:'a',to:'b'},{id:'r2',from:'a',to:'b'},{id:'r3',from:'a',to:'c'}]));
 assert.equal(g.records.size,2);assert.equal(g.edges.size,1);assert.equal(g.truncated,true);
 g.merge(expansion(['a']));assert.equal(g.truncated,true);
});
test('labels use actual payload and keep hostile text inert',()=>{
 assert.ok(api.label);assert.equal(api.label({...record('a'),payload:'{"title":"<script>bad</script>"}'}),'<script>bad</script>');
 assert.equal(api.label({...record('b'),payload:'bad json'}),'b');
});
test('graph labels explain relation records and repeated lexical observations',()=>{
 assert.equal(api.label({...record('r'),kind:'relation',payload:JSON.stringify({subject:'ReAct',type_id:'uses',object:'reasoning'})}),'ReAct → uses → reasoning');
 assert.equal(api.label({...record('e'),payload:JSON.stringify({projection_observations:[{name:'ReAct'},{name:'ReAct'}]})}),'ReAct · 2 observations');
});
test('only exact supplied Unicode codepoint spans become source highlights',()=>{
 assert.ok(api.evidenceSpan);const r={...record('s'),text:'A 🧠 quoted text'};
 assert.deepEqual(api.evidenceSpan(r,{record_id:'s',start:4,end:10,quote:'quoted'}),{before:'A 🧠 ',quote:'quoted',after:' text'});
 assert.equal(api.evidenceSpan(r,{record_id:'s',start:4,end:10,quote:'wrong'}),null);
 assert.equal(api.evidenceSpan(r,{record_id:'other',start:4,end:10,quote:'quoted'}),null);
});
