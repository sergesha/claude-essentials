"""Native Utopia stages with exact statement wire references for page updates."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from .extract_codec import CODEC_VERSION, COLLECTIONS, prepare_update_request, expand_statement_references

ROOT = Path(__file__).resolve().parents[2]

def native(mode, payload):
    binary=os.environ.get('POC9_EXTRACT_BIN',str(ROOT/'extract/target/debug/poc9-extract'))
    run=subprocess.run([binary,mode],input=payload if mode=='parse' and isinstance(payload,str) else json.dumps(payload,ensure_ascii=False),text=True,capture_output=True)
    if run.returncode:
        raise RuntimeError('native extractor '+mode+' failed: '+run.stderr[-2000:])
    return json.loads(run.stdout)


def response_text(response):
    if isinstance(response,str): return response
    if isinstance(response,dict) and isinstance(response.get('raw_response'),str): return response['raw_response']
    return json.dumps(response,ensure_ascii=False)


def native_call(stage, payload, model, calls, *, previous_calls=None, model_profile=None):
    request={'operation':stage,'messages':native(stage+'_prepare',payload)}
    trace={'request':copy.deepcopy(request)};calls.append(trace)
    if stage=='phrase' and isinstance(model_profile,dict) and model_profile:
        binary=Path(os.environ.get('POC9_EXTRACT_BIN',str(ROOT/'extract/target/debug/poc9-extract')))
        contract={'model_profile':copy.deepcopy(model_profile),
                  'native_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
        basis={'request':request,'payload':payload,'contract':contract}
        key=hashlib.sha256(json.dumps(basis,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        trace.update(cache_contract=contract,cache_payload=copy.deepcopy(payload),cache_key=key)
        for old in previous_calls or []:
            if (old.get('error') or 'response' not in old or old.get('request')!=request
                    or old.get('cache_contract')!=contract or old.get('cache_payload')!=payload
                    or old.get('cache_key')!=key):continue
            try:
                parsed=native(stage+'_parse',{**payload,'response':response_text(old['response'])})
                if parsed.get('malformed')!=0:continue
            except (ValueError,RuntimeError,OSError):continue
            trace.update(response=copy.deepcopy(old['response']),parsed=copy.deepcopy(parsed),
                         reused=True,reused_from=key)
            return parsed
    try:
        response=model(request);trace['response']=copy.deepcopy(response)
        parsed=native(stage+'_parse',{**payload,'response':response_text(response)})
        trace['parsed']=copy.deepcopy(parsed)
        return parsed
    except (ValueError,RuntimeError,OSError) as exc:
        trace['error']=str(exc)
        return {'error':str(exc)}


def extract_wiki(wiki, model, *, previous=None, previous_wiki=None, model_profile=None, max_workers=1):
    """Extract a {relative page path: Markdown text} export into one retained bundle.

    Calls may overlap only across independent pages; output retains input order.
    Reuse is exact and opt-in via an explicit model profile, from the selected
    previous extraction only. Cached raw replies still pass the native parser.
    """
    if not isinstance(max_workers,int) or isinstance(max_workers,bool) or max_workers<1:
        raise ValueError('max_workers must be a positive integer')
    if model_profile is not None and (not isinstance(model_profile,dict) or not model_profile):
        raise ValueError('model_profile must be a nonempty object or None')
    binary=Path(os.environ.get('POC9_EXTRACT_BIN',str(ROOT/'extract/target/debug/poc9-extract')))
    contract={'model_profile':copy.deepcopy(model_profile),
              'native_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
              'codec_version':CODEC_VERSION}
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    retained={}
    if model_profile is not None:
        for trace in (previous or {}).get('model_calls',[]):
            if (trace.get('filename') and isinstance(trace.get('native_response'),str)
                    and trace.get('cache_contract')==contract and not trace.get('error')):
                retained[trace['filename']]=trace
    jobs=[]
    for filename,text in wiki.items():
        request={'operation':'open','messages':native('prepare',{'filename':filename,'text':text})}
        key=digest({'filename':filename,'request':request,**contract})
        jobs.append((filename,text,request,key))

    def process(job):
        filename,text,page_request,key=job;started=time.monotonic()
        trace={'page_request':copy.deepcopy(page_request),'filename':filename,'cache_key':key,
               'cache_contract':copy.deepcopy(contract),'reused':False,'mode':'full'}
        try:
            old=retained.get(filename);parsed=None;baseline=None
            if old:
                try:
                    candidate=native('parse',old['native_response'])
                    if candidate['skipped']==0 and not candidate['truncated']:
                        if old.get('cache_key')==key and old.get('page_request')==page_request:
                            parsed=candidate
                            for field in ('request','request_sha256','response','native_response'):
                                trace[field]=copy.deepcopy(old[field])
                            trace.update(reused=True,reused_from=key,mode='reused')
                        elif filename in (previous_wiki or {}) and previous_wiki[filename]!=text:
                            old_request={'operation':'open','messages':native('prepare',{
                                'filename':filename,'text':previous_wiki[filename]})}
                            if (old.get('page_request')==old_request and old.get('cache_key')==
                                    digest({'filename':filename,'request':old_request,**contract})):
                                raw=json.loads(old['native_response'])
                                if (isinstance(raw,dict) and set(raw)==set(COLLECTIONS)
                                        and all(isinstance(raw[k],list) and
                                                all(isinstance(r,list) for r in raw[k]) for k in COLLECTIONS)):
                                    baseline=raw
                except (ValueError,RuntimeError,OSError,KeyError):
                    parsed=None;baseline=None  # Unusable previous result is a miss.
            if parsed is None:
                request=page_request
                if baseline is not None:
                    request=prepare_update_request(page_request,previous_wiki[filename],text,baseline)
                    trace['mode']='statement_refs'
                trace['request']=copy.deepcopy(request);trace['request_sha256']=digest(request)
                reply=model(request);trace['response']=copy.deepcopy(reply)
                raw=response_text(reply)
                if baseline is not None:
                    raw=json.dumps(expand_statement_references(json.loads(raw),baseline),ensure_ascii=False)
                trace['native_response']=raw
                parsed=native('parse',raw)
                if baseline is not None and (parsed['skipped'] or parsed['truncated']):
                    raise ValueError('incomplete native extraction after statement-reference expansion')
            trace['parsed']=copy.deepcopy(parsed)
        except (ValueError,RuntimeError,OSError) as exc:
            trace['error']=str(exc)
            raise RuntimeError('extraction failed for '+filename+': '+str(exc)) from exc
        finally:
            trace['elapsed_seconds']=time.monotonic()-started
        return filename,parsed,trace

    result={'entities':[],'statements':[],'names':[],'time':[],'skipped':0,'truncated':False,'model_calls':[]}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        completed=list(pool.map(process,jobs))
    for filename,parsed,trace in completed:
        result['model_calls'].append(trace)
        result['statements'].extend({**row,'wiki_page':filename} for row in parsed['statements'])
        for key in ('entities','names','time'):result[key].extend(parsed.get(key,[]))
        result['skipped']+=parsed['skipped'];result['truncated']|=parsed['truncated']
    reused=sum(trace['reused'] for trace in result['model_calls'])
    result['cache']={'reused_pages':reused,'model_pages':len(jobs)-reused,'enabled':model_profile is not None}
    return result


def phrase_items(extraction, indexes=None):
    return [{'id':i,'phrase':s.get('phrase',''),'object_is_value':s.get('object') is None,
             'examples':[json.dumps(s,ensure_ascii=False)],'quotes':[s.get('quote','')]}
            for i,s in enumerate(extraction['statements']) if indexes is None or i in indexes]
