"""Exercise real Linux readiness/reconciliation with isolated system boundaries."""
import pytest
import sys
from types import SimpleNamespace
from test_remote_configuration import agent


def prepare(m,monkeypatch):
    m.P.update(role='app',operation='original',manifest='expected',images={
        'prodcast-backend':{'transport_tag':'backend'},
        'prodcast-frontend':{'transport_tag':'frontend'},
        'prodcast-gateway':{'transport_tag':'gateway'}})
    m.ST.update(version=m.P['version'],manifest='expected',operation='original')
    m.ST['step_tls']={'original:configure':m.tls_revision()}
    monkeypatch.setattr(m,'healthy_services',lambda role:set(m.APIS+['frontend','gateway','celery-worker','celery-beat','api','license-proxy']))
    monkeypatch.setattr(m,'verify',lambda:{'healthy':True})
    monkeypatch.setattr(m,'run',lambda args,**kw:'sha256:running')
    monkeypatch.setattr(m,'dc',lambda *args,**kw:'container-id')


def test_healthcheck_does_not_write_or_start_services(agent,monkeypatch):
    prepare(agent,monkeypatch)
    monkeypatch.setattr(agent,'save',lambda:pytest.fail('read-only check must not save'))
    assert agent.installation_healthcheck()['services_restarted'] is False


def test_healthcheck_requires_scheduler_and_does_not_start_it(agent,monkeypatch):
    prepare(agent,monkeypatch)
    monkeypatch.setattr(agent,'healthy_services',lambda role:set(agent.APIS+['frontend','gateway','celery-worker']))
    with pytest.raises(RuntimeError,match='scheduler'):agent.installation_healthcheck()


def test_healthcheck_rejects_old_image_even_if_ready(agent,monkeypatch):
    prepare(agent,monkeypatch)
    monkeypatch.setattr(agent,'run',lambda args,**kw:'expected' if 'image' in args else 'old')
    with pytest.raises(RuntimeError,match='running image'):agent.installation_healthcheck()


def test_reconcile_clears_only_ownership_and_receipt(agent,monkeypatch):
    prepare(agent,monkeypatch);saved=[]
    agent.P['action']='reconcile'
    monkeypatch.setattr(agent,'save',lambda:saved.append(True))
    assert agent.dispatch()['healthy']
    assert agent.ST['operation']=='' and saved==[True]


def test_unapplied_release_cannot_be_declared_complete(agent,monkeypatch):
    prepare(agent,monkeypatch);agent.ST['manifest']='old'
    with pytest.raises(RuntimeError,match='not fully installed'):agent.installation_healthcheck()


def test_completed_component_checks_installed_compose_images_not_selected_zip(agent,monkeypatch):
    import json
    prepare(agent,monkeypatch);agent.P['installed_runtime']=True;agent.P['images']={}
    roles=agent.APIS+['frontend','gateway','celery-worker','celery-beat']
    monkeypatch.setattr(agent,'current_dc',lambda *a,**kw:json.dumps({'services':{r:{'image':'installed-'+r} for r in roles}}))
    inspected=[]
    def run(args,**kw):
        if args[:3]==['docker','image','inspect']:
            inspected.append(args[-1]);assert args[-1].startswith('installed-')
        return 'sha256:installed'
    monkeypatch.setattr(agent,'run',run)
    assert agent.installation_healthcheck()['healthy']
    assert set(inspected)=={'installed-'+r for r in roles}


@pytest.mark.parametrize('resume',[False,True])
def test_consumer_control_targets_only_selected_worker(agent,monkeypatch,resume):
    calls=[];selected='prodcast-worker-01@host';other='prodcast-worker-02@host'
    class Inspect:
        def active_queues(self):return {selected:[] if resume else [{'name':q} for q in ('workflows','workflows_data','scenarios')],other:[]}
        def active(self):return {selected:[]}
        reserved=active;scheduled=active
    class Control:
        def inspect(self,**kw):return Inspect()
        def broadcast(self,action,**kw):
            calls.append((action,kw));return [{selected:{'ok':True}}]
    monkeypatch.setitem(sys.modules,'mainapp',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'mainapp.celery',SimpleNamespace(app=SimpleNamespace(control=Control())))
    agent.P['changed_workers']=['worker1']
    monkeypatch.setattr(agent,'app_python',lambda code,current:exec(code,{}))
    assert agent.selected_worker_consumers(resume)['app_restarted'] is False
    assert len(calls)==3
    assert all(kw['destination']==[selected] for action,kw in calls)
    assert all(action==('add_consumer' if resume else 'cancel_consumer') for action,kw in calls)
