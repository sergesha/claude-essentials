"""Portable canonical decisions; controlled vectors are not native qualification."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import uuid
import zipfile
import pytest
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
    tokenizer.write_text('controlled tokenizer fixture');formatter.write_text('# controlled formatter fixture\n')
    profile=json.loads((_assets()/'embedding-profile.json').read_text())
    profile['profile']['endpoint']['tokenizer_sha256']=hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    profile['profile']['formatter_sha256']=hashlib.sha256(formatter.read_bytes()).hexdigest()
    profile['profile_id']=hashlib.sha256(canonical(profile['profile']).encode()).hexdigest()
    (assets/'embedding-profile.json').write_text(json.dumps(profile))
    text='title: none | text: Retained controlled document'
    artifact={'profile_id':profile['profile_id'],'profile':profile['profile'],'role':'document','input':text,
        'input_sha256':hashlib.sha256(text.encode()).hexdigest(),'raw768':[1.0]+[0.0]*767}
    def projection(content):
        return {'content_commit':content,'graph_commit':'controlled-'+content,'search_checkpoint':'controlled-'+content,
                'database':'admin/fixture','ready':True,'profile_id':profile['profile_id'],
                'record_count':1,'chunk_count':1,'vector_artifacts':{'fixture':artifact}}
    monkeypatch.setattr(decisions,'_assets',lambda:assets)
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    store=GitStore(tmp_path/'atlas');candidate=snapshot(store)
    content=Publications(store).review(candidate,{'decision':'accept','kind':'synthetic','actor':'Fixture',
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
    assert manifest['format']==2
    assert len(manifest['decisions']['entries'])==3
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
    manifest['decisions']['entries'][0]['receipt']['candidate']='a'*40
    damaged=tmp_path/'damaged.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(damaged,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    with pytest.raises(ValueError,match='decision'):
        restore_atlas(damaged,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


def test_legacy_archive_does_not_invent_decisions(portable,tmp_path):
    path=tmp_path/'new.zip';manifest=archive(portable,path)
    manifest['format']=1;manifest.pop('decisions',None)
    legacy=tmp_path/'legacy.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(legacy,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    restored=restore_atlas(legacy,tmp_path/'restored')
    assert decision_inventory(restored)==[]
    assert Publications(restored).ready()==portable[3]


def test_restore_refuses_existing_destination(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    destination=tmp_path/'existing';destination.mkdir();(destination/'keep').write_text('retained')
    with pytest.raises(ValueError,match='destination exists'): restore_atlas(path,destination)
    assert (destination/'keep').read_text()=='retained'


def test_restored_documents_replay_without_inference(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    restored=restore_atlas(path,tmp_path/'restored')
    provider=EmbeddingProvider(restored.base/'assets/embedding-profile.json',restored.base/'vectors','http://127.0.0.1:1')
    result=provider.embed({'role':'document','model':portable[4]['model_namespace'],'dimensions':256,
                           'input':['Retained controlled document']})
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
    assert (operation.evidence/'archive.zip').read_bytes()==original
    assert submit(service,request).operation_id==operation.operation_id
    monkeypatch.setattr(archives, 'rebuild_restored', lambda ctx: ActionResult(data={'ready': portable[3]}))
    # An explicit recovery handler can authorize replay when canonical installation
    # never started. Replay must consume the original frozen bytes.
    with database(service) as db:
        db.execute("UPDATE operations SET state='interrupted' WHERE operation_id=?", (operation.operation_id,))
    def reconcile(ctx):
        assert not ctx.atlas_path.exists()
        assert (ctx.previous_evidence / 'archive.zip').read_bytes() == original
        return None
    service.recoveries['archive.restore'] = reconcile
    resume(service, operation.operation_id, str(uuid.uuid4()))
    run_next(service)
    assert inspect_operation(service, operation.operation_id).state == 'succeeded'
    assert not (operation.evidence / 'archive.zip').exists()
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
    assert (first.evidence / 'archive.zip').read_bytes() == source.read_bytes()


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
    assert not (failed.evidence / 'archive.zip').exists()
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
    assert (record.evidence / 'archive.zip').read_bytes() == path.read_bytes()

    # Simulate a resume that an older deployment already queued.
    service.recoveries['archive.restore'] = lambda ctx: None
    operations.resume(service, operation.operation_id, resume_id)
    service.recoveries.clear()
    run_next(service)
    assert operations.inspect_operation(service, operation.operation_id).state == 'interrupted'
    assert (record.evidence / 'archive.zip').read_bytes() == path.read_bytes()


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
        inputs.append(record.evidence / 'archive.zip')
        with operations.database(service) as db:
            db.execute('UPDATE operations SET state=? WHERE operation_id=?', (state, record.operation_id))
    for retained in inputs:
        retained.write_bytes(path.read_bytes())  # Simulate v0.3.1 retention.
    orphan = service.service_root / 'attempts' / str(uuid.uuid4()) / '1' / 'archive.zip'
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(path.read_bytes())

    dispatcher = operations.Dispatcher(service)
    dispatcher.start()
    dispatcher.stop()
    assert [staged.exists() for staged in inputs] == [True, False, False]
    assert not orphan.exists()
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


def test_full_inventory_preserves_legacy_mixed_case_ref_order(portable, tmp_path):
    store, _, _, publication, _, _ = portable
    content = Publications(store).read(publication)['content_commit']
    candidate = propose_records(store, content, [], actor='Fixture', method='Review', reason='Archive',
                                recorded_at='2026-11-13T02:00:00Z', base_publication=publication)
    ids = ['BBBBBBBB-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-000000000002']
    for identifier in ids:
        ctx = context_for(tmp_path, store, candidate, 'reject', publication)
        decide_candidate(replace(ctx, request=replace(ctx.request, request_id=identifier)))
    assert [e['receipt']['decision_id'] for e in decision_inventory(store)] == ids
    path = tmp_path/'mixed-case.zip'
    archive(portable, path)
    restored = restore_atlas(path, tmp_path/'restored')
    assert [e['receipt']['decision_id'] for e in decision_inventory(restored)] == ids
