#!/usr/bin/env python3
"""Codex native Wiki MCP agent: isolated config, retained JSONL, no retries."""
import argparse
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--effort', default='high', choices=['low', 'medium', 'high', 'xhigh'])
    args = parser.parse_args()
    request = json.load(sys.stdin)
    workspace = Path(request['workspace']).resolve()
    artifacts = workspace/'.llmwiki'/'poc9'
    artifacts.mkdir(parents=True, exist_ok=True)
    schema = {'type':'object', 'properties':{'status':{'type':'string', 'enum':['completed','blocked']},
        'summary':{'type':'string'}}, 'required':['status','summary'], 'additionalProperties':False}
    schema_path = artifacts/'codex-output-schema.json'
    schema_path.write_text(json.dumps(schema, indent=2))
    tools = ['guide', 'search', 'read', 'create', 'edit', 'append', 'lint']
    with tempfile.TemporaryDirectory(prefix='poc9-wiki-agent-') as temp:
        command = [os.environ.get('KNOWLEDGE_ATLAS_CODEX', 'codex'), '--no-daemon', '--ask-for-approval', 'never', 'exec',
            '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
            '--sandbox', 'read-only']
        for feature in ['shell_tool', 'code_mode', 'code_mode_only',
                        'apps', 'plugins', 'multi_agent', 'skill_search']:
            command += ['--disable', feature]
        command += ['--enable', 'skip_host_skill_discovery', '--enable', 'code_mode_host']
        config = {'project_doc_max_bytes':0, 'suppress_unstable_features_warning':True,
                  'web_search':'disabled', 'model_reasoning_effort':args.effort,
                  'model_reasoning_summary':'none',
                  'mcp_servers.wiki.command':request['mcp']['command'],
                  'mcp_servers.wiki.args':request['mcp']['args'],
                  'mcp_servers.wiki.enabled_tools':tools,
                  'mcp_servers.wiki.required':True}
        # These local Wiki operations are explicitly authorized for this run.
        # `approve` resolves their MCP permission prompts without widening the
        # global noninteractive approval policy or any unrelated server.
        for tool in tools:
            config['mcp_servers.wiki.tools.'+tool+'.approval_mode'] = 'approve'
        for key, value in request['mcp'].get('env', {}).items():
            config['mcp_servers.wiki.env.'+key] = value
        for key, value in config.items():
            command += ['-c', key+'='+json.dumps(value, ensure_ascii=False)]
        command += ['--model', args.model, '--json', '--output-schema', str(schema_path), '--cd', temp, '-']
        prompt = (Path(__file__).with_name('wiki-instructions.txt').read_text()
                  + json.dumps({k:v for k,v in request.items() if k not in {'mcp','workspace'}}, ensure_ascii=False))
        (artifacts/'codex-prompt.txt').write_text(prompt)
        (artifacts/'codex-command.json').write_text(json.dumps(command, indent=2))
        # Write the trace directly, retaining partial output on interruption/failure.
        with (artifacts/'codex-events.jsonl').open('w') as stdout, (artifacts/'codex-stderr.txt').open('w') as stderr:
            run = subprocess.run(command, input=prompt, text=True, stdout=stdout, stderr=stderr)
        answers = []
        for line in (artifacts/'codex-events.jsonl').read_text().splitlines():
            event = json.loads(line)
            item = event.get('item', {})
            if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
                answers.append(item['text'])
        try:
            completion = json.loads(answers[-1]) if answers else {}
        except (json.JSONDecodeError, TypeError):
            completion = {}
        status = completion.get('status', 'blocked')
        if status not in {'completed','blocked'}:
            status = 'blocked'
        summary = completion.get('summary', 'Missing valid completion metadata')
        evidence = {'returncode':run.returncode, 'final_message':answers[-1] if answers else None,
                    'trace':str(artifacts/'codex-events.jsonl'),
                    'stderr':str(artifacts/'codex-stderr.txt'), 'model':args.model,
                    'status':status, 'summary':summary}
        (artifacts/'codex-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        if run.returncode or status != 'completed':
            print(json.dumps(evidence), file=sys.stderr)
            return run.returncode or 1
        print(json.dumps(evidence, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
