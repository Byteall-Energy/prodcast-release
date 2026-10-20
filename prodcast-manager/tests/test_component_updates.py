import copy
import json
from unittest.mock import Mock
import pytest
from test_manager import engine
from prodcast_manager.config import core_roles,topology_hash
from prodcast_manager.component_updates import (fingerprint,receipt,changed_roles,update_actions,ExistingInstallation)
from prodcast_manager.release import sha


@pytest.fixture
def deployed(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False;e.vault.initialize(e.c)
    e.vault.data['installation_id']='test-installation';e.vault.save=Mock()
    e.release=copy.copy(e.release);e.release.doc={};e.release.offline_spec={};e.release.assets={}
    files={}
    for role in core_roles(e.c):
        path=tmp_path/(role+'.zip');path.write_bytes(role.encode());files[role]=path
        e.release.assets[path.name]={'sha256':sha(path)}
    e.release.for_role=lambda role:{'payload':files[role]}
    e.test_files=files;e.keep_ai=True
    versions={role:dict(version=e.release.version,manifest=e.release.digest,operation='') for role in core_roles(e.c)}
    calls=[]
    class Remote:
        fail=None
        def __init__(self,role,*args):self.role=role
        def probe(self):return {'free':40*1024**3}
        def prepare_stage(self):pass
        def put_bytes(self,*args):pass
        def put(self,*args):pass
        def cleanup(self):pass
        def close(self):pass
        def action(self,p,action,resources):
            calls.append((self.role,action,copy.deepcopy(p)))
            if self.fail==(self.role,action):raise RuntimeError('simulated unhealthy service')
            if action=='preflight':return dict(managed=True,**versions[self.role])
            if action=='healthcheck':return {'healthy':True,'services_restarted':False,'account':'EXAMPLE\\approved-worker'}
            if action in ('commit','reconcile'):versions[self.role].update(version=p['version'],manifest=p['manifest'],operation='')
            if action=='claim':versions[self.role]['operation']=p['operation']
            return {'healthy':True}
    e.factory=Remote;e.test_remote=Remote;e.test_calls=calls;e.test_versions=versions
    journal=dict(operation='original',mode='install',manifest=e.release.digest,status='complete',install_ai=False,steps=[])
    (tmp_path/'journal.json').write_text(json.dumps(journal))
    e.vault.data['deployment_components']=receipt(e)
    return e


def change(e,*roles):
    for role in roles:
        path=e.test_files[role];path.write_bytes(path.read_bytes()+b'new')
        e.release.assets[path.name]['sha256']=sha(path)
    e.release.version='v0.4';e.release.digest='target-release';e.release.allowed_from=['v0.3']


def test_app_only_update_never_mutates_worker_or_database(deployed):
    e=deployed;change(e,'app');result=e.run('update')
    assert result['changed_roles']==['app']
    assert all(action=='preflight' or role=='db' and action=='backup' for role,action,p in e.test_calls if role!='app')
    assert any(role=='db' and action=='backup' for role,action,p in e.test_calls)
    assert not any(role.startswith('worker') and action in ('stop','install','claim','commit') for role,action,p in e.test_calls)
    assert e.test_versions['worker1']['version']=='v0.3'


def test_worker_only_update_does_not_restart_app_db_or_other_workers(deployed):
    e=deployed;change(e,'worker1');result=e.run('update')
    assert result['changed_roles']==['worker1']
    app_actions=[a for r,a,p in e.test_calls if r=='app']
    assert set(app_actions)=={'preflight','drain-workers','resume-workers','verify'}
    assert all(a=='preflight' for r,a,p in e.test_calls if r in ('db','worker2'))
    assert not any(a in ('pause','stop','configure','start','bootstrap','migrate','commit') for a in app_actions)


def test_db_only_update_does_not_stop_app_or_workers(deployed):
    e=deployed;change(e,'db');result=e.run('update')
    assert result['changed_roles']==['db']
    assert all(a in ('preflight','verify') for r,a,p in e.test_calls if r!='db')


def test_unchanged_release_has_no_mutating_core_actions(deployed):
    e=deployed;result=e.run('update')
    assert result['changed_roles']==[] and all(a=='preflight' for r,a,p in e.test_calls)
    assert all(v['version']=='v0.3' for v in e.test_versions.values())


def test_missing_baseline_does_not_guess_or_restart_anything(deployed):
    e=deployed;e.vault.data.pop('deployment_components');change(e,'app')
    with pytest.raises(ValueError,match='baseline'):e.run('update')
    assert not e.test_calls


def test_service_credential_failure_is_before_any_stop(deployed):
    e=deployed;change(e,'worker1');e.test_remote.fail=('worker1','service-preflight')
    with pytest.raises(RuntimeError):e.run('update')
    assert not any(a in ('stop','claim','install','backup','drain-workers') for r,a,p in e.test_calls)


def test_failed_worker_update_restores_consumers_without_restarting_app(deployed):
    e=deployed;change(e,'worker1');e.test_remote.fail=('worker1','backup')
    with pytest.raises(RuntimeError):e.run('update')
    assert any(r=='app' and a=='resume-workers' for r,a,p in e.test_calls)
    assert not any(r=='app' and a in ('stop','start','pause') for r,a,p in e.test_calls)
    journal=json.loads((e.dir/'journal.json').read_text());assert journal['status']=='failed'


def test_component_update_resumes_with_same_operation(deployed):
    e=deployed;change(e,'app');e.test_remote.fail=('app','backup')
    with pytest.raises(RuntimeError):e.run('update')
    operation=json.loads((e.dir/'journal.json').read_text())['operation']
    e.test_remote.fail=None;e.test_calls.clear();e.run('update')
    assert all(p['operation']==operation for r,a,p in e.test_calls)


def test_healthcheck_finalizes_metadata_only_after_all_roles_pass(deployed):
    e=deployed;e.vault.data.pop('deployment_components')
    old=json.loads((e.dir/'journal.json').read_text());old['status']='failed';old['inflight']={'role':'worker1','action':'install'}
    (e.dir/'journal.json').write_text(json.dumps(old))
    result=e.run('healthcheck')
    assert result['healthy'] and not result['services_restarted']
    actions=[a for r,a,p in e.test_calls]
    assert set(actions)=={'preflight','healthcheck','reconcile'}
    assert actions.index('reconcile')>max(i for i,a in enumerate(actions) if a=='healthcheck')
    assert json.loads((e.dir/'journal.json').read_text())['status']=='complete'
    assert json.loads((e.dir/'history/core-before-healthcheck-original.json').read_text())==old
    assert e.vault.data['deployment_components']['installation_id']=='test-installation'


def test_healthcheck_failure_does_not_clear_or_forge_completion(deployed):
    e=deployed;e.test_remote.fail=('worker2','healthcheck')
    old=json.loads((e.dir/'journal.json').read_text());old['status']='failed';(e.dir/'journal.json').write_text(json.dumps(old))
    with pytest.raises(RuntimeError):e.run('healthcheck')
    assert json.loads((e.dir/'journal.json').read_text())==old
    assert not any(a=='reconcile' for r,a,p in e.test_calls)


def test_healthcheck_rejects_foreign_owner_and_wrong_release(deployed):
    e=deployed;e.test_versions['worker1']['operation']='another-installation'
    with pytest.raises(ValueError,match='ownership'):e.run('healthcheck')
    assert not any(a=='reconcile' for r,a,p in e.test_calls)
    e.test_calls.clear();e.release.digest='newer-not-installed'
    with pytest.raises(ValueError,match='exact release'):e.run('healthcheck')
    assert not e.test_calls


def test_signatures_are_platform_selection_independent(deployed):
    e=deployed;path=e.dir/'dependencies.zip';path.write_bytes(b'deps');e.release.assets[path.name]={'sha256':sha(path)}
    e.release.offline_spec={'linux_packages':{'debian:12':path.name,'rhel:9':path.name}}
    original=e.release.for_role
    e.release.for_role=lambda role:{**original(role),'offline-linux-debian-12':path,'offline-linux-rhel-9':path}
    before=fingerprint(e,'app')
    e.release.for_role=lambda role:{**original(role),'offline-linux-debian-12':path}
    assert fingerprint(e,'app')==before


def test_worker_service_settings_are_isolated_to_worker_payload(deployed):
    e=deployed;e.vault.data['worker_services']={'worker1':dict(mode='custom',account='DOMAIN\\svc',password='test-private-password')}
    assert e.payload('worker1','op','check')['worker_service']['password']=='test-private-password'
    assert 'worker_service' not in e.payload('worker2','op','check')
    assert 'worker_service' not in e.payload('app','op','check')


def test_failed_account_preflight_never_controls_consumers(deployed):
    e=deployed;change(e,'worker1');e.test_remote.fail=('worker1','service-preflight')
    with pytest.raises(RuntimeError):e.run('update')
    assert not any(a in ('resume-workers','drain-workers','stop','claim') for r,a,p in e.test_calls)


def test_healthcheck_can_finalize_completed_target_of_failed_scoped_update(deployed):
    e=deployed;change(e,'worker1');e.test_remote.fail=('worker1','commit')
    with pytest.raises(RuntimeError):e.run('update')
    e.test_remote.fail=None;e.test_calls.clear()
    assert e.run('healthcheck')['healthy']
    expected=e.vault.data['deployment_components']['roles']
    assert expected['worker1']['manifest']=='target-release'
    assert expected['app']['manifest']!='target-release'
    assert set(a for r,a,p in e.test_calls)=={'preflight','healthcheck','reconcile'}
