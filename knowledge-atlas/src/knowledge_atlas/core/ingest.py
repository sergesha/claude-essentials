"""Connect native compilation/extraction to a candidate; publication stays separate."""
import copy
import json
import time
import threading
from pathlib import Path
from .wiki_stage import WikiStage
from .extract_stage import extract_wiki
from .relations import resolve_relations
from .git_store import git
from .publication import Publications, commit_files
from .feedback import compilation_impact

async def ingest(store, evidence, sources, *, recorded_at, model, agent=None,
                 previous_commit=None, resume_wiki=None, resume_extraction=None, resume_workspace=None,
                 extract_workers=2, model_profile=None, relation_model_profile=None, remove_sources=()):
    evidence=Path(evidence);evidence.mkdir(parents=True,exist_ok=False)
    execution={'status':'running','recorded_at':recorded_at,'previous_commit':previous_commit,
               'sources':copy.deepcopy(sources),'remove_sources':list(remove_sources),'resumed':{'wiki':resume_wiki is not None,'extraction':resume_extraction is not None,'workspace':str(resume_workspace) if resume_workspace else None},'stages':{}}
    started=time.monotonic();calls=[];call_lock=threading.Lock()
    def save(name,value):
        (evidence/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    def tracked(request):
        trace={'request':copy.deepcopy(request)};begin=time.monotonic()
        if hasattr(model,'prepare_request'):
            trace['effective_request']=model.prepare_request(request)
            trace['model_profile']=model.effective_profile(request['operation'])
        with call_lock:
            calls.append(trace);save('model-calls',calls)
        try:
            response=model(request)
            with call_lock:trace['response']=copy.deepcopy(response)
            return response
        except Exception as exc:
            with call_lock:trace['error']=str(exc)
            raise
        finally:
            with call_lock:
                trace['elapsed_seconds']=time.monotonic()-begin;save('model-calls',calls)
    save('model-calls',calls)
    try:
        previous=store.read(previous_commit) if previous_commit else None
        binding=None;metadata=None
        if previous:
            pubs=Publications(store);ready=pubs.ready();manifest=previous['manifest']
            origin=manifest.get('proposal_base_content',previous_commit)
            base_publication=manifest.get('proposal_base_publication')
            if ready:
                published=pubs.read(ready)['content_commit']
                if previous_commit==published:
                    origin=published;base_publication=ready
                elif (manifest['status']!='candidate' or base_publication!=ready or origin!=published):
                    raise ValueError('stale native update: select current published content or its proposal')
                git(store.root,'merge-base','--is-ancestor',origin,previous_commit)
            elif base_publication is not None:
                raise ValueError('stale native update: proposal publication is not current READY')
            binding={'proposal_base_content':origin,'proposal_base_publication':base_publication}
            files=git(store.root,'ls-tree','--name-only',previous_commit).splitlines()
            if manifest['status']=='candidate' and 'proposal.json' in files:
                metadata=json.loads(git(store.root,'show',previous_commit+':proposal.json'))
            else:
                metadata={'base_content':previous_commit,'base_publication':base_publication,
                          'method':'Native Wiki/Utopia update','recorded_at':recorded_at}
        begin=time.monotonic()
        if resume_wiki is not None:
            if remove_sources and (resume_wiki.get('removed_sources') != list(remove_sources)
                    or set(remove_sources) & {source['filename'] for source in resume_wiki.get('sources', [])}):
                raise ValueError('completed Wiki reuse is incompatible with explicit source removal')
            wiki=copy.deepcopy(resume_wiki)
        else:
            if agent is None:raise ValueError('native agent required unless explicitly resuming completed Wiki')
            if resume_workspace:
                retained=json.loads((Path(resume_workspace).parent/'execution.json').read_text())
                if retained['previous_commit']!=previous_commit:
                    raise ValueError('resume selected Git content differs from retained stage')
            wiki=await WikiStage(resume_workspace or evidence/'workspace').run_agent(sources,agent,recorded_at=recorded_at,previous=previous.get('wiki') if previous else None,resume=bool(resume_workspace),remove_sources=remove_sources)
        if not wiki.get('pages'):raise ValueError('completed Wiki must contain pages')
        save('wiki',wiki);execution['stages']['wiki_seconds']=time.monotonic()-begin
        begin=time.monotonic()
        def wiki_pages(bundle):
            return {str(Path(p['path'])/p['filename']) if p['path'].endswith('/') else p['path']:p['content']
                    for p in bundle['pages']}
        extraction=copy.deepcopy(resume_extraction) if resume_extraction is not None else extract_wiki(
            wiki_pages(wiki),tracked,
            previous=previous.get('extraction') if previous else None,
            previous_wiki=wiki_pages(previous['wiki']) if previous and 'wiki' in previous else None,
            model_profile=model_profile,max_workers=extract_workers)
        save('extraction',extraction);execution['stages']['extract_seconds']=time.monotonic()-begin
        begin=time.monotonic()
        relations=resolve_relations(extraction,tracked,previous=previous.get('relations') if previous else None,
                                    model_profile=relation_model_profile)
        save('relations',relations);execution['stages']['relations_seconds']=time.monotonic()-begin
        errors=[c['error'] for c in relations['model_calls'] if c.get('error')]
        if errors:raise RuntimeError('native relation transport failed: '+'; '.join(errors))
        begin=time.monotonic()
        if previous_commit:
            git(store.root,'checkout','--detach',previous_commit)
        candidate=store.snapshot(wiki,extraction,{'native_ingest':execution,'model_calls':calls},recorded_at=recorded_at,relation_bundle=relations)
        if previous:
            compiled=candidate['commit'];fresh=store.read(compiled)
            manifest={**fresh['manifest'],**binding}
            metadata['compiled_snapshot']=compiled
            metadata['wiki_impact']=compilation_impact(previous,fresh,previous_commit,compiled)
            result=commit_files(store.root,compiled,{'manifest.json':manifest,'proposal.json':metadata},
                                [compiled],'Retain native revision review context')
            git(store.root,'update-ref','refs/poc9/proposals/'+result,result)
            candidate={'commit':result,**manifest}
        execution['stages']['git_seconds']=time.monotonic()-begin
        execution.update(status='completed',candidate=candidate);save('candidate',candidate)
        return execution
    except Exception as exc:
        execution.update(status='failed',error=str(exc));raise
    finally:
        execution['elapsed_seconds']=time.monotonic()-started;save('execution',execution)
