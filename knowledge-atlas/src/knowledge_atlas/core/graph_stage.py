"""Native TerminusDB projection of an exact Git candidate; no publication implied."""
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error


def key(kind,value):
    return kind+'-'+hashlib.sha256(value.encode()).hexdigest()[:24]


def _local_records(snapshot,commit):
    """Retain original payloads; links do not infer acceptance or valid time."""
    manifest=snapshot['manifest']
    # A newly created composition-only atlas has no local compiled module.
    if 'wiki' not in snapshot and 'knowledge' not in manifest.get('modules', {}):
        wiki={'sources':[], 'pages':[], 'references':[]}
        extract={'entities':[], 'statements':[]}
    else:
        wiki=snapshot['wiki'];extract=snapshot['extraction']
    records=[];native_ids={};entities={}
    bundle=snapshot.get('relations')
    if bundle:
        from .relations import validate_bundle
        validate_bundle(bundle, extract)
    relation_rows={r['index']:r for r in bundle['relations']} if bundle else {}
    statement_ids={}
    def record(k,kind,text,payload,links=()):
        doc={'@type':'Record','@id':'Record/'+k,'key':k,'kind':kind,'text':text,
             'payload':json.dumps(payload,ensure_ascii=False,sort_keys=True),
             'git_commit':commit,'recorded_at':manifest['recorded_at'],
             'status':manifest['status'],'links':list(links)}
        if manifest.get('accepted_at'):doc['accepted_at']=manifest['accepted_at']
        records.append(doc);return doc
    for source in wiki['sources']:
        k=key('source',source['filename']);native_ids[source['id']]='Record/'+k
        doc=record(k,'source',snapshot['sources'][source['filename']].decode('utf-8'),source)
        doc['recorded_at'] = source.get('recorded_at') or wiki.get('recorded_at') or manifest['recorded_at']
        for field in ('source_time','valid_from','valid_to'):
            if source.get(field):doc[field]=source[field]
    for page in wiki['pages']:
        k=key('wiki',page['path']+page['filename']);native_ids[page['id']]='Record/'+k
        record(k,'wiki',page['content'],page)
    wiki_pages={p['path']+p['filename']:native_ids[p['id']] for p in wiki['pages']}
    by_id={d['@id']:d for d in records}
    for edge in wiki['references']:
        # Upstream returns source_id/target_id for native reference edges.
        src=native_ids[edge['source_id']];target=native_ids[edge['target_id']]
        by_id[src]['links'].append(target)
    for entity in extract['entities']:
        k=key('entity',entity['name']);entities[entity['name']]='Record/'+k
        record(k,'entity',entity['name']+' ('+entity['kind']+')',entity)
    for index,statement in enumerate(extract['statements']):
        quoted=statement.get('quote') or ''
        origin=statement.get('wiki_page')
        if origin is not None and origin not in wiki_pages:raise ValueError('Unknown Wiki page origin: '+str(origin))
        links=[wiki_pages[origin]] if origin is not None else []
        k=key('statement',json.dumps(statement,sort_keys=True,ensure_ascii=False))
        qualifiers = statement.get('qualifiers') or []
        pairs = qualifiers.items() if isinstance(qualifiers, dict) else qualifiers
        moods = [pair[1] for pair in pairs if isinstance(pair, (tuple, list)) and len(pair) == 2 and pair[0] == 'mood']
        payload = {**statement, 'modality': moods} if moods and extract.get('passage_version', 0) >= 1 else statement
        doc=record(k,'statement',quoted,payload,links)
        statement_ids[index]=doc['@id']
        if bundle and relation_rows[index]['status']!='resolved':doc['status']='candidate'
        if statement['subject'] in entities:doc['subject']=entities[statement['subject']]
        if statement.get('object') in entities:doc['object']=entities[statement['object']]
        if statement.get('when'):doc['valid_time_text']=statement['when']
        # Preserve explicit relation roles, not just untyped adjacency.
        doc['links']=sorted(set(doc['links']+[doc[f] for f in ('subject','object') if f in doc]))
    if bundle:
        catalog={t['id']:t for t in bundle['catalog']}
        occurrences={}
        for row in bundle['relations']:
            if row['status']=='resolved':
                rid=statement_ids[row['index']]
                occurrences[rid]=occurrences.get(rid,0)+1
        for row in bundle['relations']:
            if row['status']!='resolved':continue
            endpoints=[]
            attribute=catalog[row['type_id']]['kind']=='attribute'
            for role in (('subject',) if attribute else ('subject','object')):
                name=row[role]
                if name not in entities:
                    term=record(key('relation-term',name),'entity',name,{'name':name,'kind':'relation_term'})
                    entities[name]=term['@id']
                endpoints.append(entities[name])
            payload={**row,'type_definition':catalog[row['type_id']],
                     'extraction_digest':bundle['extraction_digest']}
            text_fields={k:payload[k] for k in ('subject','type_id','object','polarity','details','qualifiers','when','value')}
            if 'ended' in payload:text_fields['ended']=payload['ended']
            if attribute:text_fields['phrase']=payload['phrase']
            text=json.dumps(text_fields,ensure_ascii=False,sort_keys=True)
            statement_id=statement_ids[row['index']]
            # Repeated identical statements share evidence identity, but their
            # native alignment rows have distinct indexes/bindings. Retain both
            # occurrences without selecting a semantic winner or colliding IDs.
            identity=statement_id
            if occurrences[statement_id]>1:identity+=':occurrence:'+str(row['index'])
            doc=record(key('relation',identity),'attribute' if attribute else 'relation',text,payload,
                       [statement_id,*endpoints])
            doc['subject']=endpoints[0]
            if not attribute:doc['object']=endpoints[1]
    from .relations import core_catalog
    defined_types={t['id']:t for t in (bundle['catalog'] if bundle else core_catalog())}
    semantic=snapshot.get('knowledge_records', [])
    identifiers={r['id']:'Record/'+key('knowledge',manifest['package_id']+':'+r['id']) for r in semantic}
    def semantic_id(local):
        return local if local.startswith('Record/') else 'Record/'+key('knowledge',manifest['package_id']+':'+local)
    for entry in semantic:
        payload={**entry, 'local_id':entry['id'], 'id':identifiers[entry['id']]}
        references=[child for group in entry.get('support_sets', []) for child in group]
        references+=entry.get('depends_on', [])+entry.get('conflicts_with', [])
        unresolved=sorted(set(references)-identifiers.keys())
        if unresolved:payload['unresolved_references']=unresolved
        payload['support_sets']=[[semantic_id(child) for child in group] for group in entry.get('support_sets', [])]
        for field in ('depends_on','conflicts_with'):
            payload[field]=[semantic_id(child) for child in entry.get(field, [])]
        payload['status']=('candidate' if manifest['status']!='accepted' else
                           {'active':'accepted','withdrawn':'withdrawn','needs_review':'needs_review'}[entry['state']])
        if all(isinstance(entry.get(f),str) for f in ('from','to','type')):
            definition=defined_types.get(entry['type'])
            if definition:payload['relation_type_definition']=definition
            else:payload['status']='candidate'
        links={citation['record_id'] for citation in entry['provenance']}
        # Missing references remain in the payload; native edges target only
        # existing records. No invented node can supply the absent evidence.
        links.update(identifiers[child] for child in references if child in identifiers)
        doc=record(identifiers[entry['id']].removeprefix('Record/'),entry['kind'],entry['text'],payload,sorted(links))
        doc['status']=payload['status'];doc['recorded_at']=entry['recorded_at']
    return records


def package_record_id(package_id,record_id):
    if record_id.startswith(('Record/knowledge-','Record/package-')):return record_id
    return 'Record/'+key('package',package_id+':'+record_id)


def records_for(snapshot,commit,*,namespace=None):
    """Compose pinned packages once, with package-qualified child identities."""
    nodes={};root_id=snapshot['manifest']['package_id']
    def collect(data,oid,mount,publication=None):
        identity=data['manifest']['package_id']
        if identity in nodes:
            if nodes[identity]['commit']!=oid:
                raise ValueError('conflicting pins for package identity: '+identity)
            nodes[identity]['mounts'].add(mount)
        else:nodes[identity]={'snapshot':data,'commit':oid,'mounts':{mount}}
        if publication is not None:
            nodes[identity].setdefault('receipts', {})[mount] = publication
        for inclusion in data.get('included_packages',[]):
            proofs = inclusion.get('inclusions', [])
            receipts = {item['mount']: item['publication'] for item in proofs}
            if (len(receipts) != len(proofs) or set(receipts) != set(inclusion['mounts'])
                    or any(not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid)
                           for oid in receipts.values())):
                raise ValueError('exact child publication required for every mount')
            for child_mount in inclusion['mounts']:
                collect(inclusion['snapshot'],inclusion['commit'],mount+'/'+child_mount if mount else child_mount, receipts[child_mount])
    collect(snapshot,commit,'')
    records=[];semantic={}
    for identity,node in nodes.items():
        data=node['snapshot'];qualified=identity!=root_id or namespace is not None
        def qualified_id(rid):return package_record_id(identity,rid) if qualified else rid
        for record in _local_records(data,node['commit']):
            payload=json.loads(record['payload'])
            original_id=record['@id']
            if qualified:
                record['@id']=qualified_id(original_id);record['key']=record['@id'].removeprefix('Record/')
                record['links']=[qualified_id(ref) for ref in record['links']]
                for field in ('subject','object'):
                    if field in record:record[field]=qualified_id(record[field])
                payload.update(package_id=identity,package_commit=node['commit'],local_record_id=original_id,
                    mounts=sorted(node['mounts']),package_accepted_at=data['manifest'].get('accepted_at'),
                    atlas_accepted_at=snapshot['manifest'].get('accepted_at'))
                if 'receipts' in node:
                    payload['inclusions'] = [{'mount': path, 'publication': receipt}
                                             for path, receipt in sorted(node['receipts'].items())]
                for citation in payload.get('provenance',[]):
                    citation['record_id']=qualified_id(citation['record_id'])
                    citation.setdefault('source_package_id',identity)
            record['git_commit']=commit
            record['payload']=json.dumps(payload,ensure_ascii=False,sort_keys=True)
            records.append(record)
        for entry in data.get('knowledge_records',[]):
            rid='Record/'+key('knowledge',identity+':'+entry['id'])
            semantic[rid]=(identity,entry)
    present={r['@id'] for r in records}
    for record in records:
        if record['@id'] not in semantic:continue
        identity,entry=semantic[record['@id']];payload=json.loads(record['payload'])
        refs=[child for group in entry.get('support_sets',[]) for child in group]
        refs+=entry.get('depends_on',[])+entry.get('conflicts_with',[])
        def ref_id(ref):return ref if ref.startswith('Record/') else 'Record/'+key('knowledge',identity+':'+ref)
        unresolved=sorted({ref for ref in refs if ref_id(ref) not in present})
        if unresolved:payload['unresolved_references']=unresolved
        else:payload.pop('unresolved_references',None)
        record['links']=sorted(set(record['links'])|{ref_id(ref) for ref in refs if ref_id(ref) in present})
        record['payload']=json.dumps(payload,ensure_ascii=False,sort_keys=True)
    # Native extraction is page-local; identical lexical keys can recur across
    # pages. One graph ID must be sent once, without declaring same_as or picking
    # one differing entity description as the canonical semantic interpretation.
    grouped={}
    for record in records:
        group=grouped.setdefault(record['@id'],[])
        if record not in group:group.append(record)
    unique=[]
    for group in grouped.values():
        first=group[0]
        if len(group)>1:
            if first['kind']!='entity' or any(
                    {k:v for k,v in row.items() if k not in ('text','payload')} !=
                    {k:v for k,v in first.items() if k not in ('text','payload')}
                    for row in group):
                raise ValueError('conflicting non-entity projection identity: '+first['@id'])
            first={**first,'text':'Extracted label observations:\n'+'\n'.join(row['text'] for row in group),
                   'payload':json.dumps({'projection_observations':[json.loads(row['payload']) for row in group],
                       'identity_basis':'Repeated projected lexical key; no semantic identity assertion'},
                       ensure_ascii=False,sort_keys=True)}
        unique.append(first)
    return unique


def record_schema(*, embed=True):
    schema={'@type':'Class','@id':'Record','@key':{'@type':'Lexical','@fields':['key']},
            **{f:'xsd:string' for f in ('key','kind','text','payload','git_commit','status')},
            'recorded_at':'xsd:dateTime','links':{'@type':'Set','@class':'Record'},
            **{f:{'@type':'Optional','@class':'Record'} for f in ('subject','object')},
            **{f:{'@type':'Optional','@class':'xsd:string'} for f in ('source_time','valid_from','valid_to','valid_time_text')},
            'accepted_at':{'@type':'Optional','@class':'xsd:dateTime'}}
    if embed:
        schema['@metadata']={'embedding':{'query':'query ($id: ID) { Record(id: $id) { text } }','template':'{{text}}'}}
    return schema


class GraphStage:
    def __init__(self,url,database,password=None):
        self.url=url.rstrip('/');self.database=database;self.password=password;self.trace=[]

    def call(self,method,path,body=None):
        password = self.password
        if password is None:
            endpoint = urllib.parse.urlsplit(self.url)
            if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or endpoint.username is not None:
                raise ValueError('the managed graph credential requires a local graph endpoint')
            key_file = os.environ.get('KNOWLEDGE_ATLAS_GRAPH_KEY_FILE')
            if not key_file:
                raise ValueError('configure KNOWLEDGE_ATLAS_GRAPH_KEY_FILE for this graph')
            password = Path(key_file).read_text().strip()
        req=urllib.request.Request(self.url+path,
            data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json'},method=method)
        req.add_unredirected_header('Authorization', 'Basic '+base64.b64encode(('admin:'+password).encode()).decode())
        try:
            with urllib.request.urlopen(req,timeout=60) as response:
                raw=response.read().decode();headers={k.lower():v for k,v in response.headers.items()}
                result={'status':response.status,'headers':headers,'body':json.loads(raw) if raw else None}
        except urllib.error.HTTPError as error:
            result={'status':error.code,'error':error.read().decode()}
            self.trace.append({'method':method,'path':path,**result})
            raise RuntimeError(str(result)) from error
        self.trace.append({'method':method,'path':path,**result})
        return result

    def project(self,store,commit):
        snapshot=store.read(commit);docs=records_for(snapshot,commit)
        self.call('POST','/api/db/'+self.database,{'label':'PoC9 unaccepted Git candidate','schema':True})
        schema=record_schema()
        self.call('POST','/api/document/'+self.database+'?graph_type=schema&author=poc9&message=schema',[schema])
        result=self.call('POST','/api/document/'+self.database+'?author=poc9&message=git-candidate',docs)
        graph_commit=result['headers']['terminusdb-data-version'].split(':')[-1]
        actual=self.documents(graph_commit)
        if {d['@id'].split('/data/')[-1] for d in actual}!={d['@id'] for d in docs}:
            raise ValueError('graph membership does not match Git candidate')
        return {'git_commit':commit,'graph_commit':graph_commit,'database':self.database,
                'count':len(docs),'status':snapshot['manifest']['status'],'published':False}

    def documents(self,commit):
        result=self.call('GET','/api/document/'+self.database+'/local/commit/'+commit+'?as_list=true')
        if result['headers'].get('terminusdb-data-version')!='commit:'+commit:
            raise ValueError('served graph commit mismatch')
        return result['body']
