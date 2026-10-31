import copy
import importlib.util
import json
import os
import stat
import sys
import zipfile
from functools import lru_cache
from pathlib import Path
from unittest.mock import Mock
import pytest
from cryptography.fernet import InvalidToken
from cryptography import x509
from cryptography.x509.oid import ExtensionOID

sys.path.insert(0,str(Path(__file__).parents[1]))
from prodcast_manager.config import example, validate, topology_hash, ROLES
from prodcast_manager.vault import Vault, make_pki
from prodcast_manager.release import safe_zip, Release, PIN_V02, sha
from prodcast_manager.ssh import PinnedPolicy, fingerprint, ps
from prodcast_manager.engine import Engine, plan, RESOURCES

@pytest.mark.parametrize('change',[
    lambda c:c['hosts']['app'].update(address='127.0.0.1'),
    lambda c:c['hosts']['db'].update(address=c['hosts']['app']['address']),
    lambda c:c.update(public_url='http://insecure.test'),
    lambda c:c.update(public_url='https://user:password@example.com'),
    lambda c:c.update(public_url='https://example.com/path'),
    lambda c:c['hosts']['app'].update(username='root; rm -rf /'),
    lambda c:c['hosts']['ai'].update(password='plaintext'),
    lambda c:c.update(app_subnet='192.0.2.0/24'),
    lambda c:c.update(site_id='../escape'),
    lambda c:c.update(license_expires_at='2027-01-01T00:00:00'),
])
def test_config_rejects_bad_inputs(change):
    c=example();change(c)
    with pytest.raises((ValueError,KeyError)):validate(c)

def test_trust_required_and_auth_not_part_of_topology():
    c=example();before=topology_hash(c)
    with pytest.raises(ValueError):validate(c,True)
    c['hosts']['app']['username']='another';c['hosts']['app']['key_path']='C:/a path/key'
    assert topology_hash(c)==before
    c['admin_ip']='192.0.2.226';assert topology_hash(c)!=before

def test_vault_encrypts_and_preserves_secrets(tmp_path):
    p=tmp_path/'vault.json';v=Vault(p,'sufficient master passphrase')
    v.data['ssh']={'app':{'password':'not-in-plain-text'}};v.initialize(example())
    saved=copy.deepcopy(v.data)
    assert 'not-in-plain-text' not in p.read_text()
    assert v.data['secrets']['PW_APP'] not in p.read_text()
    loaded=Vault(p,'sufficient master passphrase');loaded.initialize(example())
    assert loaded.data==saved
    with pytest.raises(InvalidToken):Vault(p,'incorrect master passphrase')
    c=example();c['hosts']['app']['address']='192.0.2.25'
    with pytest.raises(ValueError):loaded.initialize(c)

def test_pki_has_ip_sans_and_ca_not_distributed():
    c=example();tls=make_pki(c)
    cert=x509.load_pem_x509_certificate(tls['postgres.crt'].encode())
    san=cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert c['hosts']['db']['address'] in [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    assert cert.issuer==x509.load_pem_x509_certificate(tls['ca.crt'].encode()).subject
    ca=x509.load_pem_x509_certificate(tls['ca.crt'].encode())
    ski=ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    for name,pem in tls.items():
        if not name.endswith('.crt'):continue
        leaf=x509.load_pem_x509_certificate(pem.encode())
        assert leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier==ski
        assert leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest

def test_stale_vault_cannot_overwrite_new_secrets(tmp_path):
    path=tmp_path/'vault.json'
    a=Vault(path,'long enough password');b=Vault(path,'long enough password')
    a.data={'important':'preserve'};a.save()
    b.data={'important':'lost'}
    with pytest.raises(RuntimeError,match='another process'):b.save()
    assert Vault(path,'long enough password').data['important']=='preserve'

@pytest.mark.parametrize('name',['../escape','/absolute','C:/escape','folder\\escape'])
def test_zip_traversal(tmp_path,name):
    z=tmp_path/'a.zip'
    with zipfile.ZipFile(z,'w') as f:f.writestr(name,'bad')
    # ZipInfo normalizes backslashes when constructing an archive on Windows.
    if '\\' in name:z.write_bytes(z.read_bytes().replace(name.replace('\\','/').encode(),name.encode()))
    with pytest.raises(ValueError):safe_zip(z,tmp_path/'out')

def test_zip_symlink_and_duplicate(tmp_path):
    z=tmp_path/'a.zip'
    with zipfile.ZipFile(z,'w') as f:
        i=zipfile.ZipInfo('link');i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16;f.writestr(i,'../escape')
    with pytest.raises(ValueError):safe_zip(z,tmp_path/'out')
    with zipfile.ZipFile(z,'w') as f:f.writestr('A','a');f.writestr('a','b')
    with pytest.raises(ValueError):safe_zip(z,tmp_path/'out')

def test_host_key_must_match():
    key=Mock();key.asbytes.return_value=b'public server key'
    PinnedPolicy(fingerprint(key)).missing_host_key(None,'host',key)
    with pytest.raises(Exception):PinnedPolicy('SHA256:wrong').missing_host_key(None,'host',key)

def test_powershell_command_encodes_content():
    import base64
    command="Write-Output 'path with space'"
    assert base64.b64decode(ps(command).split()[-1]).decode('utf-16-le').endswith(command)
    assert '-OutputFormat Text' in ps(command)

@lru_cache
def fake_vault_pki():return make_pki(example())

class FakeVault:
    def __init__(self):self.data={'secrets':{},'tls':dict(fake_vault_pki()),'ssh':{}}
    def initialize(self,c):self.data['topology']=topology_hash(c)

class FakeRelease:
    version='v0.3';digest='verified';allowed_from=['v0.2'];images={};assets={}
    def for_role(self,r):return {}

class FakeRemote:
    history=[];fail=None;installed=True
    def __init__(self,r,h,a,log):self.role=r
    def probe(self):return {'free':40*1024**3}
    def prepare_stage(self):pass
    def put_bytes(self,*a):pass
    def put(self,*a):pass
    def cleanup(self):pass
    def close(self):pass
    def action(self,p,action,resources):
        self.history.append((self.role,action,p['operation']))
        if self.fail==(self.role,action):raise RuntimeError('simulated failure')
        if action=='preflight':return {'managed':self.installed,'version':'v0.2' if self.installed else '', 'operation':p['operation'] if p['mode']=='stop-operation' else '', 'manifest':'previous-manifest' if self.installed else ''}
        return {'ok':True}

def engine(tmp_path):
    c=example()
    c['public_url']='https://192.168.31.23';c['admin_ip']='192.168.31.225'
    for h in c['hosts'].values():h['address']=h['address'].replace('192.0.2.','192.168.31.')
    for h in c['hosts'].values():h['fingerprint']='SHA256:'+'a'*43
    FakeRemote.history=[];FakeRemote.fail=None;FakeRemote.installed=True
    return Engine(c,FakeVault(),FakeRelease(),tmp_path,lambda x:None,FakeRemote)

@pytest.mark.parametrize('field',['app','db','worker1','worker2','admin_ip','public_url'])
def test_live_configuration_rejects_template_addresses(tmp_path,field):
    c=engine(tmp_path).c
    if field in c['hosts']:c['hosts'][field]['address']='192.0.2.55'
    elif field=='public_url':c[field]='https://192.0.2.23'
    else:c[field]='192.0.2.225'
    with pytest.raises(ValueError,match='демонстрационный адрес'):validate(c,True)

def test_update_backup_failure_blocks_migrations_and_preserves_resume_id(tmp_path):
    e=engine(tmp_path);FakeRemote.fail=('db','backup')
    with pytest.raises(RuntimeError):e.run('update')
    assert not any(a=='migrate' for _,a,_ in FakeRemote.history)
    journal=json.loads((tmp_path/'journal.json').read_text());assert journal['status']=='failed'
    operation=journal['operation'];FakeRemote.fail=None;e.remotes={};e.run('update')
    assert all(op==operation for _,_,op in FakeRemote.history)
    assert json.loads((tmp_path/'journal.json').read_text())['status']=='complete'

def test_failed_operation_allows_corrected_release_and_archives_old_journal(tmp_path,monkeypatch):
    e=engine(tmp_path)
    old={'operation':'failed-operation','mode':'update','manifest':'bad-release-digest',
         'status':'failed','phase':'applying','steps':[{'role':'worker1','action':'install'}]}
    (tmp_path/'journal.json').write_text(json.dumps(old),encoding='utf-8')
    payloads=[];original=FakeRemote.action
    def capture(self,payload,action,resources):
        payloads.append((action,payload['operation'],payload.get('previous_operation')))
        return original(self,payload,action,resources)
    monkeypatch.setattr(FakeRemote,'action',capture)
    e.run('update')
    current=json.loads((tmp_path/'journal.json').read_text('utf-8'))
    assert current['status']=='complete' and current['operation']!='failed-operation'
    assert json.loads((tmp_path/'history/journal-failed-operation.json').read_text('utf-8'))==old
    assert any(action=='preflight' and previous=='failed-operation' for action,_,previous in payloads)

def test_stop_previous_operation_releases_all_core_roles_and_preserves_history(tmp_path):
    e=engine(tmp_path)
    old={'operation':'stale-operation','mode':'update','manifest':'bad-release-digest',
         'status':'failed','phase':'applying','steps':[{'role':'db','action':'backup'}]}
    (tmp_path/'journal.json').write_text(json.dumps(old),encoding='utf-8')
    result=e.stop_previous_operation()
    current=json.loads((tmp_path/'journal.json').read_text('utf-8'))
    assert result['status']=='stopped' and current['status']=='stopped'
    assert json.loads((tmp_path/'history/journal-stale-operation.json').read_text('utf-8'))==old
    assert {(role,action) for role,action,_ in FakeRemote.history}=={
        pair for role in ROLES if role!='ai' for pair in ((role,'preflight'),(role,'release-operation'))}

def test_all_hosts_checked_before_claim_and_no_mutation_after_preflight_failure(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False;FakeRemote.fail=('worker2','preflight')
    with pytest.raises(RuntimeError):e.run('install')
    assert not any(a=='claim' for _,a,_ in FakeRemote.history)

def test_upgrade_requires_managed_hosts(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False
    with pytest.raises(RuntimeError,match='установка Manager не найдена'):e.run('update')
    assert not any(a=='claim' for _,a,_ in FakeRemote.history)

def test_role_secret_isolation(tmp_path):
    e=engine(tmp_path);e.vault.data['secrets']={'PG_ADMIN':'db-admin','MEDIA_KEY':'shared','PW_W01':'worker-password','SVC_W01':'svc'}
    p=e.payload('worker1','id','install')
    assert 'PG_ADMIN' not in p['secrets'];assert p['secrets']['PW_W01']=='worker-password'
    assert set(p['tls'])=={'ca.crt'}
    assert 'ca.key' not in e.payload('db','id','install')['tls']

def test_install_order_and_update_stop_order():
    p=plan('install')
    assert p.index(('db','grants'))>p.index(('app','migrate'))
    assert p.index(('app','beat'))>p.index(('worker2','install'))
    u=plan('update')
    assert u.index(('app','drain'))<u.index(('worker1','stop'))<u.index(('db','backup'))<u.index(('app','migrate'))

def test_remote_template_substitution_and_sql_grants():
    import string
    t=string.Template((RESOURCES/'app.env.in').read_text())
    values={name:'safe' for _,name,braced,_ in t.pattern.findall(t.template) for name in (name or braced,) if name}
    output=t.substitute(values)
    assert 'ALLOW_INSECURE_TLS_VERIFY=true' in output
    assert 'REQUIRE_TLS_FOR_REMOTE_SERVICES=true' in output
    assert 'LICENSE_SERVICE_FAIL_OPEN=false' in output
    sql=(RESOURCES/'worker-grants.sql').read_text()
    assert 'SELECT (id,username) ON auth_user' in sql and 'ALL ON ALL' not in sql

def test_v02_assets_if_available():
    path=Path(__file__).parents[2]/'deliverables/releases/v0.2'
    if not path.exists():pytest.skip('v0.2 external fixture not supplied')
    r=Release(path,path)
    assert r.digest==PIN_V02 and len(r.for_role('app'))==6 and len(r.for_role('worker1'))==1

def test_unknown_manifest_refused(tmp_path):
    (tmp_path/'release-manifest.json').write_text(json.dumps({'version':'v0.3'}))
    with pytest.raises(ValueError,match='Unknown release'):Release(tmp_path,tmp_path)

def test_release_contract_rejects_incompatible_runtime(tmp_path):
    p=tmp_path/'release-manifest.json';p.write_text(json.dumps({'version':'v0.3','management':{'schema':1,'profile':'prodcast-five-vm-v1','minimum_manager':'0.1.0','python':'3.15','database_major':18}}))
    with pytest.raises(ValueError,match='newer manager'):Release(tmp_path,tmp_path,sha(p))

def test_linux_agent_compiles():compile((RESOURCES/'linux.py').read_text(),'linux.py','exec')

def failed_update_journal(tmp_path,**extra):
    doc={'operation':'original-operation','mode':'update','manifest':'verified','status':'failed','steps':[],**extra}
    (tmp_path/'journal.json').write_text(json.dumps(doc),'utf-8')
    return doc

def test_legacy_empty_failed_update_can_switch_to_install_only_after_all_hosts_checked(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False
    old=failed_update_journal(tmp_path)
    e.run('install')
    assert json.loads((tmp_path/'history/journal-original-operation.json').read_text())==old
    assert [(r,a) for r,a,_ in FakeRemote.history[:4]]==[(r,'preflight') for r in ROLES if r!='ai']
    assert all(op=='original-operation' for _,_,op in FakeRemote.history[:4])
    assert all(op!='original-operation' for _,_,op in FakeRemote.history[4:])
    new=json.loads((tmp_path/'journal.json').read_text())
    assert new['mode']=='install' and new['status']=='complete' and 'inflight' not in new

def test_switch_rejected_if_one_host_has_state_even_without_completed_steps(tmp_path):
    e=engine(tmp_path);old=failed_update_journal(tmp_path)
    original=FakeRemote.action
    def partially_managed(self,p,action,resources):
        result=original(self,p,action,resources)
        if action=='preflight':return {'managed':self.role=='worker2','version':''}
        return result
    from unittest.mock import patch
    with patch.object(FakeRemote,'action',partially_managed):
        with pytest.raises(RuntimeError,match='Смена режима небезопасна'):e.run('install')
    assert json.loads((tmp_path/'journal.json').read_text())==old
    assert len(FakeRemote.history)==4 and not (tmp_path/'history').exists()

@pytest.mark.parametrize('extra',[{'phase':'applying'},{'inflight':{'role':'app','action':'claim'}},{'steps':[{'role':'app','action':'claim'}]}])
def test_switch_refused_if_a_mutation_may_have_started(tmp_path,extra):
    e=engine(tmp_path);old=failed_update_journal(tmp_path,**extra)
    with pytest.raises(ValueError,match='начатыми серверными шагами'):e.run('install')
    assert FakeRemote.history==[]
    assert json.loads((tmp_path/'journal.json').read_text())==old

def test_failed_probe_during_mode_switch_keeps_original_journal(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False;FakeRemote.fail=('worker2','preflight')
    old=failed_update_journal(tmp_path)
    with pytest.raises(RuntimeError):e.run('install')
    assert json.loads((tmp_path/'journal.json').read_text())==old
    assert not any(a=='claim' for _,a,_ in FakeRemote.history)


def test_rejected_install_then_update_on_completed_stack(tmp_path):
    e=engine(tmp_path)
    for _ in range(2):
        with pytest.raises(RuntimeError,match='Выберите «Обновить»'):e.run('install')
        assert not any(a=='claim' for _,a,_ in FakeRemote.history)
    old=json.loads((tmp_path/'journal.json').read_text())
    assert old['phase']=='preflight' and old['steps']==[]
    FakeRemote.history.clear()
    e.run('update')
    assert json.loads((tmp_path/'history'/('journal-'+old['operation']+'.json')).read_text())==old
    assert [(r,a) for r,a,_ in FakeRemote.history[:4]]==[(r,'preflight') for r in ROLES if r!='ai']
    assert all(op==old['operation'] for _,_,op in FakeRemote.history[:4])
    assert all(op!=old['operation'] for _,_,op in FakeRemote.history[4:])
    new=json.loads((tmp_path/'journal.json').read_text())
    assert new['status']=='complete' and new['mode']=='update'


@pytest.mark.parametrize('change',[
    {'managed':False}, {'version':''}, {'version':'v0.1'}, {'version':'v0.3'},
    {'operation':'original-operation'}, {'operation':None},
    {'manifest':None}, {'manifest':'different-manifest'},
])
def test_switch_to_update_requires_all_hosts_committed_consistently(tmp_path,change):
    e=engine(tmp_path)
    old=failed_update_journal(tmp_path,mode='install',phase='preflight')
    original=FakeRemote.action
    def changed(self,p,action,resources):
        result=original(self,p,action,resources)
        if self.role=='worker2' and action=='preflight':result.update(change)
        return result
    from unittest.mock import patch
    with patch.object(FakeRemote,'action',changed):
        try:
            e.run('update')
        except RuntimeError as error:
            assert 'Смена режима небезопасна' not in str(error)
    assert (tmp_path/'history/journal-original-operation.json').exists()


def test_legacy_unknown_phase_cannot_switch_on_installed_hosts(tmp_path):
    e=engine(tmp_path);old=failed_update_journal(tmp_path,mode='install')
    with pytest.raises(RuntimeError,match='Смена режима небезопасна'):e.run('update')
    assert json.loads((tmp_path/'journal.json').read_text())==old


def test_failed_update_preflight_can_change_release_on_committed_stack(tmp_path):
    e=engine(tmp_path)
    old=failed_update_journal(tmp_path,phase='preflight',manifest='another-release')
    e.run('update')
    assert json.loads((tmp_path/'history/journal-original-operation.json').read_text())==old
    assert json.loads((tmp_path/'journal.json').read_text())['status']=='complete'

def test_failed_claim_is_recorded_before_remote_mutation(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False;FakeRemote.fail=('app','claim')
    with pytest.raises(RuntimeError):e.run('install')
    journal=json.loads((tmp_path/'journal.json').read_text())
    assert journal['phase']=='applying' and journal['inflight']=={'role':'app','action':'claim'}
    assert journal['steps']==[] and journal['status']=='failed'

def test_status_of_fresh_hosts_is_read_only_and_does_not_erase_pending_operation(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False;old=failed_update_journal(tmp_path)
    results=e.run('status')
    assert len(results)==5 and all(x['result']['status']=='not_installed_by_manager' for x in results)
    assert all(a=='preflight' for _,a,_ in FakeRemote.history)
    assert json.loads((tmp_path/'journal.json').read_text())==old

def test_wrong_update_then_install_without_deleting_journal(tmp_path):
    e=engine(tmp_path);FakeRemote.installed=False
    with pytest.raises(RuntimeError,match='выберите «Установить»'):e.run('update')
    old=json.loads((tmp_path/'journal.json').read_text())
    assert old['phase']=='preflight' and old['steps']==[]
    e.remotes={};e.run('install')
    assert json.loads((tmp_path/'journal.json').read_text())['status']=='complete'
