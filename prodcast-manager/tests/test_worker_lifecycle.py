import copy
import json
import pytest
from test_workers import expansion
from test_workers import mixed_expansion
from prodcast_manager.worker_lifecycle import run_worker_action, removal_target, JOURNAL
from prodcast_manager.config import topology_hash, validate, validate_membership, worker_roles
from prodcast_manager.portable import runtime_config, open_credentials


def test_remove_middle_preserves_identity_and_validates(expansion):
    c, target, vault, release, remote = expansion
    original = copy.deepcopy(target)
    result = removal_target(original, 'worker2')
    assert worker_roles(result) == ('worker1', 'worker3')
    assert topology_hash(result) == topology_hash(original)
    validate(result, True)


def test_last_worker_cannot_be_removed(expansion):
    c, _, _, _, _ = expansion
    one = removal_target(c, 'worker2')
    with pytest.raises(ValueError, match='at least one'):
        removal_target(one, 'worker1')


def test_repair_touches_only_app_and_selected_worker(tmp_path, expansion):
    c, _, vault, release, remote = expansion
    before = copy.deepcopy(vault.data['secrets'])
    result = run_worker_action(tmp_path, 'worker2', 'worker-repair', vault, release, remote_factory=remote)
    assert {r for r, _ in remote.calls} == {'app', 'worker2'}
    assert ('worker2', 'reset') in remote.calls
    assert ('worker2', 'repair') in remote.calls
    assert ('worker2', 'verify') in remote.calls
    assert result['hosts'] == c['hosts']
    assert before == vault.data['secrets']
    assert json.loads((tmp_path / JOURNAL).read_text())['status'] == 'complete'


def test_remove_updates_local_roster_and_releases_access(tmp_path, expansion):
    c, _, vault, _, remote = expansion
    result = run_worker_action(tmp_path, 'worker2', 'worker-remove', vault, remote_factory=remote)
    assert worker_roles(result) == ('worker1',)
    assert topology_hash(result) == topology_hash(c)
    validate_membership(result, vault.data)
    assert ('db', 'worker-retire-access') in remote.calls
    assert ('worker2', 'worker-retire') in remote.calls
    assert ('db', 'worker-retire-membership') in remote.calls
    assert ('worker1', 'maintenance-stop') not in remote.calls


def test_removal_prunes_only_deleted_workers_component_receipt(tmp_path,expansion):
    c,_,vault,_,remote=expansion
    from prodcast_manager.config import core_roles
    records={r:dict(signature='preserve-'+r,version=remote.states[r]['version'],manifest=remote.states[r]['manifest']) for r in core_roles(c)}
    vault.data['deployment_components']={'schema':1,'topology':topology_hash(c),'installation_id':vault.data['installation_id'],'roles':copy.deepcopy(records)}
    run_worker_action(tmp_path,'worker2','worker-remove',vault,remote_factory=remote)
    assert vault.data['deployment_components']['roles']=={r:v for r,v in records.items() if r!='worker2'}


def test_mixed_version_removal_preserves_remaining_component_content(tmp_path,expansion):
    from prodcast_manager.engine import Engine
    from prodcast_manager.component_updates import changed_roles
    _,c,target,v,release,remote=mixed_expansion(tmp_path,expansion)
    before=copy.deepcopy(v.data['deployment_components']['roles']);seen=[]
    class Capture(remote):
        def action(self,p,action,resources):
            seen.append((self.role,action,dict(p)))
            return super().action(p,action,resources)
    result=run_worker_action(tmp_path,'worker2','worker-remove',v,remote_factory=Capture)
    assert all(p['manifest']=='older-db' for r,a,p in seen if r=='db')
    after=Engine(result,v,release,tmp_path)
    assert changed_roles(after,v.data['deployment_components'])==()
    for role,record in v.data['deployment_components']['roles'].items():
        assert record['content_signature']==before[role]['content_signature']
        assert record['version']==before[role]['version']


def test_individual_worker_repair_accepts_older_worker_than_app(tmp_path,expansion):
    _,c,target,v,release,remote=mixed_expansion(tmp_path,expansion)
    remote.states['app'].update(version='v0.4',manifest='newer-app')
    v.data['deployment_components']['roles']['app'].update(version='v0.4',manifest='newer-app')
    seen=[]
    class Capture(remote):
        def action(self,p,action,resources):
            seen.append((self.role,action,dict(p)));return super().action(p,action,resources)
    run_worker_action(tmp_path,'worker1','worker-repair',v,release,remote_factory=Capture)
    assert all(p['manifest']=='newer-app' for r,a,p in seen if r=='app')
    assert all(p['manifest']==release.digest for r,a,p in seen if r=='worker1')
    assert not any(r=='db' for r,a,p in seen)


def test_mixed_removal_resumes_after_local_membership_commit(tmp_path,expansion):
    _,c,target,v,release,remote=mixed_expansion(tmp_path,expansion)
    remote.failure=('app','maintenance-resume')
    with pytest.raises(RuntimeError):run_worker_action(tmp_path,'worker2','worker-remove',v,remote_factory=remote)
    assert json.loads((tmp_path/JOURNAL).read_text())['phase']=='committing'
    remote.failure=None
    result=run_worker_action(tmp_path,'worker2','worker-remove',v,remote_factory=remote)
    assert 'worker2' not in result['hosts'] and 'worker2' not in v.data['deployment_components']['roles']


@pytest.mark.parametrize('failure', [('worker2', 'repair'), ('db', 'worker-retire-membership'), ('app', 'maintenance-resume')])
def test_resume_does_not_repeat_completed_destructive_steps(tmp_path, expansion, failure):
    c, _, vault, release, remote = expansion
    mode = 'worker-repair' if failure[1] == 'repair' else 'worker-remove'
    remote.failure = failure
    with pytest.raises(RuntimeError, match='disconnect'):
        run_worker_action(tmp_path, 'worker2', mode, vault, release if mode == 'worker-repair' else None, remote_factory=remote)
    reset_count = remote.calls.count(('worker2', 'reset'))
    retire_count = remote.calls.count(('worker2', 'worker-retire'))
    remote.failure = None
    run_worker_action(tmp_path, 'worker2', mode, vault, release if mode == 'worker-repair' else None, remote_factory=remote)
    assert remote.calls.count(('worker2', 'reset')) == reset_count
    assert remote.calls.count(('worker2', 'worker-retire')) == retire_count


def test_failed_drain_never_stops_or_removes_worker(tmp_path, expansion):
    c, _, vault, _, remote = expansion
    remote.failure = ('app', 'drain')
    with pytest.raises(RuntimeError):
        run_worker_action(tmp_path, 'worker2', 'worker-remove', vault, remote_factory=remote)
    assert ('worker2', 'maintenance-stop') not in remote.calls
    assert ('db', 'worker-retire-access') not in remote.calls
    assert runtime_config(tmp_path / 'site.json')['hosts'] == c['hosts']


def test_gui_stable_ids_after_middle_removal(expansion):
    from prodcast_manager.gui import App
    from unittest.mock import Mock
    c, target, _, _, _ = expansion
    app = App.__new__(App)
    app.original_config = removal_target(target, 'worker2')
    app.worker_count = Mock(get=lambda:'2')
    assert app.visible_workers() == ('worker1', 'worker3')
    app.worker_count = Mock(get=lambda:'3')
    assert app.visible_workers() == ('worker1', 'worker2', 'worker3')


@pytest.mark.parametrize('legacy',[False,True])
def test_removed_worker_can_be_added_again_with_new_address(tmp_path,expansion,legacy):
    from prodcast_manager.workers import add_workers
    c,_,vault,release,remote=expansion
    removed=run_worker_action(tmp_path,'worker2','worker-remove',vault,remote_factory=remote)
    if legacy:
        vault.data.pop('retired_workers',None);vault.save()
    remote.states['worker2']['retired']=True
    target=copy.deepcopy(removed)
    target['hosts']['worker2']={**c['hosts']['worker2'],'address':'192.168.31.155'}
    result=add_workers(tmp_path,target,vault,release,remote_factory=remote)
    assert result['hosts']['worker2']['address']=='192.168.31.155'
    assert topology_hash(result)==topology_hash(c)
    validate_membership(result,vault.data)
    assert 'worker2' not in vault.data['retired_workers']


def test_cloned_active_worker_is_not_silently_reclaimed(tmp_path,expansion):
    from prodcast_manager.workers import add_workers
    c,_,vault,release,remote=expansion
    removed=run_worker_action(tmp_path,'worker2','worker-remove',vault,remote_factory=remote)
    target=copy.deepcopy(removed);target['hosts']['worker2']=c['hosts']['worker2']
    remote.calls.clear()
    with pytest.raises(ValueError,match='already belongs'):
        add_workers(tmp_path,target,vault,release,remote_factory=remote)
    assert ('db','workers-prepare') not in remote.calls
