import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from prodcast_manager.config import example, atomic_json, topology_hash
from prodcast_manager.desktop_controller import DesktopController
from prodcast_manager.portable import save_config, open_credentials, remember, profile_directory


def configured():
    value = example(); value['install_ai'] = False
    value['public_url'] = 'https://prodcast.example.test'; value['admin_ip'] = '10.20.30.100'
    for n,host in enumerate(value['hosts'].values(),10):
        host.update(address='10.20.30.'+str(n),auth='password',fingerprint='SHA256:'+'a'*43)
    return value


def wait(controller):
    deadline = time.monotonic() + 5
    while controller.busy and time.monotonic() < deadline: time.sleep(.01)
    assert not controller.busy
    return controller.last_result


@pytest.fixture
def controller(tmp_path):
    return DesktopController(tmp_path)


def test_empty_hints_and_public_state_do_not_contain_passwords(controller):
    assert all(h['address']=='' for h in controller.snapshot()['config']['hosts'].values())
    controller.dispatch('save_access',{'role':'app','secrets':{'password':'sensitive-synthetic-password'}})
    state = controller.snapshot()
    assert state['credentials']['app']['password'] is True
    assert 'sensitive-synthetic-password' not in json.dumps(state)
    controller.save_draft()
    assert 'sensitive-synthetic-password' not in (controller.directory/'ui-draft.json').read_text('utf-8')


def test_public_config_cannot_accept_credentials_or_unknown_fields(controller):
    value = copy.deepcopy(controller.config); value['hosts']['app']['password']='leak'
    with pytest.raises(ValueError,match='Secrets'): controller.update({'config':value})
    with pytest.raises(ValueError,match='Unsupported'): controller.dispatch('__getattribute__',{})


def test_incomplete_draft_roundtrip_and_saved_access_follow_initial_profile(controller):
    controller.save_access({'role':'app','secrets':{'password':'keep-me'}})
    controller.save_draft()
    reopened = DesktopController(controller.home)
    assert reopened.config['hosts']['app']['address']==''
    reopened.update({'config':configured()}); reopened.save_profile()
    assert reopened._read_vault()['ssh']['app']['password']=='keep-me'
    assert runtime_site(reopened)['hosts']['app']['address']=='10.20.30.10'


def runtime_site(controller):
    from prodcast_manager.portable import runtime_config
    return runtime_config(controller.directory/'site.json')


def test_clearing_search_account_removes_stored_password(controller):
    value=dict(enabled=True,url='ldaps://dc.example.com',domain='example.com',
               base_dn='DC=example,DC=com',group_dn='CN=Users,DC=example,DC=com',
               bind_dn='svc@example.com',bind_password='old-search-secret')
    controller.directory_save(value)
    value.pop('bind_password');value['bind_dn']=''
    controller.directory_save(value)
    saved=controller._read_vault()['directory']
    assert saved['bind_dn']==saved['bind_password']==''
    assert saved['enabled']


def test_installed_core_and_worker_membership_cannot_be_saved_as_regular_edits(controller):
    controller.update({'config':configured()}); controller.save_profile()
    vault = controller._vault(); vault.data['topology']=topology_hash(controller.config); vault.save()
    controller.config['hosts']['app']['address']='10.20.30.200'
    with pytest.raises(ValueError,match='identity'): controller.save_profile()
    controller.config=runtime_site(controller); controller.add_worker_draft()
    controller.config['hosts']['worker3']['address']='10.20.30.16'
    with pytest.raises(ValueError,match='membership'): controller.save_profile()


def test_worker_ids_remain_stable_when_adding_into_a_gap(controller):
    controller.config=configured()
    controller.config['hosts']['worker3']=controller.config['hosts'].pop('worker2')
    assert controller.add_worker_draft()['role']=='worker2'
    assert set(controller.config['hosts'])=={'app','db','ai','worker1','worker2','worker3'}


def test_fingerprint_trust_is_explicit_and_bound_to_endpoint(controller,monkeypatch):
    controller.config=configured()
    controller.config['hosts']['app']['fingerprint']=''
    monkeypatch.setattr('prodcast_manager.ssh.scan',lambda *a,**k:'SHA256:'+'b'*43)
    controller.scan_server({'role':'app'}); result=wait(controller)
    assert result['ok'] and controller.config['hosts']['app']['fingerprint']==''
    controller.config['hosts']['app']['address']='10.20.30.99'
    with pytest.raises(ValueError,match='Endpoint'): controller.trust_server({'role':'app','fingerprint':'SHA256:'+'b'*43})
    controller.config['hosts']['app']['address']='10.20.30.10'
    controller.trust_server({'role':'app','fingerprint':'SHA256:'+'b'*43})
    assert controller.config['hosts']['app']['fingerprint']=='SHA256:'+'b'*43


def test_core_update_keeps_ai_packages_omitted_and_logs_are_redacted(controller,monkeypatch):
    controller.update({'config':configured(),'packages':{'release':'release.zip','trusted':'hash','ai_model':'models.zip','ai_ollama':'runtime.zip','ai_action':'keep'}})
    controller.save_access({'role':'app','secrets':{'password':'sensitive-password'}})
    release = Mock(return_value=SimpleNamespace(version='v0.6.7',digest='digest'))
    monkeypatch.setattr('prodcast_manager.desktop_controller.Release',release)
    def engine(config,vault,package,directory,emit,**kwargs):
        def run(mode):
            emit('app: migrate sensitive-password')
            atomic_json(directory/'journal.json',dict(status='complete',mode=mode,operation='operation'))
        return SimpleNamespace(run=run)
    monkeypatch.setattr('prodcast_manager.desktop_controller.Engine',engine)
    controller.run({'mode':'update'}); result=wait(controller)
    assert result['ok']
    assert release.call_args.kwargs['model_path']==release.call_args.kwargs['ollama_path']==''
    record=controller.history.list(controller.directory)[0]
    logs=controller.logs({'id':record['id'],'target':'app'})
    assert 'migrate' in logs['text'] and 'sensitive-password' not in logs['text']
    assert controller.snapshot()['applied']=='match'


def test_read_only_administrator_access_does_not_write_or_require_idle(controller):
    controller.update({'config':configured()});controller.save_profile()
    vault=controller._vault();vault.data['secrets']={'ADMIN_PASSWORD':'synthetic-password'};vault.save()
    before={p.name:p.read_bytes() for p in controller.directory.iterdir() if p.is_file()}
    controller.busy=True
    assert controller.administrator()['password']=='synthetic-password'
    assert before=={p.name:p.read_bytes() for p in controller.directory.iterdir() if p.is_file()}
    with pytest.raises(ValueError,match='running'): controller.update({'config':configured()})
    controller.busy=False


def test_pending_journals_and_unreadable_entries_are_not_cleared_by_view(controller):
    controller.directory.mkdir(parents=True)
    atomic_json(controller.directory/'worker-action-journal.json',{'status':'failed','role':'worker2'})
    (controller.directory/'ai-journal.json').write_text('{broken')
    assert len(controller.snapshot()['pending'])==2
    assert (controller.directory/'ai-journal.json').read_text()=='{broken'


def test_operation_failure_and_unknown_action_do_not_appear_successful(controller):
    def fail(): raise RuntimeError('worker2: expected failure')
    identity=controller._job('worker-repair',fail)['operation']; result=wait(controller)
    assert not result['ok'] and controller.history.list()[0]['status']=='failed'
    assert 'expected failure' in controller.logs({'id':identity,'target':'worker2'})['text']


def test_issued_activation_identity_is_not_reported_as_software_installed(controller):
    controller.update({'config':configured()})
    identity=controller.initialize_identity()['installation_id']
    assert '-' in identity
    state=controller.snapshot()
    assert state['identity_bound'] and not state['installed']


def test_switching_site_does_not_carry_another_sites_release(controller):
    controller.update({'config':configured()});controller.save_profile();first=controller.directory.name
    controller.new_site();second=configured();second['site_id']='second';controller.update({'config':second});controller.save_profile()
    controller.packages['release']='another-sites-release.zip'
    controller.select_site({'id':first})
    assert controller.packages['release']==''


def test_existing_offline_activation_is_preserved_and_revalidated(controller,monkeypatch):
    controller.update({'config':configured()});controller.initialize_identity()
    vault=controller._vault();vault.data['activation']={'mode':'offline','document':json.dumps({'payload':{'tenant_id':'company'}}),'public_key':'stored-public-key'};vault.save()
    validate=Mock(side_effect=lambda value,identity:value)
    monkeypatch.setattr('prodcast_manager.activation.validate_activation',validate)
    controller.activation_save({'mode':'offline'})
    assert validate.call_args.args[0]['public_key']=='stored-public-key'
    assert controller._read_vault()['activation']['tenant_id']=='company'
