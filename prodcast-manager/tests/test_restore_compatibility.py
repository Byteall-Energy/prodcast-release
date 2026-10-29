import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from prodcast_manager.backup_archive import release_order, validate_database_metadata
from prodcast_manager.engine import RESOURCES


def load(name):
    spec=importlib.util.spec_from_file_location(name,RESOURCES/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def restore(tmp_path,monkeypatch):
    r=load('restore_compatibility')
    monkeypatch.setitem(sys.modules,'maintenance_linux',load('maintenance_linux'))
    target={'version':'v0.7.1','manifest':'b'*64}
    m=SimpleNamespace(ROOT=tmp_path,STATE=tmp_path/'state',P={
        'operation':'a'*32,'restore_target':target,'restore_target_app_manifest':'b'*64,
        'restore_file':{'sha256':'c'*64,'name':'media.tar.gz'},'stage':str(tmp_path/'upload'),
        'restore_database_major':18,'media_scope':'application-data-without-models-v1'},
        ST={**target,'maintenance':'a'*32},C={'hosts':{'app':{'address':'192.0.2.10'}}})
    def write(path,text,*args):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    m.write=write
    return r,m


@pytest.mark.parametrize('bad',['maintenance','manifest','operation'])
def test_restore_requires_owned_target(restore,bad):
    r,m=restore
    if bad=='operation':m.P['operation']='unsafe; sql'
    else:m.ST[bad]='wrong'
    with pytest.raises(RuntimeError):r.context(m)


def test_major_and_extension_mismatch_rejected(restore):
    r,m=restore
    m.psql=lambda sql,*a:'170000' if 'server_version' in sql else '{}'
    with pytest.raises(RuntimeError,match='major'):r.check_database(m)
    m.psql=lambda sql,*a:'180000' if 'server_version' in sql else '{"vector":"0.8.0"}'
    m.P['restore_database']={'extensions':{'vector':'0.8.6'}}
    with pytest.raises(RuntimeError,match='extension'):r.check_database(m)


def test_hba_rule_is_narrow_owned_and_reversible(restore):
    r,m=restore;queries=[];m.psql=lambda sql:queries.append(sql)
    path=m.ROOT/'db/config/pg_hba.conf';path.parent.mkdir(parents=True)
    original='local all postgres peer\nhost all all 0.0.0.0/0 reject\n'
    path.write_text(original);r.access_rule(m,True)
    assert 'hostssl prodcast_restore_'+'a'*24+' prodcast_app 192.0.2.10/32 scram-sha-256' in path.read_text()
    r.access_rule(m,True);assert len(queries)==1
    path.write_text(path.read_text()+'# unrelated change\n')
    r.access_rule(m,False);assert path.read_text()==original+'# unrelated change\n'


def test_switch_requires_app_proof_and_retries_after_lost_reply(restore,monkeypatch):
    r,m=restore;stage,previous=r.database_names(m)
    record=r.initial_receipt(m);record.update(phase='prepared',original_oid=1,stage_oid=2)
    r.save_receipt(m,'db',record)
    oids={'prodcast2':1,stage:2};calls=[]
    monkeypatch.setattr(r,'database_oids',lambda m:oids)
    monkeypatch.setattr(r,'access_rule',lambda *args:None)
    monkeypatch.setattr(r,'restore_grants',lambda m:calls.append('grants'))
    m.psql=lambda sql:calls.append(sql)
    with pytest.raises(RuntimeError,match='validated'):r.promote_database(m)
    assert calls==[]
    m.P['restore_migration_result']={'database':stage,'migrated':True}
    r.promote_database(m)
    assert any(sql.startswith('BEGIN; ALTER DATABASE') for sql in calls)
    oids.clear();oids.update(prodcast2=2,**{previous:1});calls.clear()
    m.P.pop('restore_migration_result')
    assert r.promote_database(m)['previous_database']==previous
    assert calls==['grants']


def test_receipt_cannot_reuse_different_source(restore):
    r,m=restore;r.save_receipt(m,'db',r.initial_receipt(m))
    m.P['restore_file']['sha256']='d'*64
    with pytest.raises(RuntimeError,match='another source'):r.load_receipt(m,'db')


def test_media_stages_without_touching_live_then_preserves_models(restore,monkeypatch):
    r,m=restore;volume=m.ROOT/'volume';volume.mkdir()
    (volume/'workflows').mkdir();(volume/'workflows/old').write_text('old')
    for name in ('models_files','official_models'):
        (volume/name).mkdir();(volume/name/'keep').write_text('model')
    upload=Path(m.P['stage']);upload.mkdir()
    with tarfile.open(upload/'media.tar.gz','w:gz') as tar:
        member=tarfile.TarInfo('workflows/new');member.size=3
        tar.addfile(member,io.BytesIO(b'new'))
    m.run=lambda args:json.dumps([{'Mountpoint':str(volume)}])
    monkeypatch.setattr(r,'os_chmod_chown',lambda *args:None)
    r.prepare_media(m);assert (volume/'workflows/old').read_text()=='old'
    result=r.promote_media(m);r.promote_media(m)
    assert (volume/'workflows/new').read_text()=='new'
    assert (Path(result['previous_media'])/'workflows/old').read_text()=='old'
    assert all((volume/name/'keep').read_text()=='model' for name in ('models_files','official_models'))


def test_migration_reply_and_database_identity_are_checked(restore):
    r,m=restore;name=r.database_names(m)[0];m.P['restore_staging_database']=name
    m.app_python=lambda *args,**kwargs:'PRODCAST_RESTORE_RESULT:'+json.dumps({'database':name,'migrated':True})
    assert r.migrate_database(m)['migrated']
    m.app_python=lambda *args,**kwargs:'PRODCAST_RESTORE_RESULT:{"migrated":true}'
    with pytest.raises(RuntimeError,match='confirm'):r.migrate_database(m)
    m.P['restore_staging_database']='prodcast2'
    with pytest.raises(RuntimeError,match='Unexpected'):r.migrate_database(m)


def test_release_order_and_metadata_validation():
    assert release_order('v0.6.6')<release_order('v0.7.1-rc.2')<release_order('v0.7.1')
    with pytest.raises(ValueError):release_order(123)
    with pytest.raises(ValueError):validate_database_metadata({'major':True})
