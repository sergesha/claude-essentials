/** A bounded view of one published graph, never a replacement graph store. */
export function payload(record){
  try{return typeof record?.payload==='string'?JSON.parse(record.payload):(record?.payload||{});}catch{return {};}
}
export function label(record){
  const p=payload(record);
  if(p.projection_observations?.length){
    const names=[...new Set(p.projection_observations.map(o=>o.name).filter(Boolean))];
    if(names.length)return names.join(' / ').slice(0,90)+' · '+p.projection_observations.length+' observations';
  }
  if(p.subject&&p.type_id)return [p.subject,p.type_id,p.object||p.phrase||p.value].filter(Boolean).join(' → ').slice(0,120);
  return String(p.title||p.name||p.filename||record?.text||record?.['@id']||'Unknown record').replace(/\s+/g,' ').slice(0,120);
}
export function edgeKey(edge){return JSON.stringify([edge.representation,edge.id,edge.from,edge.to,edge.type]);}
export class GraphModel{
  constructor(publication,{nodeLimit=150,edgeLimit=500}={}){
    this.publication=publication;this.nodeLimit=nodeLimit;this.edgeLimit=edgeLimit;
    this.records=new Map();this.edges=new Map();this.truncated=false;this.graphCommit=null;
  }
  merge(expansion){
    if(expansion.publication_commit!==this.publication)throw new Error('Graph publication mismatch');
    if(this.graphCommit&&this.graphCommit!==expansion.graph_commit)throw new Error('Graph commit mismatch');
    this.graphCommit=expansion.graph_commit;
    this.truncated ||= Boolean(expansion.truncated);
    for(const record of expansion.records||[]){
      const id=record['@id'];
      if(!this.records.has(id)&&this.records.size>=this.nodeLimit){this.truncated=true;continue;}
      this.records.set(id,record);
    }
    for(const edge of expansion.edges||[]){
      if(!this.records.has(edge.from)||!this.records.has(edge.to))continue;
      const key=edgeKey(edge);
      if(!this.edges.has(key)&&this.edges.size>=this.edgeLimit){this.truncated=true;continue;}
      this.edges.set(key,edge);
    }
  }
  visibleEdges(includeNative){return [...this.edges.values()].filter(e=>includeNative||e.representation!=='native_link');}
}

/** Python provenance offsets count Unicode codepoints, not UTF-16 units. */
export function evidenceSpan(record,citation){
  if(!citation||citation.record_id!==record['@id']||!Number.isInteger(citation.start)||!Number.isInteger(citation.end))return null;
  const chars=[...(record.text||'')],{start,end,quote}=citation;
  if(start<0||end<=start||end>chars.length||chars.slice(start,end).join('')!==quote)return null;
  return {before:chars.slice(0,start).join(''),quote,after:chars.slice(end).join('')};
}
