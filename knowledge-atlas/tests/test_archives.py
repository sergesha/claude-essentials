"""Portable canonical decisions; controlled vectors are not native qualification."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import uuid
import zipfile
import pytest
from acceptance_fixture import review_acceptance
from knowledge_atlas.core.archive import export_atlas, restore_atlas
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.embedding_provider import EmbeddingProvider, canonical
from knowledge_atlas.actions.decisions import decide_candidate, revise_candidate, decision_inventory
from test_packages import snapshot
from test_decisions import context_for


@pytest.fixture
def portable(tmp_path,monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.runtime import _assets
    assets=tmp_path/'assets';assets.mkdir()
    tokenizer,formatter=assets/'tokenizer.json',assets/'chunking.py'
    from tokenizers import Tokenizer, models, pre_tokenizers
    from knowledge_atlas.core.graph_stage import records_for
    from knowledge_atlas.core.vector_chunks import chunks_for
    codec = Tokenizer(models.WordLevel({'[UNK]':0}, unk_token='[UNK]'))
    codec.pre_tokenizer = pre_tokenizers.Whitespace()
    codec.save(str(tokenizer))
    formatter.write_bytes((_assets()/'chunking.py').read_bytes())
    profile=json.loads((_assets()/'embedding-profile.json').read_text())
    tokenizer_digest = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    profile['profile']['endpoint']['tokenizer_sha256'] = tokenizer_digest
    profile['profile']['plugin_profile']['tokenizer_sha256'] = tokenizer_digest
    profile['profile_id']=hashlib.sha256(canonical(profile['profile']).encode()).hexdigest()
    profile_path = assets/'embedding-profile.json'
    profile_path.write_text(json.dumps(profile))

    def projection(content, owner=None):
        records = records_for((owner or store).read(content), content)
        chunks = chunks_for(records, profile_path, tokenizer, formatter)
        artifacts = {}
        for chunk in chunks:
            text = 'title: none | text: ' + chunk['text']
            digest = hashlib.sha256(text.encode()).hexdigest()
            artifacts[digest] = {'profile_id':profile['profile_id'],'profile':profile['profile'],'role':'document',
                                 'input':text,'input_sha256':digest,'raw768':[1.0]+[0.0]*767}
        return {'content_commit':content,'graph_commit':'controlled-'+content,'search_checkpoint':'controlled-'+content,
                'database':'admin/fixture','ready':True,'profile_id':profile['profile_id'],
                'record_count':len(records),'chunk_count':len(chunks),'vector_artifacts':artifacts}
    from knowledge_atlas import runtime
    monkeypatch.setattr(runtime, '_assets', lambda:assets)
    monkeypatch.setattr(decisions,'_assets',lambda:assets)
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    store=GitStore(tmp_path/'atlas');candidate=snapshot(store)
    content=review_acceptance(Publications(store), candidate,{'decision':'accept','kind':'synthetic','actor':'Fixture',
        'reason':'Controlled archive fixture','accepted_at':'2026-11-13T01:00:00Z','base_publication':None},profile)
    publication=Publications(store).publish(content,projection)
    return store,tokenizer,formatter,publication,profile,projection


def archive( portable, destination):
    return export_atlas(portable[0],destination,tokenizer=portable[1],formatter=portable[2])


def with_decisions(portable,tmp_path):
    store,_,_,publication,_,_=portable
    content=Publications(store).read(publication)['content_commit']
    candidate=propose_records(store,content,[],actor='Fixture',method='Explicit review',reason='Decision archive',
        recorded_at='2026-11-13T02:00:00Z',base_publication=publication)
    rejected=decide_candidate(context_for(tmp_path,store,candidate,'reject',publication))
    revision=context_for(tmp_path,store,candidate,'reject',publication)
    revision=replace(revision,request=replace(revision.request,action='candidate.revise',payload={
        **revision.request.payload,'entries':[]}))
    revised=revise_candidate(revision)
    accepted=decide_candidate(context_for(tmp_path,store,revised.data['candidate'],'accept',publication))
    return rejected,revised,accepted


def test_decision_roundtrip_without_service_db(portable,tmp_path):
    with_decisions(portable,tmp_path)
    path=tmp_path/'atlas.zip'; manifest=archive(portable,path)
    assert manifest['format']==3
    assert len(manifest['decisions']['entries'][manifest['root_package_id']])==4
    restored=restore_atlas(path,tmp_path/'restored')
    assert {item['receipt']['action'] for item in decision_inventory(restored)}=={'accept','reject','revise'}
    assert decision_inventory(restored)==decision_inventory(portable[0])
    assert Publications(restored).ready()==Publications(portable[0]).ready()
    for item in decision_inventory(restored):
        assert restored.closure(item['commit'])
    assert not list(restored.base.rglob('operations.sqlite3'))


def test_missing_decision_object_rejected(portable,tmp_path):
    with_decisions(portable,tmp_path)
    path=tmp_path/'original.zip';manifest=archive(portable,path)
    manifest['decisions']['entries'][manifest['root_package_id']][0]['receipt']['candidate']='a'*40
    damaged=tmp_path/'damaged.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(damaged,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    with pytest.raises(ValueError,match='decision'):
        restore_atlas(damaged,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


@pytest.mark.parametrize('version', [1, 2, 3.0])
def test_unsupported_archive_is_preserved(portable,tmp_path,version):
    path=tmp_path/'new.zip';manifest=archive(portable,path)
    manifest['format']=version
    legacy=tmp_path/'legacy.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(legacy,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    original = legacy.read_bytes()
    with pytest.raises(ValueError, match='unsupported archive format'):
        restore_atlas(legacy,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()
    assert legacy.read_bytes() == original


def test_restore_refuses_existing_destination(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    destination=tmp_path/'existing';destination.mkdir();(destination/'keep').write_text('retained')
    with pytest.raises(ValueError,match='destination exists'): restore_atlas(path,destination)
    assert (destination/'keep').read_text()=='retained'


def test_restored_documents_replay_without_inference(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    restored=restore_atlas(path,tmp_path/'restored')
    provider=EmbeddingProvider(restored.base/'assets/embedding-profile.json',restored.base/'vectors','http://127.0.0.1:1')
    artifact = next(iter(Publications(restored).read(portable[3])['vector_artifacts'].values()))
    result=provider.embed({'role':'document','model':portable[4]['model_namespace'],'dimensions':256,
                           'input':[artifact['input'].removeprefix('title: none | text: ')]})
    assert result['data'][0]['embedding']==[1.0]+[0.0]*255
    assert provider.stats['inference_calls']==0
    assert provider.stats['cache_hits']==1


def test_rebuild_maps_oldest_first_without_changing_history(portable,tmp_path):
    from knowledge_atlas.core.archive import rebuild_projections
    with_decisions(portable,tmp_path)
    store=portable[0];pubs=Publications(store);ready=pubs.ready()
    previous=pubs.read(ready)['previous_publication']
    built=[]
    class ControlledProjection:
        def build(self,content):
            built.append(content)
            return {**portable[5](content),'graph_commit':'rebuilt-'+content,'search_checkpoint':'rebuilt-'+content}
    result=rebuild_projections(store,ControlledProjection())
    mapping=json.loads((store.base/'rebuilt-projections.json').read_text())
    assert built==[pubs.read(previous)['content_commit'],pubs.read(ready)['content_commit']]
    assert result['ready']==ready
    assert mapping[ready]['graph_commit'].startswith('rebuilt-')
    assert pubs.ready()==ready
    assert pubs.read(ready)['graph_commit'].startswith('controlled-')


def test_restore_registers_only_after_rebuild(portable,tmp_path,monkeypatch):
    from knowledge_atlas.actions import archives
    from knowledge_atlas.contracts import ActionContext,ActionRequest,ActionResult,ServiceContext,AtlasError
    from knowledge_atlas.operations import database
    path=tmp_path/'atlas.zip';archive(portable,path)
    service=ServiceContext(tmp_path/'fresh-service')
    def rebuild(context):
        restored = GitStore(context.atlas_path)
        assert Publications(restored).ready()==portable[3]
        identity = restored.read(portable[3])['manifest']['package_id']
        assert context.configuration['graph']['database'] == 'admin/atlas-' + uuid.UUID(identity).hex
        with database(service) as db:
            assert db.execute('SELECT count(*) FROM atlases').fetchone()[0] == 0
        return ActionResult(data={'ready':portable[3],'publications':1})
    monkeypatch.setattr(archives,'rebuild_restored',rebuild)
    request=ActionRequest('archive.restore',request_id=str(uuid.uuid4()),payload={'archive':str(path),'path':str(tmp_path/'restored')})
    context=ActionContext(service,request,tmp_path/'restored',tmp_path/'attempt',{})
    result=archives.restore_archive(context)
    assert result.publication==portable[3]
    assert result.data['status']=='ready'
    with database(service) as db:
        assert db.execute('SELECT path FROM atlases').fetchone()[0]==str(tmp_path/'restored')
    def unavailable(context): raise AtlasError('projection_unavailable','controlled offline',503)
    monkeypatch.setattr(archives,'rebuild_restored',unavailable)
    failed=replace(context,atlas_path=tmp_path/'unbuilt',request=replace(request,payload={**request.payload,'path':str(tmp_path/'unbuilt')}))
    with pytest.raises(AtlasError,match='atlas_conflict'): archives.restore_archive(failed)
    with database(service) as db: assert db.execute('SELECT count(*) FROM atlases').fetchone()[0]==1
    assert Publications(GitStore(tmp_path/'unbuilt')).ready()==portable[3]
    fresh = replace(failed, service=ServiceContext(tmp_path/'failed-service'), atlas_path=tmp_path/'offline')
    with pytest.raises(AtlasError,match='projection_unavailable'): archives.restore_archive(fresh)
    with database(fresh.service) as db: assert db.execute('SELECT count(*) FROM atlases').fetchone()[0]==0


def test_submitted_restore_freezes_archive_bytes(portable,tmp_path,monkeypatch):
    from knowledge_atlas.actions import archives
    from knowledge_atlas.contracts import ActionRequest,ActionResult,ServiceContext
    from knowledge_atlas.operations import database,inspect_operation,resume,submit
    from knowledge_atlas.worker import run_next
    path=tmp_path/'atlas.zip';archive(portable,path)
    original=path.read_bytes()
    request=ActionRequest('archive.restore',request_id=str(uuid.uuid4()),payload={'archive':str(path),'path':str(tmp_path/'restore')})
    service=ServiceContext(tmp_path/'service')
    operation=submit(service,request)
    path.write_bytes(b'changed after submission')
    assert (operation.evidence/'archive-v3.zip').read_bytes()==original
    assert submit(service,request).operation_id==operation.operation_id
    monkeypatch.setattr(archives, 'rebuild_restored', lambda ctx: ActionResult(data={'ready': portable[3]}))
    # An explicit recovery handler can authorize replay when canonical installation
    # never started. Replay must consume the original frozen bytes.
    with database(service) as db:
        db.execute("UPDATE operations SET state='interrupted' WHERE operation_id=?", (operation.operation_id,))
    def reconcile(ctx):
        assert not ctx.atlas_path.exists()
        assert (ctx.previous_evidence / 'archive-v3.zip').read_bytes() == original
        return None
    service.recoveries['archive.restore'] = reconcile
    resume(service, operation.operation_id, str(uuid.uuid4()))
    run_next(service)
    assert inspect_operation(service, operation.operation_id).state == 'succeeded'
    assert not (operation.evidence / 'archive-v3.zip').exists()
    assert path.read_bytes() == b'changed after submission'


@pytest.mark.parametrize('limit,value,match', [
    ('MAX_ARCHIVE_ENTRIES', 1, 'directory'),
    ('MAX_DIRECTORY_BYTES', 32, 'directory'),
    ('MAX_MANIFEST_BYTES', 3, 'size limit'),
    ('MAX_MEMBER_BYTES', 80, 'size limit'),
    ('MAX_EXPANDED_BYTES', 140, 'size limit'),
])
def test_archive_inventory_limits(tmp_path, monkeypatch, limit, value, match):
    from knowledge_atlas.core import archive as codec

    path = tmp_path / 'fixture.zip'
    content = b'ordinary fixture text' * 5
    manifest = {'format': 1, 'sha256': {'notes.txt': hashlib.sha256(content).hexdigest()}}
    with zipfile.ZipFile(path, 'w') as output:
        output.writestr('notes.txt', content)
        output.writestr('archive.json', json.dumps(manifest))
    monkeypatch.setattr(codec, limit, value)
    with pytest.raises(ValueError, match=match):
        restore_atlas(path, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('extra', ['archive.json', 'tokenizer.json', 'other.txt'])
def test_archive_inventory_requires_unique_exact_names(portable, tmp_path, extra):
    path = tmp_path / 'fixture.zip'
    archive(portable, path)
    with zipfile.ZipFile(path, 'a') as output:
        if extra in output.namelist():
            content = output.read(extra)
            with pytest.warns(UserWarning, match='Duplicate'):
                output.writestr(extra, content)
            expected = 'duplicate archive member'
        else:
            output.writestr(extra, 'ordinary fixture')
            expected = 'inventory mismatch'
    with pytest.raises(ValueError, match=expected):
        restore_atlas(path, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_copy_limit_applies_as_bytes_arrive(monkeypatch):
    import io
    from knowledge_atlas.core import archive as codec

    monkeypatch.setattr(codec, 'COPY_CHUNK_BYTES', 3)
    target = io.BytesIO()
    with pytest.raises(ValueError, match='byte limit'):
        codec.copy_bounded(io.BytesIO(b'abcdefg'), target, 6)
    assert target.getvalue() == b'abcdef'


@pytest.mark.parametrize('kind', ['service', 'alias', 'hardlink', 'atlas', 'directory', 'size', 'plain', 'zip_without_manifest'])
def test_restore_refuses_unsuitable_inputs_without_attempt(tmp_path, monkeypatch, kind):
    from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext
    from knowledge_atlas.core import archive as codec
    from knowledge_atlas.operations import database, register_atlas, submit

    service = ServiceContext(tmp_path / 'service')
    atlas = tmp_path / 'registered'
    store = GitStore(atlas)
    register_atlas(service, store.read(snapshot(store))['manifest']['package_id'], atlas)
    path = tmp_path / 'notes'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'service':
        path = service.service_root / 'private-notes'
        path.write_text('fixture')
    elif kind in ('alias', 'hardlink'):
        original = service.service_root / 'private-notes'
        original.write_text('fixture')
        if kind == 'alias':
            path.symlink_to(original)
        else:
            path.hardlink_to(original)
    elif kind == 'atlas':
        path = atlas / 'notes'
        path.write_text('fixture')
    elif kind == 'zip_without_manifest':
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('notes.txt', 'ordinary file')
    else:
        path.write_text('fixture')
        if kind == 'size':
            monkeypatch.setattr(codec, 'MAX_ARCHIVE_BYTES', 3)
    req = ActionRequest('archive.restore', request_id=str(uuid.uuid4()),
                        payload={'archive': str(path), 'path': str(tmp_path / 'restored')})
    with pytest.raises(AtlasError) as error:
        submit(service, req)
    assert error.value.code == 'invalid_archive'
    with database(service) as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert list((service.service_root / 'attempts').iterdir()) == []


def test_concurrent_restore_stages_outside_transaction(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    from threading import Barrier
    from knowledge_atlas import operations
    from knowledge_atlas.contracts import ActionRequest, ServiceContext

    source = tmp_path / 'exports' / 'fixture.zip'
    source.parent.mkdir()
    with zipfile.ZipFile(source, 'w') as package:
        package.writestr('archive.json', '{}')
    service = ServiceContext(tmp_path / 'service')
    with operations.database(service):
        pass
    request = ActionRequest('archive.restore', request_id=str(uuid.uuid4()),
                            payload={'archive': str(source), 'path': str(tmp_path / 'restored')})
    stage = operations._stage_archive
    together = Barrier(2)

    def check(service, req, evidence):
        with sqlite3.connect(service.service_root / 'operations.sqlite3', timeout=1) as other:
            other.execute('BEGIN IMMEDIATE')
        stage(service, req, evidence)

    monkeypatch.setattr(operations, '_stage_archive', check)
    def concurrent_submit(_):
        together.wait(timeout=5)
        return operations.submit(service, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(concurrent_submit, range(2))
    assert first.operation_id == second.operation_id
    assert list((service.service_root / 'attempts').iterdir()) == [first.evidence.parent]
    assert (first.evidence / 'archive-v3.zip').read_bytes() == source.read_bytes()


def test_restore_staging_quota_and_terminal_cleanup(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from knowledge_atlas import operations
    from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext
    from knowledge_atlas.worker import run_next

    path = tmp_path / 'incomplete.zip'
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr('archive.json', '{}')
    service = ServiceContext(tmp_path / 'service')
    requests = [ActionRequest('archive.restore', request_id=str(uuid.uuid4()),
                              payload={'archive': str(path), 'path': str(tmp_path / str(i))})
                for i in range(2)]
    monkeypatch.setattr(operations, 'MAX_STAGED_ARCHIVE_BYTES', path.stat().st_size)
    barrier = Barrier(2)

    def submit(request):
        barrier.wait(timeout=5)
        try:
            return operations.submit(service, request)
        except AtlasError as error:
            assert error.code == 'archive_staging_full'
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, requests))
    assert results.count(None) == 1
    failed = next(result for result in results if result)
    run_next(service)
    assert operations.inspect_operation(service, failed.operation_id).error['code'] == 'invalid_archive'
    assert not (failed.evidence / 'archive-v3.zip').exists()
    assert (failed.evidence / 'error.json').is_file()
    assert operations.submit(service, requests[results.index(None)]).state == 'queued'


def test_unsupported_restore_resume_preserves_input(tmp_path):
    from knowledge_atlas import operations
    from knowledge_atlas.contracts import ActionRequest, AtlasError, ServiceContext
    from knowledge_atlas.worker import run_next

    path = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr('archive.json', '{}')
    service = ServiceContext(tmp_path / 'service')
    request = ActionRequest('archive.restore', request_id=str(uuid.uuid4()),
                            payload={'archive': str(path), 'path': str(tmp_path / 'restored')})
    operation = operations.submit(service, request)
    with operations.database(service) as db:
        db.execute("UPDATE operations SET state='running' WHERE operation_id=?", (operation.operation_id,))
    resume_id = str(uuid.uuid4())
    with pytest.raises(AtlasError) as rejected:
        operations.resume(service, operation.operation_id, resume_id)
    assert (rejected.value.code, rejected.value.status) == ('unsafe_resume', 409)
    record = operations.inspect_operation(service, operation.operation_id)
    assert (record.state, record.attempt) == ('interrupted', 1)
    assert not (record.evidence.parent / '2').exists()
    assert (record.evidence / 'archive-v3.zip').read_bytes() == path.read_bytes()

    # Simulate a resume that an older deployment already queued.
    service.recoveries['archive.restore'] = lambda ctx: None
    operations.resume(service, operation.operation_id, resume_id)
    service.recoveries.clear()
    run_next(service)
    assert operations.inspect_operation(service, operation.operation_id).state == 'interrupted'
    assert (record.evidence / 'archive-v3.zip').read_bytes() == path.read_bytes()


def test_restore_reconciles_retained_inputs_on_start(tmp_path):
    from knowledge_atlas import operations
    from knowledge_atlas.contracts import ActionRequest, ServiceContext

    path = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr('archive.json', '{}')
    service = ServiceContext(tmp_path / 'service')
    states = ('interrupted', 'succeeded', 'failed')
    inputs = []
    for state in states:
        request = ActionRequest('archive.restore', request_id=str(uuid.uuid4()),
                                payload={'archive': str(path), 'path': str(tmp_path / state)})
        record = operations.submit(service, request)
        inputs.append(record.evidence / 'archive-v3.zip')
        with operations.database(service) as db:
            db.execute('UPDATE operations SET state=? WHERE operation_id=?', (state, record.operation_id))
    orphan = service.service_root / 'attempts' / str(uuid.uuid4()) / '1' / 'archive-v3.zip'
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(path.read_bytes())

    old_copy = orphan.with_name('archive.zip')
    old_copy.write_bytes(b'untouched')
    dispatcher = operations.Dispatcher(service)
    dispatcher.start()
    dispatcher.stop()
    assert [staged.exists() for staged in inputs] == [True, False, False]
    assert not orphan.exists()
    assert old_copy.read_bytes() == b'untouched'
    assert path.is_file()


@pytest.mark.parametrize('limit', ['MAX_ARCHIVE_BYTES', 'MAX_ARCHIVE_ENTRIES', 'MAX_MEMBER_BYTES',
                                   'MAX_EXPANDED_BYTES', 'MAX_MANIFEST_BYTES', 'MAX_DIRECTORY_BYTES'])
def test_export_limits_leave_no_unrestorable_file(portable, tmp_path, monkeypatch, limit):
    from knowledge_atlas.core import archive as codec

    monkeypatch.setattr(codec, limit, 1)
    path = tmp_path / 'export.zip'
    with pytest.raises(ValueError, match='limit|too many'):
        archive(portable, path)
    assert not path.exists()


def test_full_inventory_preserves_decision_ref_order(portable, tmp_path):
    store, _, _, publication, _, _ = portable
    content = Publications(store).read(publication)['content_commit']
    candidate = propose_records(store, content, [], actor='Fixture', method='Review', reason='Archive',
                                recorded_at='2026-11-13T02:00:00Z', base_publication=publication)
    ids = ['aaaaaaaa-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-000000000002']
    accepted_id = decision_inventory(store)[0]['receipt']['decision_id']
    for identifier in ids:
        ctx = context_for(tmp_path, store, candidate, 'reject', publication)
        decide_candidate(replace(ctx, request=replace(ctx.request, request_id=identifier)))
    assert [e['receipt']['decision_id'] for e in decision_inventory(store)] == sorted([accepted_id, *ids])
    path = tmp_path/'decisions.zip'
    archive(portable, path)
    restored = restore_atlas(path, tmp_path/'restored')
    assert [e['receipt']['decision_id'] for e in decision_inventory(restored)] == sorted([accepted_id, *ids])


def test_restore_checks_uuid_publication_content_binding(portable, tmp_path):
    source = tmp_path/'source.zip'
    manifest = archive(portable, source)
    # A substituted inventory label cannot assign an existing Git history to a new UUID.
    manifest['root_package_id'] = str(uuid.uuid4())
    root = next(item for item in manifest['packages'] if item['bundle'] == 'root.bundle')
    root['package_id'] = manifest['root_package_id']
    modified = tmp_path/'substituted.zip'
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(modified, 'w') as result:
        for name in original.namelist():
            result.writestr(name, json.dumps(manifest) if name == 'archive.json' else original.read(name))
    with pytest.raises(ValueError, match='identity'):
        restore_atlas(modified, tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


@pytest.mark.parametrize('wrong_owner', ['ready', 'previous', None])
def test_restore_root_publication_owner(portable, tmp_path, monkeypatch, wrong_owner):
    store, _, _, previous, profile, projection = portable
    pubs = Publications(store)
    base = pubs.read(previous)['content_commit']
    child = GitStore(tmp_path/'child')
    child_commit = snapshot(child)
    from test_packages import layout, selection
    child_pubs = Publications(child)
    child_content = review_acceptance(child_pubs, child_commit, {'decision':'accept','kind':'synthetic','actor':'Fixture',
        'reason':'Review child','accepted_at':'2026-11-13T01:00:00Z','base_publication':None}, profile)
    child_publication = child_pubs.publish(child_content, lambda oid:projection(oid, child))
    proposed = layout(store, selection(store, child, child_publication, 'materials'))
    content = review_acceptance(pubs, proposed, {'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture',
        'reason': 'Review child publication', 'accepted_at': '2026-11-13T03:00:00Z',
        'base_publication': previous}, profile)
    ready = pubs.publish(content, projection)
    path, destination = tmp_path/'backup.zip', tmp_path/'restored'
    archive(portable, path)

    if wrong_owner is not None:
        selected = ready if wrong_owner == 'ready' else previous
        verify = Publications._read_receipt

        def mismatch(self, publication, manifest):
            receipt = verify(self, publication, manifest)
            if publication == selected:
                # Inject a different owner at the verified snapshot boundary.
                manifest['package_id'] = str(uuid.uuid4())
            return receipt

        monkeypatch.setattr(Publications, '_read_receipt', mismatch)
        with pytest.raises(ValueError, match='publication.*identity'):
            restore_atlas(path, destination)
        assert not destination.exists()
        return

    restored = restore_atlas(path, destination)
    assert git(restored.root, 'rev-parse', 'HEAD') == git(store.root, 'rev-parse', 'HEAD') != ready
    restored_pubs = Publications(restored)
    assert restored_pubs.ready() == ready
    assert restored_pubs.read(ready)['previous_publication'] == previous
    assert restored_pubs.read(previous)['content_commit'] == base
    child_pin, = [mod for mod in restored.read(content)['manifest']['modules'].values() if mod.get('kind') == 'atlas']
    assert child_pin['commit'] == child_content and child_pin['publication'] == child_publication


@pytest.mark.parametrize('damage', ['unmarked-parent', 'missing-project-manifest', 'missing-receipt', 'missing-vectors', 'corrupt-vector', 'missing-one-vector',
                                    'missing-child-inventory', 'forged-child-decision'])
def test_staged_child_history_is_verified(portable, tmp_path, damage):
    from knowledge_atlas.core.publication import commit_files
    from test_packages import layout, selection
    parent, _, _, previous, profile, projection = portable
    child = GitStore(tmp_path/'child')
    proposal = snapshot(child)
    child_pubs = Publications(child)
    decision = {'decision':'accept', 'kind':'synthetic', 'actor':'Fixture', 'reason':'Archive test',
                'base_publication':None, 'accepted_at':'2026-11-13T02:00:00Z'}
    content = review_acceptance(child_pubs, proposal, decision, profile)
    wrapper = child_pubs.publish(content, lambda oid:projection(oid, child))
    parent_proposal = layout(parent, selection(parent, child, wrapper))
    parent_content = review_acceptance(Publications(parent), parent_proposal, {**decision, 'base_publication':previous}, profile)
    Publications(parent).publish(parent_content, projection)
    # Retain a child decision in its own namespace.
    decide_candidate(context_for(tmp_path, child, layout(child), 'reject', wrapper))
    original = tmp_path/'original.zip'
    inventory = archive(portable, original)
    child_id = child.read(content)['manifest']['package_id']
    entries = inventory['decisions']['entries']
    if damage in ('missing-child-inventory', 'forged-child-decision'):
        assert set(entries) == {inventory['root_package_id'], child_id}
        if damage == 'missing-child-inventory':
            del entries[child_id]
        else:
            entries[child_id][0]['receipt']['actor'] = 'Substitution'
    else:
        if damage == 'unmarked-parent':
            unmarked = commit_files(child.root, proposal, {'package.json':{'package_id':child_id}},
                                    [proposal], 'Unmarked history')
            bad = commit_files(child.root, proposal, {}, [unmarked], 'Current tip with unsupported ancestor')
        elif damage == 'missing-project-manifest':
            bad = commit_files(child.root, proposal, {}, [proposal], 'Missing child atlas manifest',
                               remove_paths=['manifest.json'])
        elif damage == 'missing-receipt':
            bad = commit_files(child.root, content, {}, [content], 'Without wrapper document')
            git(child.root, 'update-ref', Publications.ref, bad)
        else:
            receipt = child_pubs.read(wrapper)
            if damage == 'missing-vectors':
                del receipt['vector_artifacts']
            elif damage == 'missing-one-vector':
                assert len(receipt['vector_artifacts']) > 1
                receipt['vector_artifacts'].popitem()
            else:
                next(iter(receipt['vector_artifacts'].values()))['raw768'] = [0]
            bad = commit_files(child.root, content, {'publication.json':receipt}, [content], 'Bad vectors')
        git(child.root, 'update-ref', 'refs/fixture/history', bad)
    changed = {}
    if damage not in ('missing-child-inventory', 'forged-child-decision'):
        name = next(row['bundle'] for row in inventory['packages'] if row['package_id'] == child_id)
        bundle = tmp_path/'child.bundle'
        git(child.root, 'bundle', 'create', str(bundle), '--all')
        changed[name] = bundle.read_bytes()
        inventory['sha256'][name] = hashlib.sha256(changed[name]).hexdigest()
    path = tmp_path/'changed.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, 'w') as target:
        for name in source.namelist():
            target.writestr(name, json.dumps(inventory) if name == 'archive.json' else changed.get(name, source.read(name)))
    original_bytes = path.read_bytes()
    with pytest.raises(ValueError):
        restore_atlas(path, tmp_path/'restored')
    assert not (tmp_path/'restored').exists()
    assert path.read_bytes() == original_bytes


def test_export_inventory_matches_mirrored_refs(portable, tmp_path, monkeypatch):
    from knowledge_atlas.core import archive as codec
    store, _, _, ready, _, _ = portable
    from test_packages import layout
    candidate = layout(store)
    actual_git = codec.git
    def add_decision_before_mirror(repo, *args, **kwargs):
        if args[:2] == ('clone', '--mirror') and str(store.root) in args:
            decide_candidate(context_for(tmp_path, store, candidate, 'reject', ready))
        return actual_git(repo, *args, **kwargs)
    monkeypatch.setattr(codec, 'git', add_decision_before_mirror)
    path = tmp_path/'raced.zip'
    archive(portable, path)
    restored = restore_atlas(path, tmp_path/'restored')
    assert len(decision_inventory(restored)) == 2


@pytest.mark.parametrize(('location', 'fields'), [
    ('ready', {'ready':False}),
    ('ready', {'ready':1}),
    ('history', {'graph_commit':'', 'search_checkpoint':''}),
    ('child', {'search_checkpoint':'different-graph-revision'}),
])
def test_invalid_projection_receipts_rejected_before_archive_exposure(portable, tmp_path, location, fields):
    from knowledge_atlas.core.publication import commit_files
    from test_packages import layout, selection

    root, _, _, ready, profile, projection = portable
    owner = root
    if location == 'child':
        owner = GitStore(tmp_path/'child')
        pubs = Publications(owner)
        proposal = snapshot(owner)
        content = review_acceptance(pubs, proposal, {'decision':'accept','kind':'synthetic','actor':'Fixture',
            'reason':'Review child','accepted_at':'2026-11-13T01:00:00Z','base_publication':None}, profile)
        selected = pubs.publish(content, lambda oid: projection(oid, owner))
        pending = layout(root, selection(root, owner, selected))
    else:
        pubs = Publications(root)
        selected = ready

    original = tmp_path/'original.zip'
    inventory = archive(portable, original)
    receipt = pubs.read(selected)
    content = receipt['content_commit']
    bad = commit_files(owner.root, content, {'publication.json':{**receipt, **fields}},
                       [content], 'Inconsistent projection receipt')
    if location == 'ready':
        git(root.root, 'update-ref', pubs.ref, bad, selected)
        inventory['ready'] = bad
    else:
        git(owner.root, 'update-ref', 'refs/fixture/retained', bad)

    if location == 'child':
        manifest = root.read(pending)['manifest']
        child, = [mod for mod in manifest['modules'].values() if mod.get('kind') == 'atlas']
        child['publication'] = bad
        pending = commit_files(root.root, pending, {'manifest.json':manifest}, [pending], 'Select bad child wrapper')
        git(root.root, 'update-ref', 'refs/poc9/proposals/'+pending, pending)

    # Rebundle the raw Git refs and refresh the ZIP digests independently of export validation.
    members = {}
    for store in dict.fromkeys((root, owner)):
        identity = json.loads(git(store.root, 'show', 'HEAD:package.json'))['package_id']
        name = next(row['bundle'] for row in inventory['packages'] if row['package_id'] == identity)
        bundle = tmp_path/name
        git(store.root, 'bundle', 'create', str(bundle), '--all')
        members[name] = bundle.read_bytes()
        inventory['sha256'][name] = hashlib.sha256(members[name]).hexdigest()
    members['archive.json'] = json.dumps(inventory).encode()
    damaged = tmp_path/'damaged.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, 'w') as target:
        for name in source.namelist():
            target.writestr(name, members[name] if name in members else source.read(name))

    before = damaged.read_bytes()
    with pytest.raises(ValueError, match='projection is not ready'):
        restore_atlas(damaged, tmp_path/'restored')
    assert damaged.read_bytes() == before
    assert not (tmp_path/'restored').exists()
    with pytest.raises(ValueError, match='projection is not ready'):
        archive(portable, tmp_path/'export.zip')
    assert not (tmp_path/'export.zip').exists()
    if location == 'child':
        with pytest.raises(ValueError, match='projection is not ready'):
            root.read(pending)


def test_restore_rejects_missing_acceptance_ref_even_with_matching_outer_inventory(portable, tmp_path):
    store = portable[0]
    original = tmp_path / 'current.zip'
    manifest = archive(portable, original)
    project = manifest['root_package_id']
    entry, = manifest['decisions']['entries'][project]
    git(store.root, 'update-ref', '-d', entry['ref'])
    bundle = tmp_path / 'missing.bundle'
    git(store.root, 'bundle', 'create', str(bundle), '--all')
    member = next(p['bundle'] for p in manifest['packages'] if p['package_id'] == project)
    payload = bundle.read_bytes()
    manifest['sha256'][member] = hashlib.sha256(payload).hexdigest()
    manifest['decisions']['entries'][project] = []
    damaged = tmp_path / 'damaged.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, 'w') as dest:
        for name in source.namelist():
            dest.writestr(name, json.dumps(manifest) if name == 'archive.json' else
                          payload if name == member else source.read(name))
    before = damaged.read_bytes()
    with pytest.raises(ValueError, match='decision'):
        restore_atlas(damaged, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()
    assert damaged.read_bytes() == before
