"""PoC9 boundary to unmodified LLM Wiki; compilation is performed by its LLM client.

This stage imports raw bytes and runs native Wiki write tools. It does not
replace semantic compilation with a template or claim proposals are accepted.
The native SQLite workspace is disposable staging, not the atlas canon.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import sys

def _native_tools():
    """Load the explicitly configured native runtime only when work starts."""
    import os
    location = os.environ.get('KNOWLEDGE_ATLAS_WIKI_ROOT')
    if not location:
        raise RuntimeError('Native Wiki runtime is not configured: KNOWLEDGE_ATLAS_WIKI_ROOT')
    vendor = Path(location).expanduser().resolve()
    if not all((vendor / part).is_dir() for part in ('mcp', 'shared')):
        raise RuntimeError('Configured native Wiki runtime lacks mcp/shared directories')
    for part in ('mcp', 'shared'):
        value = str(vendor / part)
        if value not in sys.path:
            sys.path.insert(0, value)
    from tools.write import WriteHandler
    from tools.references import update_references, _WIKI_LINK_RE, _parse_wiki_links
    from vaultfs.sqlite import SqliteVaultFS
    return vendor, WriteHandler, update_references, _WIKI_LINK_RE, _parse_wiki_links, SqliteVaultFS


class WikiStage:
    def __init__(self, workspace):
        self.workspace = Path(workspace)

    async def run_agent(self, sources, agent, *, recorded_at, previous=None, resume=False, remove_sources=()):
        """Compile raw sources using an async external native-MCP agent.

        ``agent(request)`` must finish all MCP work before returning JSON evidence.
        ``previous`` is the selected historical bundle, never the latest workspace.
        Native IDs are disposable; exported IDs of restored documents stay stable.
        Failed workspaces and agent evidence are retained for diagnosis.
        """
        import json
        import os
        import uuid
        import shutil
        VENDOR, WriteHandler, update_references, _WIKI_LINK_RE, _parse_wiki_links, SqliteVaultFS = _native_tools()
        if datetime.fromisoformat(recorded_at.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('recorded_at requires a timezone')
        previous = previous or {}
        records = {s['filename']: dict(s) for s in previous.get('sources', [])}
        for record in records.values():
            record.setdefault('recorded_at', previous.get('recorded_at', recorded_at))
        removed_names = list(remove_sources)
        if len(set(removed_names)) != len(removed_names) or any(name not in records for name in removed_names):
            raise ValueError('explicit removal must name distinct existing sources')
        for name in removed_names:
            records.pop(name)
        inputs = [(s, Path(s['path']), Path(s['path']).read_bytes()) for s in sources]
        names = [p.name for _, p, _ in inputs]
        if len(set(names)) != len(names):
            raise ValueError('duplicate source filenames require distinct source names')
        if set(names) & set(removed_names):
            raise ValueError('source cannot be updated and removed together')
        changed_names = []
        for item, path, raw in inputs:
            old = records.get(path.name, {})
            temporal = {k:item[k] if k in item else old.get(k)
                        for k in ('source_time', 'valid_from', 'valid_to')}
            text = raw.decode('utf-8')
            changed = (not old or old['content'].encode('utf-8') != raw
                       or any(old.get(k) != v for k,v in temporal.items()))
            if changed:
                changed_names.append(path.name)
            records[path.name] = {'filename':path.name, 'content':text,
                'recorded_at':recorded_at if changed else old['recorded_at'],
                'sha256':hashlib.sha256(raw).hexdigest(), **temporal,
                **{key:item.get(key, old.get(key)) for key in ('source_id','name') if key in item or key in old},
                **({'id':records[path.name]['id']} if path.name in records else {})}
        # Validate restored source integrity before creating a workspace.
        for name, record in records.items():
            if Path(name).name != name or name in {'.', '..'}:
                raise ValueError('source filenames must be plain names')
            raw = record['content'].encode('utf-8')
            if hashlib.sha256(raw).hexdigest() != record['sha256']:
                raise ValueError('previous source hash mismatch: ' + name)
        for page in previous.get('pages', []):
            if not page['path'].startswith('/wiki/') or '..' in Path(page['path']).parts:
                raise ValueError('previous pages must stay under /wiki/')
        workspace = self.workspace.resolve()
        artifacts = workspace/'.llmwiki'/'poc9'
        expected_sources=[{'filename':n, **{k:r.get(k) for k in ('sha256','source_time','valid_from','valid_to')}} for n,r in records.items()]
        if resume:
            original=json.loads((artifacts/'agent-request.json').read_text())
            if (original['sources']!=expected_sources or original['changed_sources']!=changed_names
                    or original.get('removed_sources',[])!=removed_names
                    or original['previous']!=bool(previous) or original['recorded_at']!=recorded_at):
                raise ValueError('resume input binding differs from retained native stage')
            archive=artifacts/('attempt-'+str(1+len(list(artifacts.glob('attempt-*')))))
            archive.mkdir()
            for path in artifacts.iterdir():
                if path.is_file():shutil.copy2(path,archive/path.name)
        else:
            self.workspace.mkdir(parents=True, exist_ok=False)
        user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'local'))
        native = SqliteVaultFS(user_id)
        stable_ids = {}
        try:
            await SqliteVaultFS.init(str(workspace))
            kb_id = await native.ensure_workspace(workspace.name)
            for name, record in records.items():
                raw = record['content'].encode('utf-8')
                metadata = {k:record.get(k) for k in ('sha256','source_time','valid_from','valid_to')}
                if resume:
                    doc=await native.get_document(kb_id,name,'/')
                    if not doc or doc['content']!=record['content'] or (workspace/name).read_bytes()!=raw:
                        raise ValueError('resumed raw source differs: '+name)
                else:
                    (workspace/name).write_bytes(raw)
                    doc = await native.create_document(kb_id, name, name, '/',
                        Path(name).suffix.lstrip('.'), record['content'], ['source'], metadata=metadata)
                stable_ids[doc['id']] = record.get('id', doc['id'])
                record['id'] = stable_ids[doc['id']]
            for page in previous.get('pages', []):
                if resume:
                    doc=await native.get_document(kb_id,page['filename'],page['path'])
                    if not doc:raise ValueError('resumed selected Wiki page missing')
                else:
                    doc = await native.create_document(kb_id, page['filename'], page['title'],
                        page['path'], page['file_type'], page['content'], page.get('tags', []),
                        date=page.get('date'), metadata=page.get('metadata'))
                stable_ids[doc['id']] = page['id']
            docs = await native.list_documents_with_content(kb_id, path_glob='/wiki/*')
            for doc in docs:
                await update_references(native, kb_id, str(doc['id']), doc['content'], doc['path'])
        finally:
            await SqliteVaultFS.close()
        request = {'workspace':str(workspace), 'knowledge_base':workspace.name,
            'recorded_at':recorded_at, 'previous':bool(previous),
            'sources':[{'filename':n, **{k:r.get(k) for k in ('sha256','source_time','valid_from','valid_to')}} for n,r in records.items()],
            'changed_sources':changed_names, 'resume':resume,
            'removed_sources':removed_names,
            **({'source_removal_instruction': 'The listed removed sources are no longer current. Use native Wiki tools to reconcile the retained pages, summaries and frontmatter against the remaining sources, removing assertions and citations supported only by removed sources. Preserve independent support and uncertainty. Prior source/page versions remain in Git history; do not present removed contributions as current knowledge.'} if removed_names else {}),
            'mcp':{'command':sys.executable,
                   'args':[str(Path(__file__).resolve().parents[1] / 'wiki_mcp.py'), '--workspace', str(workspace)],
                   'env':{'PYTHONPATH':os.pathsep.join([str(VENDOR/'mcp'), str(VENDOR/'shared')]),
                          'LLMWIKI_USER_ID':user_id}}}
        artifacts = workspace/'.llmwiki'/'poc9'
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts/'agent-request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2))
        evidence = await agent(request)
        (artifacts/'agent-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        if not isinstance(evidence, dict) or evidence.get('status') != 'completed':
            failure = {'status':'failed', 'reason':'agent did not complete native Wiki workflow',
                       'agent':evidence}
            (artifacts/'stage-failure.json').write_text(json.dumps(failure, ensure_ascii=False, indent=2))
            raise RuntimeError(failure['reason'])
        try:
            await SqliteVaultFS.init(str(workspace))
            docs = await native.list_documents_with_content(kb_id, path_glob='/wiki/*')
            if not docs:
                failure = {'status':'failed', 'reason':'agent produced no Wiki pages'}
                (artifacts/'stage-failure.json').write_text(json.dumps(failure, indent=2))
                raise ValueError(failure['reason'])
            for doc in docs:
                await update_references(native, kb_id, str(doc['id']), doc['content'], doc['path'])
                doc['id'] = stable_ids.get(doc['id'], doc['id'])
            refs = await native.get_reference_edges(kb_id)
            for ref in refs:
                for key in ('source_id','target_id'):
                    if key in ref:
                        ref[key] = stable_ids.get(ref[key], ref[key])
            # The agent must not change the source layer, even if it ignores guide.
            for name, record in records.items():
                doc = await native.get_document(kb_id, name, '/')
                if not doc or doc['content'] != record['content'] or (workspace/name).read_bytes() != record['content'].encode('utf-8'):
                    raise ValueError('agent changed raw source: ' + name)
            return {'stage':'wiki', 'status':'candidate', 'recorded_at':recorded_at,
                'accepted_at':None, 'sources':list(records.values()), 'pages':docs,
                'references':refs, 'native_responses':evidence, 'compiler':'native-mcp-agent',
                **({'removed_sources':removed_names} if removed_names else {}),
                'wiki_revision':'aac3e6493306b7fff3b09cb181ca93918e93d0e6'}
        finally:
            await SqliteVaultFS.close()

    async def run(self, sources, pages, *, recorded_at):
        """Historical ready-page import compatibility; use run_agent for ingest.

        Unknown source/valid time remains null. Recorded time never supplies
        missing valid time or acceptance time. A failed workspace is retained.
        """
        VENDOR, WriteHandler, update_references, _WIKI_LINK_RE, _parse_wiki_links, SqliteVaultFS = _native_tools()
        if datetime.fromisoformat(recorded_at.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('recorded_at requires a timezone')
        inputs = [(item, Path(item['path']), Path(item['path']).read_bytes()) for item in sources]
        names = [path.name for _, path, _ in inputs]
        if len(set(names)) != len(names):
            raise ValueError('duplicate source filenames require distinct source names')
        texts = [raw.decode('utf-8') for _, _, raw in inputs]
        for page in pages:
            parts = Path(page['path']).parts
            if not page['path'].startswith('/wiki/') or '..' in parts:
                raise ValueError('compiled pages must stay under /wiki/')
        self.workspace.mkdir(parents=True, exist_ok=False)
        native = SqliteVaultFS('poc9-local')
        initialized = False
        try:
            await SqliteVaultFS.init(str(self.workspace))
            initialized = True
            kb_id = await native.ensure_workspace(self.workspace.name)
            kb = await native.get_workspace()
            writer = WriteHandler(native, kb)
            source_records = []
            for (item, path, raw), content in zip(inputs, texts):
                (self.workspace / path.name).write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                metadata = {'sha256':digest, 'source_time':item.get('source_time'),
                            'valid_from':item.get('valid_from'), 'valid_to':item.get('valid_to')}
                doc = await native.create_document(kb_id, path.name, path.name, '/',
                    path.suffix.lstrip('.'), content, ['source'], metadata=metadata)
                source_records.append({'id':doc['id'], 'filename':path.name, 'content':content, 'recorded_at':recorded_at,
                                       **metadata})
            responses = []
            # Native create derives filenames from titles, not the requested path.
            # Resolve model-declared links using that same pinned native behavior.
            aliases = {}
            for page in pages:
                if page['path'].endswith('.md'):
                    actual = writer._to_dir_path(page['path']) + writer._title_to_filename(page['title'])[0]
                    requested = page['path'].removeprefix('/wiki/').lower()
                    if requested in aliases and aliases[requested] != actual:
                        raise ValueError('ambiguous requested Wiki page path')
                    aliases[requested] = actual
            for page in pages:
                relative_dir = writer._to_dir_path(page['path']).removeprefix('/wiki/')
                def resolve_link(match):
                    targets = _parse_wiki_links(match.group(0), relative_dir)
                    actual = aliases.get(targets[0].lower()) if targets else None
                    if actual is None:
                        return match.group(0)
                    start = match.start(1) - match.start()
                    end = match.end(1) - match.start()
                    return match.group(0)[:start] + actual + match.group(0)[end:]
                content = _WIKI_LINK_RE.sub(resolve_link, page['content'])
                response = await writer.create(page['path'], page['title'], content,
                    page.get('tags', []), recorded_at[:10], False)
                responses.append(response)
                if response.startswith('Error:'):
                    raise ValueError(response)
            docs = await native.list_documents_with_content(kb_id, path_glob='/wiki/*')
            # Resolve links whose targets were created later in this compilation.
            for doc in docs:
                await update_references(native, kb_id, str(doc['id']), doc['content'], doc['path'])
            return {'stage':'wiki', 'status':'candidate', 'recorded_at':recorded_at,
                    'accepted_at':None, 'sources':source_records, 'pages':docs,
                    'references':await native.get_reference_edges(kb_id),
                    'native_responses':responses,
                    'wiki_revision':'aac3e6493306b7fff3b09cb181ca93918e93d0e6'}
        finally:
            if initialized:
                await SqliteVaultFS.close()
