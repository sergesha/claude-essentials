import cytoscape from './vendor/cytoscape.esm.min.mjs';
import {GraphModel,label,payload,edgeKey,evidenceSpan} from './graph-model.mjs';

/** Browser controller. All requests capture one publication and view generation. */
export class GraphExplorer{
  constructor({document,send,onOpenPage}){
    this.document=document;this.$=id=>document.getElementById(id);this.send=send;this.onOpenPage=onOpenPage;
    this.generation=0;this.selection=0;this.searchRequest=0;this.cy=null;this.model=new GraphModel(null);
    this.$('graph-search-form').onsubmit=e=>{e.preventDefault();return this.search(this.$('graph-query').value);};
    this.$('graph-native').onchange=()=>this.render();
    this.$('graph-fit').onclick=()=>this.cy?.fit(undefined,45);
    this.$('graph-clear').onclick=()=>this.reset(this.model.publication);
  }
  node(tag,text,className){const n=this.document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
  button(text,fn){const n=this.node('button',text,'secondary'),epoch=this.generation;n.onclick=async event=>{if(epoch!==this.generation)return;try{await fn();if(epoch===this.generation&&event?.detail===0&&n.closest?.('#graph-nodes, #graph-edges'))this.$('graph-inspector').focus();}catch(error){if(epoch===this.generation)this.message('Action unavailable: '+error.message);}};return n;}
  details(title,value){const n=this.node('details',undefined,'technical');n.append(this.node('summary',title),this.node('pre',JSON.stringify(value,null,2)));return n;}
  message(text){this.$('graph-status').textContent=text;}
  reset(publication){
    this.generation++;this.selection++;this.searchRequest++;this.model=new GraphModel(publication);this.seed=null;
    this.cy?.destroy();this.cy=null;
    for(const id of ['graph-results','graph-nodes','graph-edges','graph-inspector'])this.$(id).replaceChildren();
    this.$('graph-inspector').append(this.node('h3','Follow a connection'),this.node('p','Select an object or a relationship to inspect its evidence.','help-text'));
    this.$('graph-query').value='';this.$('graph-context').textContent=publication?'Publication '+publication:'No published version';
    this.$('graph-empty').hidden=false;this.$('graph-count').textContent='0 objects';
    this.message('Start from a wiki page or search for a concept. Only published records are shown.');
  }
  async request(action,payload,epoch=this.generation){
    const publication=this.model.publication;if(!publication)throw new Error('Choose a published version first.');
    const result=await this.send(action,{publication,payload});
    if(epoch!==this.generation)return null;
    if(result.publication!==publication)throw new Error('Response publication mismatch; no graph data was displayed.');
    return result.data;
  }
  async open(id){
    const publication=this.model.publication;this.reset(publication);this.seed=id;
    await this.expand(id,true);
  }
  async expand(id,initial=false){
    const epoch=this.generation;
    this.message('Loading the published neighborhood…');
    try{
      const data=await this.request('evidence.read',{record_id:id,expand:true,depth:1,limit:40},epoch);
      if(epoch!==this.generation)return;
      if(!data?.expansion){this.message('This record is unavailable in the selected publication.');return;}
      this.model.merge(data.expansion);this.render(id,initial);
      if(initial)await this.inspect(id);
    }catch(error){if(epoch===this.generation)this.message('Could not load graph: '+error.message);}
  }
  async search(query){
    const epoch=this.generation,request=++this.searchRequest;this.$('graph-results').replaceChildren();
    if(!query.trim())return;this.message('Searching this publication…');
    try{
      const data=await this.request('search',{query,mode:'fts',count:12},epoch);
      if(epoch!==this.generation||request!==this.searchRequest)return;
      for(const hit of data?.hits||[]){const r=hit.record;this.$('graph-results').append(this.button((r.kind||'record')+' · '+label(r),()=>this.open(r['@id'])));}
      this.message(data?.hits?.length?'Choose a result to start a new neighborhood. Up to 12 text matches.':'No text matches in this publication. Try another term; this does not prove absence.');
    }catch(error){if(epoch===this.generation&&request===this.searchRequest)this.message('Search unavailable: '+error.message);}
  }
  render(centerId,initial=false){
    const records=[...this.model.records.values()],edges=this.model.visibleEdges(this.$('graph-native').checked);
    this.$('graph-empty').hidden=records.length>0;
    if(records.length&&!this.cy){
      this.cy=cytoscape({container:this.$('graph-canvas'),elements:[],layout:{name:'preset'},minZoom:.15,maxZoom:3,wheelSensitivity:.25,
        style:[
          {selector:'node',style:{'label':'data(label)','font-size':11,'text-wrap':'ellipsis','text-max-width':135,'text-valign':'bottom','text-margin-y':9,'background-color':'#44836a','color':'#24332f','width':26,'height':26,'border-width':2,'border-color':'#fff'}},
          {selector:'node[kind="wiki"]',style:{'shape':'round-rectangle','background-color':'#5279a7','width':32,'height':28}},
          {selector:'node[kind="source"]',style:{'shape':'diamond','background-color':'#b68039','width':34,'height':34}},
          {selector:'node[kind="statement"],node[kind="relation"],node[kind="attribute"]',style:{'background-color':'#9584b1','shape':'hexagon'}},
          {selector:'edge',style:{'curve-style':'bezier','width':1.5,'line-color':'#6b9b85','target-arrow-color':'#6b9b85','target-arrow-shape':'triangle','label':'data(label)','font-size':9,'color':'#52665a','text-background-color':'#fafbf9','text-background-opacity':.9,'text-background-padding':3,'text-rotation':'autorotate'}},
          {selector:'edge[native]',style:{'line-style':'dashed','line-color':'#bbc5c3','target-arrow-color':'#bbc5c3','label':''}},
          {selector:'node:selected',style:{'border-width':4,'border-color':'#24332f'}},
          {selector:'edge:selected',style:{'line-color':'#24332f','target-arrow-color':'#24332f','width':4}},
        ]});
      this.cy.on('tap','node',event=>this.inspect(event.target.id()));
      this.cy.on('tap','edge',event=>{const edge=this.model.edges.get(event.target.id());if(edge)this.inspect(edge.id,edge);});
    }
    if(this.cy){
      const center=this.cy.getElementById(centerId||this.seed||'');const origin=center.length?center.position():{x:0,y:0};
      const added=records.filter(r=>!this.cy.getElementById(r['@id']).length);
      this.cy.batch(()=>{
        added.forEach((r,i)=>{const angle=i*2*Math.PI/Math.max(added.length,1),radius=initial?Math.max(180,added.length*11):190;
          const position=initial&&r['@id']===this.seed?{x:0,y:0}:{x:origin.x+Math.cos(angle)*radius,y:origin.y+Math.sin(angle)*radius};
          this.cy.add({group:'nodes',data:{id:r['@id'],label:label(r).slice(0,65),kind:r.kind},position});});
        this.cy.edges().remove();
        for(const e of edges)this.cy.add({group:'edges',data:{id:edgeKey(e),source:e.from,target:e.to,label:e.type,...(e.representation==='native_link'?{native:1}:{})}});
      });
      this.cy.resize();if(initial)this.cy.fit(undefined,50);
    }
    this.$('graph-count').textContent=`${records.length} objects · ${edges.length} connections`;
    this.message(`${this.model.truncated?'Partial neighborhood: a limit was reached. ':'Local neighborhoods only. '}Expand an object to explore further. Up to 40 records per request, 150 objects / 500 connections on screen. Missing links do not prove absence.`);
    this.$('graph-nodes').replaceChildren();this.$('graph-edges').replaceChildren();
    for(const r of records)this.$('graph-nodes').append(this.button((r.kind||'record')+' · '+label(r),()=>this.inspect(r['@id'])));
    for(const e of edges){const text=label(this.model.records.get(e.from))+' → '+(e.representation==='native_link'?'reference / navigation':e.type)+' → '+label(this.model.records.get(e.to));this.$('graph-edges').append(this.button(text,()=>this.inspect(e.id,e)));}
    if(!edges.length)this.$('graph-edges').append(this.node('p','No connections in this visible layer. Enable reference links or expand another object.','help-text'));
  }
  resize(){this.cy?.resize();}
  async inspect(id,edge=null,citation=null){
    const epoch=this.generation,selection=++this.selection,box=this.$('graph-inspector');
    box.replaceChildren(this.node('p','Loading evidence…','help-text'));
    try{
      const data=await this.request('evidence.read',{record_id:id},epoch);
      if(epoch!==this.generation||selection!==this.selection)return;
      const record=data?.record;
      if(!record){box.replaceChildren(this.node('p','Evidence unavailable in this publication.','help-text'));return;}
      const p=payload(record);box.replaceChildren();
      const native=edge?.representation==='native_link';
      box.append(this.node('span',edge?(native?'REFERENCE / NAVIGATION':'SEMANTIC RELATIONSHIP'):String(record.kind||'record').toUpperCase(),'eyebrow'));
      box.append(this.node('h3',edge?(native?'Reference link':edge.type):label(record)));
      if(edge)box.append(this.node('p',label(this.model.records.get(edge.from))+' → '+label(this.model.records.get(edge.to)),'graph-direction'));
      box.append(this.node('p','Record status: '+(record.status||'unknown')+' · Acceptance: '+(data.acceptance_kind||'unknown')+'. Publication is not a truth guarantee.','help-text'));
      if(native)box.append(this.node('p','This stored link provides navigation. It does not by itself assert support, cause or another semantic relationship.','help-text'));
      const meaning=edge||p;const definition=meaning.definition||p.type_definition||p.relation_type_definition;
      if(definition){box.append(this.node('h4','Relationship meaning'),this.node('p',typeof definition==='string'?definition:definition.definition,'graph-field'));
        if(definition.subject_role||definition.object_role)box.append(this.node('p',[definition.subject_role,definition.object_role].filter(Boolean).join(' → '),'help-text'));}
      const fields=['polarity','conditions','qualifiers','when','valid_time','ended','value','phrase'];
      for(const key of fields){const value=meaning[key]??p[key];if(value!==undefined&&value!==null&&value!==''){
        box.append(this.node('h4',key.replaceAll('_',' ')),this.node('div',typeof value==='string'?value:JSON.stringify(value,null,2),'graph-field'));
      }}
      const actions=this.node('div',undefined,'graph-actions');
      actions.append(this.button('Expand neighbors',()=>this.expand(id)),this.button('Start here',()=>this.open(id)));
      if(record.kind==='wiki')actions.append(this.button('Read wiki page ↗',()=>{if(epoch===this.generation)return this.onOpenPage(id);}));
      box.append(actions);
      const span=evidenceSpan(record,citation);
      if(citation)box.append(this.details('Original cited fragment & source pin',citation));
      if(span)box.append(this.node('p','The supplied character span and quote match this selected-publication record. The original source commit remains in the citation details.','help-text'));
      if(record.kind==='source'&&!span)box.append(this.node('p',citation?'Stored source document. The supplied fragment could not be verified in this version; showing the whole document.':'Stored source document. No paragraph locator was supplied for this selection; the complete text is shown below.','help-text'));
      const content=this.node('div',undefined,'graph-record');let mark;
      if(span){mark=this.node('mark',span.quote);content.append(this.node('span',span.before),mark,this.node('span',span.after));}
      else content.textContent=record.text||'No text supplied.';
      box.append(this.node('h4',edge?'Underlying record':'Content'),content);
      if(mark)content.scrollTop=Math.max(0,mark.offsetTop-content.offsetTop-30);
      for(const cited of p.provenance||[]){
        if(cited.record_id&&typeof cited.quote==='string'){
          box.append(this.node('h4','Cited fragment'),this.node('blockquote',cited.quote,'graph-field'),this.button('Inspect cited fragment in this version',()=>this.inspect(cited.record_id,null,cited)));
        }
      }
      const references=[...new Set([...(record.links||[]),...(p.provenance||[]).map(x=>x.record_id)].filter(x=>typeof x==='string'))];
      if(references.length){box.append(this.node('h4','Follow recorded references'));
        const list=this.node('div',undefined,'graph-reference-list');
        for(const ref of references)list.append(this.button(label(this.model.records.get(ref))==='Unknown record'?ref:label(this.model.records.get(ref)),()=>this.inspect(ref)));
        box.append(list,this.node('p','References use exact record identities in this publication. Original provenance, including pinned source commits and any locators, is retained below.','help-text'));
      }
      box.append(this.details('Exact record & provenance',record),this.details('Publication & graph',{publication:this.model.publication,graph_commit:this.model.graphCommit,...(edge?{relationship:edge}:{})}));
      this.cy?.elements().unselect();const item=this.cy?.getElementById(edge?edgeKey(edge):id);item?.select();
    }catch(error){if(epoch===this.generation&&selection===this.selection)box.replaceChildren(this.node('p','Evidence unavailable: '+error.message,'help-text'));}
  }
}
