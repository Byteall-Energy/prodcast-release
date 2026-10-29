import copy
import io
import itertools
import json
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_component_updates import deployed
from test_desktop_controller import controller, configured
from test_remote_configuration import agent
from test_workers import expansion
from prodcast_manager.config import example, topology_hash, validate
from prodcast_manager.component_updates import changed_roles, receipt
from prodcast_manager.portable import save_config, runtime_config, import_site
from prodcast_manager.worker_lifecycle import run_worker_action
from prodcast_manager.worker_queues import QUEUES, execution_queues, validate_release_queues
from prodcast_manager.workers import binding, candidate

COMBINATIONS = [list(q) for n in range(1, 4) for q in itertools.combinations(QUEUES, n)]


def supported(release):
    release.doc = {'management': {'worker_queue_selection': {'schema': 1}}}
    return release


@pytest.mark.parametrize('queues', COMBINATIONS)
def test_profile_roundtrip_import_and_identity(tmp_path, queues):
    config=example(); before=topology_hash(config)
    config['hosts']['worker1']['execution_queues']=queues
    validate(config)
    assert topology_hash(config)==before
    directory=tmp_path/'profile';saved=save_config(directory,config)
    assert execution_queues(saved['hosts']['worker1'])==queues
    _, imported=import_site(directory/'site.json',tmp_path/'portable')
    assert imported['hosts']['worker1']['execution_queues']==queues
    assert execution_queues(imported['hosts']['worker2'])==list(QUEUES)


@pytest.mark.parametrize('queues', [[], None, '', 'workflows', ['default'], ['workflows','workflows'], [1], [{}]])
def test_invalid_selection_rejected_in_profile(queues):
    config=example();config['hosts']['worker1']['execution_queues']=queues
    with pytest.raises(ValueError,match='execution_queues'):validate(config)


def test_legacy_and_unsupported_packages():
    config=example();release=SimpleNamespace(doc={})
    validate_release_queues(config,release)
    config['hosts']['worker1']['execution_queues']=['scenarios']
    with pytest.raises(ValueError,match='compatible Worker'):validate_release_queues(config,release)
    supported(release);validate_release_queues(config,release)
    release.doc['management']['worker_queue_selection']={'schema':True}
    with pytest.raises(ValueError,match='contract'):validate_release_queues(config,release)


def test_draft_keeps_each_workers_queues(controller):
    config=configured()
    config['hosts']['worker1']['execution_queues']=['workflows']
    config['hosts']['worker2']['execution_queues']=['workflows_data','scenarios']
    controller.update({'config':config});controller.save_draft()
    reopened=type(controller)(controller.home)
    assert reopened.config['hosts']==config['hosts']
    reopened.save_profile()
    assert runtime_config(reopened.directory/'site.json')['hosts']==config['hosts']


@pytest.mark.parametrize('queues', COMBINATIONS)
def test_selective_update_and_repeat_preserve_selection(deployed, queues):
    e=deployed;supported(e.release)
    e.c['hosts']['worker1']['execution_queues']=queues
    before=copy.deepcopy(e.c)
    result=e.run('update')
    assert result['changed_roles']==([] if queues==list(QUEUES) else ['worker1'])
    for role,action,payload in e.test_calls:
        if role=='worker1' and action=='install':
            assert payload['site']['hosts']['worker1']['execution_queues']==queues
        if role in ('app','db','worker2'):assert action not in ('stop','install','configure','repair')
    assert e.c==before
    assert changed_roles(e,e.vault.data['deployment_components'])==()
    assert e.run('update')['changed_roles']==[]


def test_update_cannot_change_queues_midflight(deployed):
    e=deployed;supported(e.release)
    e.c['hosts']['worker1']['execution_queues']=['scenarios']
    e.test_remote.fail=('worker1','install')
    with pytest.raises(RuntimeError):e.run('update')
    e.c['hosts']['worker1']['execution_queues']=['workflows_data']
    e.test_remote.fail=None
    with pytest.raises(ValueError,match='settings'):e.run('update')
    e.c['hosts']['worker1']['execution_queues']=['scenarios']
    assert e.run('update')['changed_roles']==['worker1']


@pytest.mark.parametrize('queues', COMBINATIONS)
def test_worker_repair_persists_selected_queues_on_resume(tmp_path, expansion, queues):
    c,_,vault,release,remote=expansion;supported(release)
    remote.failure=('worker2','repair')
    with pytest.raises(RuntimeError):
        run_worker_action(tmp_path,'worker2','worker-repair',vault,release,remote_factory=remote,selected_queues=queues)
    remote.failure=None
    result=run_worker_action(tmp_path,'worker2','worker-repair',vault,release,remote_factory=remote,selected_queues=queues)
    assert result['hosts']['worker2']['execution_queues']==queues
    assert result['hosts']['worker1']==c['hosts']['worker1']
    journal=json.loads((tmp_path/'worker-action-journal.json').read_text())
    assert journal['target']['hosts']['worker2']['execution_queues']==queues


def test_repair_rejects_legacy_payload_before_remote_calls(tmp_path, expansion):
    _,_,vault,release,remote=expansion
    with pytest.raises(ValueError,match='compatible Worker'):
        run_worker_action(tmp_path,'worker2','worker-repair',vault,release,remote_factory=remote,selected_queues=['scenarios'])
    assert remote.calls==[]


def test_queue_selection_binds_retry_but_not_installation_identity():
    config=example();other=copy.deepcopy(config)
    other['hosts']['worker1']['execution_queues']=list(QUEUES)
    assert binding(config)==binding(other)
    other['hosts']['worker1']['execution_queues']=['scenarios']
    assert binding(config)!=binding(other)
    assert topology_hash(config)==topology_hash(other)


def test_add_worker_cannot_silently_change_existing_subscriptions(expansion):
    c,target,_,_,_=expansion
    target['hosts']['worker1']['execution_queues']=['scenarios']
    with pytest.raises(ValueError,match='separately'):candidate(c,target)


def consumer_control(agent,monkeypatch,active,fail_drain=False):
    calls=[]
    class Inspect:
        def active_queues(self):return {n:[{'name':q} for q in qs] for n,qs in active.items()}
        def active(self):
            if fail_drain:raise RuntimeError('drain failed')
            return {n:[] for n in active}
        reserved=active;scheduled=active
    class Control:
        def inspect(self,**kw):return Inspect()
        def broadcast(self,action,arguments,destination,**kw):
            node=destination[0];q=arguments['queue'];calls.append((action,node,q))
            if action=='add_consumer':active[node].add(q)
            else:active[node].discard(q)
            return [{node:{'ok':True}}]
    monkeypatch.setitem(sys.modules,'mainapp.celery',SimpleNamespace(app=SimpleNamespace(control=Control())))
    def execute(code,current=False):
        output=io.StringIO()
        with redirect_stdout(output):exec(code,{})
        return output.getvalue()
    monkeypatch.setattr(agent,'app_python',execute)
    return calls


@pytest.mark.parametrize('queues', COMBINATIONS)
def test_drain_and_resume_exact_subscriptions(agent,monkeypatch,queues):
    selected='prodcast-worker-01@host';other='prodcast-worker-02@host'
    active={selected:set(queues),other:set(QUEUES)}
    calls=consumer_control(agent,monkeypatch,active)
    agent.C['hosts']['worker1']['execution_queues']=queues;agent.P['changed_workers']=['worker1']
    result=agent.selected_worker_consumers(False)
    assert set(result['previous_queues'])=={'prodcast-worker-01@'}
    assert set(result['previous_queues']['prodcast-worker-01@'])==set(queues)
    assert active[selected]==set()
    agent.selected_worker_consumers(True)
    assert active[selected]==set(queues) and active[other]==set(QUEUES)
    assert all(node==selected for _,node,_ in calls)


def test_failed_drain_restores_actual_old_subscriptions(agent,monkeypatch):
    selected='prodcast-worker-01@host';active={selected:{'workflows'}}
    consumer_control(agent,monkeypatch,active,fail_drain=True)
    agent.C['hosts']['worker1']['execution_queues']=['scenarios'];agent.P['changed_workers']=['worker1']
    with pytest.raises(RuntimeError,match='drain failed'):agent.selected_worker_consumers(False)
    assert active[selected]=={'workflows'}


def test_resume_cancels_extra_subscriptions(agent,monkeypatch):
    node='prodcast-worker-01@host';active={node:set(QUEUES)}
    consumer_control(agent,monkeypatch,active)
    agent.C['hosts']['worker1']['execution_queues']=['scenarios'];agent.P['changed_workers']=['worker1']
    agent.selected_worker_consumers(True)
    assert active[node]=={'scenarios'}


@pytest.mark.parametrize('extra',[False,True])
def test_verification_requires_exact_queue_set(agent,monkeypatch,extra):
    expected=['scenarios'];agent.C['hosts']['worker1']['execution_queues']=expected
    nodes={'prodcast-worker-01@host':[{'name':q} for q in expected+(['workflows'] if extra else [])],
           'prodcast-worker-02@host':[{'name':q} for q in QUEUES], 'celery@app':[{'name':'default'}]}
    monkeypatch.setitem(sys.modules,'mainapp.celery',SimpleNamespace(app=SimpleNamespace(control=SimpleNamespace(inspect=lambda **kw:SimpleNamespace(active_queues=lambda:nodes)))))
    db=MagicMock();db.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value=(True,)
    monkeypatch.setitem(sys.modules,'psycopg2',db)
    monkeypatch.setitem(sys.modules,'redis',SimpleNamespace(Redis=SimpleNamespace(from_url=lambda _:SimpleNamespace(ping=lambda:True))))
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'status':'active'})
    monkeypatch.setitem(sys.modules,'requests',SimpleNamespace(get=lambda *a,**kw:response))
    for key in ('DATABASE_URL','CELERY_BROKER_URL','LICENSE_SERVICE_URL','LICENSE_TENANT_ID','LICENSE_SERVICE_TOKEN'):monkeypatch.setenv(key,'synthetic')
    monkeypatch.setattr(agent,'app_python',lambda code:exec(code,{}));monkeypatch.setattr(agent,'request',lambda *a,**kw:None)
    if extra:
        with pytest.raises(AssertionError,match='subscriptions'):agent.verify()
    else:assert agent.verify()['workers']==2


def test_failed_replacement_restores_old_queue_selection(deployed):
    e=deployed;supported(e.release)
    old_action=e.test_remote.action
    def action(remote,payload,name,resources):
        result=old_action(remote,payload,name,resources)
        if name=='drain-workers':result['previous_queues']={'prodcast-worker-01@':['workflows']}
        return result
    e.test_remote.action=action
    e.c['hosts']['worker1']['execution_queues']=['scenarios']
    e.test_remote.fail=('worker1','stop')
    with pytest.raises(RuntimeError):e.run('update')
    resumes=[p for r,a,p in e.test_calls if a=='resume-workers']
    assert resumes[-1]['resume_queues']=={'prodcast-worker-01@':['workflows']}


def test_completed_replacement_keeps_new_selection_when_later_step_fails(deployed):
    e=deployed;supported(e.release)
    old_action=e.test_remote.action
    def action(remote,payload,name,resources):
        result=old_action(remote,payload,name,resources)
        if name=='drain-workers':result['previous_queues']={'prodcast-worker-01@':['workflows']}
        return result
    e.test_remote.action=action
    e.c['hosts']['worker1']['execution_queues']=['scenarios']
    e.test_remote.fail=('app','resume-workers')
    with pytest.raises(RuntimeError):e.run('update')
    resumes=[p for r,a,p in e.test_calls if a=='resume-workers']
    assert resumes[-1]['resume_queues']=={'prodcast-worker-01@':['scenarios']}
