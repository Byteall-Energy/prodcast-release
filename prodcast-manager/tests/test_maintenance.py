import copy
import ast
import importlib.util
import io
import json
import os
import subprocess
import shutil
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile
import pytest
from prodcast_manager import backup_archive as archive
from prodcast_manager.directory import app_directory
from prodcast_manager.engine import Engine,RESOURCES
from prodcast_manager.engine import plan
from prodcast_manager.config import example,topology_hash
from prodcast_manager.maintenance import Maintenance,read_backup

DATABASE_METADATA={'major':18,'extensions':{'plpgsql':'1.0'},'applied_migrations':[['apiapp','0001_initial']],'bytes':1024}


@pytest.fixture
def backup(tmp_path):
    data=tmp_path/'source';data.mkdir()
    for name in archive.FILES-{'manifest.json'}:
        p=data/name;p.parent.mkdir(exist_ok=True);p.write_bytes((name*200).encode())
    record={'schema':1,'database_major':18,'version':'v0.5.0-rc.2','manifest':'a'*64,'site':{},'vault':{'installation_id':'test','secrets':{'FERNET_KEY':'private-test-value'}}}
    (data/'recovery.json').write_text(json.dumps(record))
    z=tmp_path/'backup.zip';archive.pack(data,z)
    encrypted=tmp_path/'backup.pcbackup';archive.encrypt(z,encrypted,'password-for-test')
    return data,z,encrypted,record


def test_backup_roundtrip_excludes_heavy_files_and_hides_secrets(backup,tmp_path):
    data,z,encrypted,record=backup
    assert b'private-test-value' not in encrypted.read_bytes()
    plain=tmp_path/'decrypted.zip';archive.decrypt(encrypted,plain,'password-for-test')
    result=archive.unpack(plain,tmp_path/'restored');assert result==record
    for name in archive.FILES-{'manifest.json'}:assert (data/name).read_bytes()==(tmp_path/'restored'/name).read_bytes()
    assert not any('ollama' in name or 'docker' in name for name in zipfile.ZipFile(plain).namelist())


@pytest.mark.parametrize('damage',['password','bit','truncate','append','header'])
def test_backup_rejects_tampering_and_removes_partial_plaintext(backup,tmp_path,damage):
    _,_,encrypted,_=backup;data=bytearray(encrypted.read_bytes());password='password-for-test'
    if damage=='password':password='another-password'
    if damage=='bit':data[len(data)//2]^=1
    if damage=='truncate':data=data[:-1]
    if damage=='append':data+=b'x'
    if damage=='header':data[0]^=1
    encrypted.write_bytes(data);dest=tmp_path/'plain'
    with pytest.raises(ValueError):archive.decrypt(encrypted,dest,password)
    assert not dest.exists()


def test_decrypt_does_not_delete_existing_destination(backup,tmp_path):
    dest=tmp_path/'keep';dest.write_text('keep')
    with pytest.raises(ValueError):archive.decrypt(backup[2],dest,'password-for-test')
    assert dest.read_text()=='keep'


def test_multiframe_backup_and_atomic_output(tmp_path):
    source=tmp_path/'source';source.write_bytes(os.urandom(archive.CHUNK*2+11));dest=tmp_path/'encrypted'
    archive.encrypt(source,dest,'password-for-test');plain=tmp_path/'plain';archive.decrypt(dest,plain,'password-for-test')
    assert archive.sha(source)==archive.sha(plain)
    with pytest.raises(ValueError):archive.encrypt(source,dest,'password-for-test')


@pytest.mark.parametrize('entry',['../escape','model.gguf','db/prodcast2.dump'])
def test_backup_rejects_extra_and_duplicate_entries(backup,tmp_path,entry):
    with zipfile.ZipFile(backup[1],'a') as z:z.writestr(entry,b'bad')
    with pytest.raises(ValueError):archive.unpack(backup[1],tmp_path/'bad')


def test_ldap_mapping_enables_group_tls_and_omits_bind_password():
    value={'enabled':True,'url':'ldaps://dc.example.com','domain':'example.com','base_dn':'DC=example,DC=com',
           'group_dn':'CN=ProdCast,DC=example,DC=com','bind_dn':'svc@example.com','bind_password':'private-bind-password','ca_pem':''}
    mapped=app_directory(value)
    assert mapped['mode']=='ldaps' and mapped['include_nested_groups'] and mapped['group_filter_enabled']
    assert mapped['user_dn_template']=='%(user)s@example.com'
    assert 'private-bind-password' not in json.dumps(mapped)
    assert app_directory(None) is None and app_directory({'enabled':False})=={'mode':'disabled'}


spec=importlib.util.spec_from_file_location('maintenance_linux_test',RESOURCES/'maintenance_linux.py')
linux=importlib.util.module_from_spec(spec);spec.loader.exec_module(linux)


@pytest.mark.parametrize('mode',['update','backup'])
def test_server_backup_plans_do_not_archive_worker_installations(mode):
    actions=plan(mode,False,('worker1','worker2','worker3'))
    assert ('db','backup') in actions and ('app','backup') in actions
    assert not any(role.startswith('worker') and action=='backup' for role,action in actions)
    if mode=='update':
        assert actions.index(('worker1','stop'))<actions.index(('worker1','install'))


@pytest.mark.parametrize('server_only',[False,True])
def test_media_export_excludes_models_but_keeps_workflows_and_inputs(tmp_path,monkeypatch,server_only):
    if not shutil.which('tar'):pytest.skip('tar required')
    media=tmp_path/'media';config=tmp_path/'release'/'app';stage=tmp_path/'stage'
    config.mkdir(parents=True);stage.mkdir();(config/'app.env').write_text('test-config')
    for name in ('workflows/1.py','workflows/1.py.sig','workflows/1.ipynb','external_source_files/input.csv','models_files/large.rsa','official_models/nested/model.rsa'):
        path=media/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
    def run(args):
        if args[:3]==['docker','volume','inspect']:return json.dumps([{'Mountpoint':str(media)}])
        subprocess.run([str(a) for a in args],check=True,capture_output=True)
        return ''
    monkeypatch.setattr(linux.os,'chown',lambda *a:None,raising=False)
    m=SimpleNamespace(P={'role':'app','operation':'export','stage':str(stage)},ST={'version':'v1'},STATE=tmp_path/'state',run=run,old_target=lambda:config.parent,digest=archive.sha)
    if server_only:
        m.P['server_backup_only']=True
        (stage/'media.tar.gz').write_bytes(b'uploaded-restore-source')
    result=linux.export_data(m)
    exported=Path(result['server_backup'])/'media.tar.gz' if server_only else stage/'media.tar.gz'
    with tarfile.open(exported,'r:gz') as tar:
        names={str(Path(i.name)).replace('\\','/').removeprefix('./') for i in tar if i.isfile()}
    assert names=={'workflows/1.py','workflows/1.py.sig','workflows/1.ipynb','external_source_files/input.csv'}
    if server_only:
        assert result['staged'] is False and 'files' not in result
        assert (stage/'media.tar.gz').read_bytes()==b'uploaded-restore-source'
    else:assert result['files']['media.tar.gz']['sha256']==archive.sha(stage/'media.tar.gz')
    assert (media/'models_files/large.rsa').is_file()

    # Exercise the separate automatic-update backup implementation as well.
    state=tmp_path/'update-state';state.mkdir();(state/'state.json').write_text('{}')
    tree=ast.parse((RESOURCES/'linux.py').read_text('utf-8'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='backup')
    namespace=dict(STATE=state,ROOT=tmp_path,P={'role':'app','operation':'update'},ST={'version':'v1'},
                   old_target=lambda:config.parent,run=run,json=json,shutil=shutil,digest=archive.sha,
                   write=lambda path,value:path.write_text(value))
    exec(compile(ast.Module(body=[function],type_ignores=[]),'linux.py','exec'),namespace)
    result=namespace['backup']()
    with tarfile.open(Path(result['path'])/'media.tar.gz','r:gz') as tar:
        update_names={str(Path(i.name)).replace('\\','/').removeprefix('./') for i in tar if i.isfile()}
    assert update_names==names
    assert (state/'backups/update/state.json').exists()


@pytest.mark.parametrize('scope',['legacy-full-media',linux.MEDIA_SCOPE])
def test_media_restore_preserves_separate_models_only_for_new_backups(tmp_path,monkeypatch,scope):
    media=tmp_path/'media';stage=tmp_path/'stage';stage.mkdir()
    for name in ('models_files/model.rsa','official_models/model.rsa','workflows/stale.py','external_source_files/stale.csv'):
        path=media/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('keep-model-or-remove-stale')
    source=stage/'media.tar.gz'
    with tarfile.open(source,'w:gz') as tar:
        item=tarfile.TarInfo('./workflows/restored.py');item.size=4;tar.addfile(item,io.BytesIO(b'code'))
    monkeypatch.setattr(linux.os,'chown',lambda *a:None,raising=False)
    m=SimpleNamespace(P={'role':'app','stage':str(stage),'media_scope':scope,'restore_manifest':'manifest','restore_file':{'name':source.name,'bytes':source.stat().st_size,'sha256':archive.sha(source)}},ST={'manifest':'manifest'},digest=archive.sha,run=lambda args:json.dumps([{'Mountpoint':str(media)}]))
    linux.restore_data(m)
    assert (media/'workflows/restored.py').read_bytes()==b'code'
    assert not (media/'workflows/stale.py').exists()
    assert not (media/'external_source_files/stale.csv').exists()
    assert (media/'models_files/model.rsa').exists()==(scope==linux.MEDIA_SCOPE)
    assert (media/'official_models/model.rsa').exists()==(scope==linux.MEDIA_SCOPE)


def test_new_media_scope_rejects_model_entries_before_restore(tmp_path):
    path=tmp_path/'media.tar.gz'
    with tarfile.open(path,'w:gz') as tar:tar.addfile(tarfile.TarInfo('./models_files/file.rsa'))
    with pytest.raises(RuntimeError,match='Model files'):linux.validate_media(path,exclude_models=True)


def test_new_recovery_schema_roundtrips_and_requires_known_scope(backup,tmp_path):
    data,_,_,record=backup
    record.update(schema=2,media_scope=linux.MEDIA_SCOPE)
    (data/'recovery.json').write_text(json.dumps(record))
    path=tmp_path/'new.zip';archive.pack(data,path)
    assert archive.unpack(path,tmp_path/'new-data')['media_scope']==linux.MEDIA_SCOPE
    record['media_scope']='unknown';(data/'recovery.json').write_text(json.dumps(record))
    path=tmp_path/'unknown.zip';archive.pack(data,path)
    with pytest.raises(ValueError,match='media scope'):archive.unpack(path,tmp_path/'bad-scope')


def test_portable_restore_sends_model_exclusion_scope_to_validation_and_restore(backup,tmp_path):
    data,_,_,record=backup
    record.update(schema=2,media_scope=linux.MEDIA_SCOPE)
    (data/'recovery.json').write_text(json.dumps(record))
    plain=tmp_path/'scoped.zip';archive.pack(data,plain)
    encrypted=tmp_path/'scoped.pcbackup';archive.encrypt(plain,encrypted,'password-for-test')
    calls=[]
    class CaptureRemote(FakeRemote):
        def action(self,payload,action,*args):
            calls.append((self.role,action,dict(payload)))
            return super().action(payload,action,*args)
    FakeRemote.fail=None
    e=fake_engine(tmp_path/'site');e.factory=CaptureRemote;e.vault.data['secrets']=record['vault']['secrets']
    Maintenance(e).run('restore',encrypted,'password-for-test')
    data_calls=[p for r,a,p in calls if a in ('data-validate','data-restore')]
    assert len(data_calls)==6
    assert all(p['media_scope']==linux.MEDIA_SCOPE for p in data_calls)


@pytest.mark.parametrize('kind',['symlink','hardlink','parent','absolute'])
def test_restore_rejects_unsafe_media_before_modifying_data(tmp_path,kind):
    path=tmp_path/'media.tar.gz'
    with tarfile.open(path,'w:gz') as tar:
        i=tarfile.TarInfo({'parent':'../escape','absolute':'/escape'}.get(kind,'file'))
        if kind in ('symlink','hardlink'):i.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE;i.linkname='/etc/shadow'
        tar.addfile(i)
    with pytest.raises(RuntimeError):linux.validate_media(path)


def test_reset_keep_never_removes_storage(monkeypatch,tmp_path):
    monkeypatch.setattr(linux.shutil,'which',lambda n:'/bin/docker')
    called=[]
    def run(args):
        called.append(args)
        return 'a'*12 if args[:3]==['docker','ps','-aq'] else ''
    m=SimpleNamespace(P={'role':'db','mode':'reset-keep','operation':'test'},ST={'version':'v1','operation':'stale','steps':{'old':1}},STATE=tmp_path,run=run,save=lambda:None)
    r=linux.reset(m)
    assert r['data_preserved'] and m.ST['steps']=={} and m.ST['operation']==''
    assert called[-2]==['docker','stop','--time','90','a'*12] and called[-1]==['docker','rm','a'*12]
    assert not any('volume' in c or 'prune' in c for c in called)


def test_media_normal_files_accepted(tmp_path):
    path=tmp_path/'media.tar.gz'
    with tarfile.open(path,'w:gz') as tar:
        i=tarfile.TarInfo('./uploads/test.txt');i.size=4;tar.addfile(i,io.BytesIO(b'test'))
    linux.validate_media(path)


class FakeRemote:
    instances=[];fail=None
    def __init__(self,role,*args):self.role=role;self.actions=[];self.stage='/stage';self.instances.append(self)
    def probe(self):return {}
    def prepare_stage(self):pass
    def put_bytes(self,*a):pass
    def put(self,*a):pass
    def cleanup(self):pass
    def close(self):pass
    def action(self,payload,action,*a):
        self.actions.append(action)
        if self.fail==(self.role,action):raise RuntimeError('simulated failure')
        if action=='preflight':return {'managed':True,'version':'v0.5.0-rc.2','manifest':'a'*64}
        if action=='data-prepare':return {'switched':False,'database':'prodcast_restore_'+payload['operation'][:24]}
        if action=='data-migrate':return {'database':payload['restore_staging_database'],'migrated':True}
        return {}


def fake_engine(tmp_path):
    c=example();c['install_ai']=False
    for n,(role,h) in enumerate(c['hosts'].items(),11):h.update(address='192.168.99.'+str(n),auth='password',fingerprint='SHA256:'+'A'*43)
    c.update(public_url='https://prodcast.example.test',admin_ip='192.168.99.1')
    vault=SimpleNamespace(data={'topology':topology_hash(c),'installation_id':'test'},save=lambda:None)
    return Engine(c,vault,None,tmp_path,lambda s:None,remote_factory=FakeRemote)


def test_reset_preflights_every_host_before_removal_and_preserves_profile(tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=None;e=fake_engine(tmp_path)
    before=copy.deepcopy(e.vault.data);Maintenance(e).run('reset-full')
    assert e.vault.data==before
    assert all(r.actions[:2]==['preflight','maintenance-claim'] for r in FakeRemote.instances)
    assert all('reset' in r.actions for r in FakeRemote.instances)
    assert json.loads((tmp_path/'maintenance-journal.json').read_text())['status']=='complete'


def test_reset_failure_preserves_journal_and_resumes_same_operation(tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=('db','reset');e=fake_engine(tmp_path)
    with pytest.raises(RuntimeError):Maintenance(e).run('reset-full')
    journal=json.loads((tmp_path/'maintenance-journal.json').read_text());assert journal['status']=='failed'
    with pytest.raises(ValueError,match='previous maintenance'):Maintenance(e).run('reset-keep')
    FakeRemote.fail=None;Maintenance(e).run('reset-full')
    done=json.loads((tmp_path/'maintenance-journal.json').read_text())
    assert done['operation']==journal['operation'] and done['status']=='complete'


def test_failed_preflight_never_resets_any_host(tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=('db','preflight')
    with pytest.raises(RuntimeError):Maintenance(fake_engine(tmp_path)).run('reset-full')
    assert not any('reset' in r.actions for r in FakeRemote.instances)
    FakeRemote.fail=None


def test_release_failure_does_not_repeat_completed_reset(tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=('app','maintenance-release');e=fake_engine(tmp_path)
    with pytest.raises(RuntimeError):Maintenance(e).run('reset-full')
    journal=json.loads((tmp_path/'maintenance-journal.json').read_text())
    assert journal['work_complete'] and journal['status']=='failed'
    FakeRemote.instances=[];FakeRemote.fail=None;Maintenance(e).run('reset-full')
    assert not any('reset' in r.actions for r in FakeRemote.instances)


def test_partial_restore_stays_stopped_and_can_resume(backup,tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=('app','data-restore');e=fake_engine(tmp_path/'site')
    e.vault.data['secrets']=backup[3]['vault']['secrets']
    with pytest.raises(RuntimeError):Maintenance(e).run('restore',backup[2],'password-for-test')
    assert not any('maintenance-resume' in r.actions for r in FakeRemote.instances)
    journal=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert journal['quiesced'] and journal['steps']==['safety-db','safety-app']
    FakeRemote.instances=[];FakeRemote.fail=None;Maintenance(e).run('restore',backup[2],'password-for-test')
    app=next(r for r in FakeRemote.instances if r.role=='app')
    assert 'drain' not in app.actions and 'data-export' not in app.actions
    assert app.actions.index('maintenance-stop')<app.actions.index('data-restore')<app.actions.index('maintenance-resume')


def test_restore_wrong_identity_never_stops_services(backup,tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=None;e=fake_engine(tmp_path/'site');e.vault.data['installation_id']='different'
    with pytest.raises(ValueError,match='another installation'):Maintenance(e).run('restore',backup[2],'password-for-test')
    assert not any('pause' in r.actions or 'data-restore' in r.actions for r in FakeRemote.instances)


def test_failed_staging_migration_never_promotes_database_or_media(backup,tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=('app','data-migrate')
    e=fake_engine(tmp_path/'site');e.vault.data['secrets']=backup[3]['vault']['secrets']
    try:
        with pytest.raises(RuntimeError,match='simulated'):
            Maintenance(e).run('restore',backup[2],'password-for-test')
        assert not any('data-restore' in r.actions or 'maintenance-resume' in r.actions for r in FakeRemote.instances)
        assert any('data-prepare' in r.actions for r in FakeRemote.instances)
    finally:FakeRemote.fail=None


def test_newer_app_backup_rejected_before_stopping_services(backup,tmp_path):
    FakeRemote.instances=[];FakeRemote.fail=None
    e=fake_engine(tmp_path/'site');data,_,_,record=backup
    e.vault.data['secrets']=record['vault']['secrets'];record['version']='v99.0.0'
    (data/'recovery.json').write_text(json.dumps(record))
    plain=tmp_path/'new.zip';encrypted=tmp_path/'new.pcbackup'
    archive.pack(data,plain);archive.encrypt(plain,encrypted,'password-for-test')
    with pytest.raises(ValueError,match='newer App backup'):
        Maintenance(e).run('restore',encrypted,'password-for-test')
    assert not any('pause' in r.actions or 'data-prepare' in r.actions for r in FakeRemote.instances)


def selective_installation(tmp_path):
    e=fake_engine(tmp_path)
    components={r:{'version':'v0.7.1','manifest':'b'*64} for r in e.c['hosts'] if r!='ai'}
    components['db']={'version':'v0.6.6','manifest':'c'*64}
    e.vault.data['secrets']={'FERNET_KEY':'test-secret'}
    e.vault.data['deployment_components']={'schema':1,'installation_id':'test','topology':topology_hash(e.c),'roles':copy.deepcopy(components)}
    (e.dir/'journal.json').write_text(json.dumps({'status':'complete','selective':True,'changed_roles':['app','worker1']}))
    calls=[]
    class SelectiveRemote(FakeRemote):
        def action(self,payload,action,*args):
            calls.append((self.role,action,dict(payload)))
            if action=='preflight':return {'managed':True,**components[self.role]}
            return super().action(payload,action,*args)
    FakeRemote.fail=None;e.factory=SelectiveRemote
    return e,components,calls


def test_export_completed_selective_update_and_retry_failed_version_gate(tmp_path,monkeypatch):
    e,components,calls=selective_installation(tmp_path/'site')
    (e.dir/'maintenance-journal.json').write_text(json.dumps({'status':'failed','mode':'export','operation':'original-failed-export','steps':[]}))
    def download(self,role,folder):
        if role=='db':self.database_metadata=copy.deepcopy(DATABASE_METADATA)
        for name in (['prodcast2.dump'] if role=='db' else ['media.tar.gz','configuration.tar.gz']):
            p=folder/role/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_bytes(b'test-backup')
    monkeypatch.setattr(Maintenance,'download',download)
    path=tmp_path/'selective.pcbackup';result=Maintenance(e).run('export',path,'password-for-test')
    assert result['status']=='complete' and result['archive']==str(path.resolve())
    assert result['bytes']==path.stat().st_size and result['services_recovered'] is True
    record=read_backup(path,'password-for-test',tmp_path)
    assert record['schema']==4 and record['component_versions']==components
    assert record['database']==DATABASE_METADATA
    assert record['version']=='v0.7.1' and record['manifest']=='b'*64
    done=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert done['status']=='complete' and done['operation']=='original-failed-export'
    assert all(p['manifest']==components[r]['manifest'] for r,a,p in calls if a=='maintenance-stop')
    calls.clear();Maintenance(e).run('restore',path,'password-for-test')
    restore=[(r,p) for r,a,p in calls if a in ('data-validate','data-restore')]
    assert len(restore)==6
    assert all(p['restore_target']==components[r] and p['restore_strategy']=='validated-forward-v1' for r,p in restore)


@pytest.mark.parametrize('failure',['unrecorded-release','missing-records','wrong-installation','pending-core'])
def test_unverified_or_pending_selective_installation_stops_before_quiescing(tmp_path,failure):
    e,components,calls=selective_installation(tmp_path)
    if failure=='unrecorded-release':components['db']['manifest']='d'*64
    if failure=='missing-records':e.vault.data.pop('deployment_components')
    if failure=='wrong-installation':e.vault.data['deployment_components']['installation_id']='other'
    if failure=='pending-core':(e.dir/'journal.json').write_text(json.dumps({'status':'failed'}))
    with pytest.raises(ValueError):Maintenance(e).run('export',tmp_path/'blocked.pcbackup','password-for-test')
    assert not any(a in ('pause','drain','maintenance-stop','data-export','data-restore') for r,a,p in calls)


def test_restore_does_not_require_db_deployment_label_to_match(backup,tmp_path):
    e,components,calls=selective_installation(tmp_path/'site')
    data,_,_,record=backup
    record.update(schema=3,media_scope=linux.MEDIA_SCOPE,version=components['app']['version'],manifest=components['app']['manifest'],component_versions=copy.deepcopy(components))
    record['vault']['secrets']=e.vault.data['secrets']
    record['component_versions']['db']['manifest']='d'*64
    (data/'recovery.json').write_text(json.dumps(record))
    plain=tmp_path/'wrong.zip';archive.pack(data,plain)
    encrypted=tmp_path/'wrong.pcbackup';archive.encrypt(plain,encrypted,'password-for-test')
    Maintenance(e).run('restore',encrypted,'password-for-test')
    assert any(r=='db' and a=='data-prepare' for r,a,p in calls)
    assert any(r=='app' and a=='data-migrate' for r,a,p in calls)


def test_older_app_backup_restores_into_newer_app_with_current_worker_credentials(backup,tmp_path):
    e,components,calls=selective_installation(tmp_path/'site')
    e.vault.data['secrets']={**backup[3]['vault']['secrets'],'WORKER_01_PASSWORD':'new-password'}
    Maintenance(e).run('restore',backup[2],'password-for-test')
    assert any(a=='data-migrate' and p['restore_target']==components['app'] for r,a,p in calls)
    assert any(a=='data-restore' and r=='db' for r,a,p in calls)
    assert all(p['restore_target']==components[r] for r,a,p in calls if a=='data-restore')


def fake_download(self,role,folder):
    if role=='db':self.database_metadata=copy.deepcopy(DATABASE_METADATA)
    for name in (['prodcast2.dump'] if role=='db' else ['media.tar.gz','configuration.tar.gz']):
        p=folder/role/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_bytes(b'backup')


@pytest.mark.parametrize('destination',['','directory','existing','missing-parent'])
def test_export_invalid_destination_never_stops_services(tmp_path,destination):
    FakeRemote.instances=[];FakeRemote.fail=None;e=fake_engine(tmp_path/'site')
    path={'': '', 'directory':tmp_path,'existing':tmp_path/'exists.pcbackup','missing-parent':tmp_path/'absent'/'backup.pcbackup'}[destination]
    if destination=='existing':path.write_bytes(b'keep')
    with pytest.raises(ValueError):Maintenance(e).run('export',path,'password-for-test')
    assert not FakeRemote.instances
    if destination=='existing':assert path.read_bytes()==b'keep'


def test_original_export_error_survives_recovery_failure_and_pending_clears_on_retry(tmp_path,monkeypatch):
    from prodcast_manager.operation_history import pending_operations
    FakeRemote.fail=None;e=fake_engine(tmp_path/'site');logs=[];e.log=logs.append
    error=ValueError('original export failure')
    def fail_download(*args):raise error
    def fail_resume(*args):raise RuntimeError('secondary recovery failure')
    monkeypatch.setattr(Maintenance,'download',fail_download)
    monkeypatch.setattr(Maintenance,'resume',fail_resume)
    path=tmp_path/'backup.pcbackup'
    with pytest.raises(ValueError,match='original export failure') as caught:
        Maintenance(e).run('export',path,'password-for-test')
    assert caught.value is error
    assert any('original export failure' in line for line in logs)
    assert any('secondary recovery failure' in line for line in logs)
    journal=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert journal['archive_saved'] is False and journal['services_recovered'] is False
    assert journal['leases_released'] is True
    assert len(pending_operations(e.dir))==1
    monkeypatch.setattr(Maintenance,'download',fake_download)
    monkeypatch.setattr(Maintenance,'resume',lambda *args:None)
    Maintenance(e).run('export',path,'password-for-test')
    done=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert done['operation']==journal['operation'] and done['status']=='complete'
    assert not pending_operations(e.dir)


def test_recovered_failed_export_is_informational_and_other_actions_are_allowed(tmp_path,monkeypatch):
    from unittest.mock import Mock
    from prodcast_manager.operation_history import operation_summary
    FakeRemote.fail=None;e=fake_engine(tmp_path/'site')
    monkeypatch.setattr(Maintenance,'download',lambda *args:(_ for _ in ()).throw(ValueError('export failed')))
    with pytest.raises(ValueError):Maintenance(e).run('export',tmp_path/'failed.pcbackup','password-for-test')
    summary=operation_summary(e.dir)
    assert summary['needs_attention']==1 and summary['blocking']==0
    e._run=Mock(return_value=[])
    e.run('backup');e._run.assert_called_once_with('backup')
    Maintenance(e).run('reset-keep')
    assert json.loads((e.dir/'maintenance-journal.json').read_text())['mode']=='reset-keep'
    assert list((e.dir/'history').glob('maintenance-*.json'))


def test_export_lease_release_failure_stays_blocking(tmp_path,monkeypatch):
    from prodcast_manager.operation_history import operation_summary
    FakeRemote.fail=('app','maintenance-release');e=fake_engine(tmp_path/'site')
    monkeypatch.setattr(Maintenance,'download',lambda *args:(_ for _ in ()).throw(ValueError('export failed')))
    try:
        with pytest.raises(ValueError):Maintenance(e).run('export',tmp_path/'failed.pcbackup','password-for-test')
        assert operation_summary(e.dir)['blocking']==1
        assert json.loads((e.dir/'maintenance-journal.json').read_text())['leases_released'] is False
    finally:FakeRemote.fail=None


def test_recovered_failed_export_can_retry_after_another_component_update(tmp_path,monkeypatch):
    e,components,calls=selective_installation(tmp_path/'site')
    monkeypatch.setattr(Maintenance,'download',lambda *args:(_ for _ in ()).throw(ValueError('export failed')))
    path=tmp_path/'retry.pcbackup'
    with pytest.raises(ValueError):Maintenance(e).run('export',path,'password-for-test')
    components['db']={'version':'v0.6.7','manifest':'d'*64}
    e.vault.data['deployment_components']['roles']['db']=dict(components['db'])
    calls.clear();monkeypatch.setattr(Maintenance,'download',fake_download)
    result=Maintenance(e).run('export',path,'password-for-test')
    assert result['status']=='complete'
    assert any(a=='maintenance-snapshot' for r,a,p in calls)
    record=read_backup(path,'password-for-test',tmp_path)
    assert record['component_versions']['db']==components['db']
    archived=next((e.dir/'history').glob('export-before-component-change-*.json'))
    assert json.loads(archived.read_text())['status']=='failed'


def test_saved_archive_recovery_failure_retries_only_recovery(tmp_path,monkeypatch):
    FakeRemote.fail=None;e=fake_engine(tmp_path/'site')
    monkeypatch.setattr(Maintenance,'download',fake_download)
    def fail_resume(*args):raise RuntimeError('restart timed out')
    monkeypatch.setattr(Maintenance,'resume',fail_resume)
    path=tmp_path/'saved.pcbackup'
    with pytest.raises(RuntimeError,match='restart timed out'):Maintenance(e).run('export',path,'password-for-test')
    journal=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert journal['archive_saved'] is True and journal['services_recovered'] is False
    digest=archive.sha(path)
    def no_download(*args):raise AssertionError('Must not repeat completed export')
    monkeypatch.setattr(Maintenance,'download',no_download)
    monkeypatch.setattr(Maintenance,'resume',lambda *args:None)
    Maintenance(e).run('export',path,'password-for-test')
    assert archive.sha(path)==digest
    done=json.loads((e.dir/'maintenance-journal.json').read_text())
    assert done['status']=='complete' and done['services_recovered'] is True
