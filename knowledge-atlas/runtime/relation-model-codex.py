#!/usr/bin/env python3
"""Optional Codex CLI JSON adapter. No tools, no retries, isolated working directory."""
import argparse
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--model',default='gpt-6.1-sol')
p.add_argument('--effort',default='high',choices=['low','medium','high','xhigh'])
a=p.parse_args()
request=json.load(sys.stdin)
schema={'type':'object','properties':{'response':{'type':'string'}},'required':['response'],'additionalProperties':False}
with tempfile.TemporaryDirectory(prefix='poc9-relations-model-') as temp:
    root=Path(temp);(root/'schema.json').write_text(json.dumps(schema))
    args=[os.environ.get('KNOWLEDGE_ATLAS_CODEX', 'codex'),'--no-daemon','--ask-for-approval','never','exec','--ignore-user-config','--ephemeral',
          '--skip-git-repo-check','--sandbox','read-only','--disable','shell_tool','--disable','code_mode_host',
          '--disable','code_mode','--disable','code_mode_only','--disable','apps','--disable','plugins',
          '--disable','multi_agent','--disable','skill_search','--enable','skip_host_skill_discovery',
          '-c','project_doc_max_bytes=0','-c','suppress_unstable_features_warning=true',
          '-c','web_search="disabled"','-c','model_reasoning_effort="'+a.effort+'"',
          '-c','model_reasoning_summary="none"','--model',a.model,'--json',
          '--output-schema',str(root/'schema.json'),'--cd',str(root),'-']
    native_messages=request.get('messages')
    if native_messages is not None:
        if (not isinstance(native_messages,list) or len(native_messages)!=2 or
            [m.get('role') for m in native_messages]!=['system','user'] or
            not all(isinstance(m.get('content'),str) for m in native_messages)):
            raise ValueError('Codex native mode requires one upstream system then one user message')
        # CLI has no arbitrary chat-role input. The documented instruction file
        # replaces model instructions with exact upstream system content.
        schema_at=args.index('--output-schema')
        del args[schema_at:schema_at+2]
        instructions=root/'upstream-system.txt'
        instructions.write_text(native_messages[0]['content'])
        args[-1:-1]=['-c','model_instructions_file='+json.dumps(str(instructions))]
        prompt=native_messages[1]['content']
    else:
        prompt='Execute the supplied relation-processing request. Return the requested JSON serialized in response. No tools.\n'+json.dumps(request,ensure_ascii=False)
    run=subprocess.run(args,input=prompt,text=True,capture_output=True)
    if run.returncode:
        failures=[]
        for line in run.stdout.splitlines():
            try:event=json.loads(line)
            except json.JSONDecodeError:continue
            if not isinstance(event,dict):continue
            if event.get('type')=='error' and isinstance(event.get('message'),str):
                failures.append(event['message'])
            error=event.get('error')
            if isinstance(error,dict) and isinstance(error.get('message'),str):
                failures.append(error['message'])
        detail='\n'.join([run.stderr.strip(),*failures]).strip()
        print((detail or 'Codex command exited with status '+str(run.returncode))[-2000:],file=sys.stderr)
        sys.exit(run.returncode)
    answers=[]
    for line in run.stdout.splitlines():
        event=json.loads(line);item=event.get('item',{})
        if event.get('type')=='item.completed' and item.get('type')=='agent_message':answers.append(item['text'])
    if not answers:raise RuntimeError('no final model response')
    parsed={'raw_response':answers[-1]} if native_messages is not None else json.loads(json.loads(answers[-1])['response'])
    print(json.dumps(parsed,ensure_ascii=False))
