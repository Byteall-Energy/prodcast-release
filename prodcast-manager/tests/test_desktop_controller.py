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


def test_worker_service_account_is_encrypted_separate_and_redacted(controller):
    secret='synthetic-worker-service-secret'
    controller.worker_service_save(dict(role='worker1',mode='custom',account='EXAMPLE\\svc-worker',password=secret))
    assert controller._read_vault()['worker_services']['worker1']['password']==secret
    assert 'worker1' not in controller._read_vault().get('ssh',{})
    public=controller.snapshot()
    assert public['worker_services']['worker1']['password_stored']
    assert secret not in json.dumps(public)
    controller.save_draft()
    assert secret not in (controller.directory/'ui-draft.json').read_text('utf-8')
    assert secret not in controller.log.redact('failure '+secret)
    controller.worker_service_save(dict(role='worker1',mode='custom',account='EXAMPLE\\svc-worker'))
    assert controller._read_vault()['worker_services']['worker1']['password']==secret
    controller.worker_service_save(dict(role='worker1',mode='preserve'))
    assert not controller._read_vault()['worker_services']['worker1']['password']


def test_service_account_rejects_nonworker_and_invalid_account(controller):
    with pytest.raises(ValueError):controller.worker_service_save(dict(role='app',mode='custom',account='EXAMPLE\\svc'))
    with pytest.raises(ValueError):controller.worker_service_save(dict(role='worker1',mode='custom',account='bare-user'))


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


def test_worker_queue_settings_survive_sync_and_unused_address_removal(controller):
    value=configured();value['hosts']['worker1']['execution_queues']=['workflows','workflows_data']
    value['hosts']['worker2']['execution_queues']=['workflows_data','scenarios']
    controller.update({'config':value});controller.save_profile()
    vault=controller._vault();vault.initialize(controller.config);vault.save()
    before=copy.deepcopy(runtime_site(controller))
    role=controller.add_worker_draft()['role']
    controller.update({'config':controller.config});controller.remove_worker_draft({'role':role})
    assert runtime_site(controller)==before
    assert controller.config['hosts']['worker1']['execution_queues']==['workflows','workflows_data']
    assert controller.config['hosts']['worker2']['execution_queues']==['workflows_data','scenarios']


def test_unknown_public_fields_get_precise_error_without_leaking_values(controller):
    value=copy.deepcopy(controller.config);value['hosts']['worker1']['unexpected']='private-test-value'
    with pytest.raises(ValueError,match='worker1: unsupported server profile fields: unexpected') as error:controller.update({'config':value})
    assert 'private-test-value' not in str(error.value)


@pytest.mark.parametrize('queues',['workflows',['workflows','workflows'],['bad queue'],[{'secret':'value'}]])
def test_invalid_worker_queue_metadata_is_rejected(controller,queues):
    value=copy.deepcopy(controller.config);value['hosts']['worker1']['execution_queues']=queues
    with pytest.raises(ValueError,match='execution_queues'):controller.update({'config':value})


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


def completed_worker_removal(controller):
    from prodcast_manager.worker_lifecycle import removal_target
    from prodcast_manager.config import worker_addresses
    controller.update({'config':configured()});controller.save_profile()
    original=runtime_site(controller);stamp=controller._membership_stamp()
    target=removal_target(original,'worker2')
    vault=controller._vault();vault.initialize(original)
    vault.data.update(worker_hosts=worker_addresses(target),worker_topology_hosts=target['_worker_topology_hosts'])
    vault.data.setdefault('retired_workers',{})['worker2']={'address':original['hosts']['worker2']['address'],'operation':'removed'}
    vault.save();save_config(controller.directory,target)
    atomic_json(controller.directory/'worker-action-journal.json',dict(status='complete',mode='worker-remove',role='worker2',operation='removed',original=original,target=target))
    return original,target,stamp


def test_legacy_stale_draft_is_reconciled_without_losing_other_edits(controller):
    original,target,stamp=completed_worker_removal(controller)
    original['company_name']='Keep draft company'
    draft=dict(config=original,packages=dict(release='selected-release.zip'))
    atomic_json(controller.directory/'ui-draft.json',draft)
    reopened=DesktopController(controller.home)
    assert set(reopened.config['hosts'])==set(target['hosts'])
    assert reopened.config['company_name']=='Keep draft company'
    assert reopened.packages['release']=='selected-release.zip'
    assert list((controller.directory/'history').glob('ui-draft-before-membership-reconcile-*.json'))
    reopened._prepare()  # Update no longer fails the installed-membership guard.


def test_stale_open_window_cannot_resurrect_removed_worker(controller):
    original,target,stamp=completed_worker_removal(controller)
    result=controller.update(dict(config=original,membership_stamp=stamp))
    assert result['membership_reconciled']
    controller.save_draft();controller._prepare()
    assert 'worker2' not in controller.snapshot()['config']['hosts']


def test_explicit_readd_draft_after_removal_is_not_silently_deleted(controller):
    original,target,stamp=completed_worker_removal(controller)
    fresh=controller._membership_stamp()
    controller.update(dict(config=original,membership_stamp=fresh));controller.save_draft()
    reopened=DesktopController(controller.home)
    assert 'worker2' in reopened.config['hosts']
    with pytest.raises(ValueError,match='membership'):reopened._prepare()


def test_failed_removal_does_not_silently_change_draft_membership(controller):
    original,target,stamp=completed_worker_removal(controller)
    journal=controller._json(controller.directory/'worker-action-journal.json');journal['status']='failed'
    atomic_json(controller.directory/'worker-action-journal.json',journal)
    controller.update(dict(config=original,membership_stamp=stamp))
    assert 'worker2' in controller.config['hosts']


def test_reconciliation_does_not_hide_unrelated_address_changes(controller):
    original,target,stamp=completed_worker_removal(controller)
    original['hosts']['worker1']['address']='10.20.30.222'
    controller.update(dict(config=original,membership_stamp=stamp))
    assert 'worker2' not in controller.config['hosts']
    with pytest.raises(ValueError,match='membership'):controller._prepare()


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


def test_remove_uninstalled_address_keeps_runtime_profile_and_persists_draft(controller):
    controller.update({'config':configured()});controller.save_profile()
    vault=controller._vault();vault.initialize(controller.config);vault.save()
    before=copy.deepcopy(runtime_site(controller));secrets=copy.deepcopy(vault.data)
    role=controller.add_worker_draft()['role'];assert role=='worker3'
    assert role in controller.snapshot()['draft_workers']
    assert controller.remove_worker_draft({'role':role})['local_only']
    assert runtime_site(controller)==before and controller._read_vault()==secrets
    assert role not in DesktopController(controller.home).config['hosts']


def test_remove_after_failed_add_preflight_cancels_only_local_plan(controller):
    controller.config=configured();controller.save_profile()
    vault=controller._vault();vault.initialize(controller.config);vault.save()
    role=controller.add_worker_draft()['role']
    path=controller.directory/'workers-journal.json'
    atomic_json(path,dict(operation='failed-preflight',status='failed',phase='preflight',new_workers=[role],steps=[]))
    controller.remove_worker_draft({'role':role})
    assert controller._json(path)['status']=='cancelled'
    assert controller.snapshot()['pending_worker_addition'] is None
    assert list((controller.directory/'history').glob('workers-before-cancel-*'))


def test_draft_remove_cannot_hide_server_changes_or_remove_installed_member(controller):
    controller.config=configured();controller.save_profile()
    vault=controller._vault();vault.initialize(controller.config);vault.save()
    role=controller.add_worker_draft()['role']
    atomic_json(controller.directory/'workers-journal.json',dict(operation='started',status='failed',phase='applying',new_workers=[role],steps=[]))
    with pytest.raises(ValueError,match='Cancel'):controller.remove_worker_draft({'role':role})
    with pytest.raises(ValueError,match='installed roster'):controller.remove_worker_draft({'role':'worker1'})
    assert role in controller.config['hosts']


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
