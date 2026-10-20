import json
import pytest
from test_manager import engine,FakeRemote
from test_remote_configuration import agent
from prodcast_manager.ai_policy import preserve_existing_ai


def test_update_preserves_installed_ai_even_if_checkbox_was_cleared(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False
    prior={'status':'complete','version':'v0.2','operation':'ai-original'}
    (tmp_path/'ai-journal.json').write_text(json.dumps(prior))
    e.run('update')
    assert e.c['install_ai'] and e.keep_ai
    assert json.loads((tmp_path/'journal.json').read_text())['install_ai']
    assert json.loads((tmp_path/'ai-journal.json').read_text())==prior
    assert not any(r=='ai' for r,a,op in FakeRemote.history)


def test_never_installed_ai_can_still_be_omitted(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False
    assert not preserve_existing_ai(e.c,e.vault,e.dir)
    assert not e.c['install_ai']


def prepare_reconnect(tmp_path):
    e=engine(tmp_path);e.vault.initialize(e.c)
    (tmp_path/'journal.json').write_text(json.dumps(dict(status='complete',operation='core',mode='update',install_ai=False)))
    (tmp_path/'ai-journal.json').write_text(json.dumps(dict(status='complete',operation='old-ai',version='v0.2')))
    return e


def test_reconnect_existing_ai_never_loads_packages_or_touches_workers(tmp_path):
    e=prepare_reconnect(tmp_path)
    e.release.for_role=lambda role:pytest.fail('Reconnect must not read any package/model assets')
    result=e.run('ai')
    assert result['existing_ai_preserved'] and not result['models_reinstalled']
    actions={(r,a) for r,a,op in FakeRemote.history}
    assert actions=={('ai','preflight'),('db','preflight'),('app','preflight'),('ai','ai-present'),('db','ai-access'),('ai','ai-ready'),('app','ai-reconnect'),('app','verify-ai')}
    assert json.loads((tmp_path/'journal.json').read_text())['install_ai']
    assert json.loads((tmp_path/'history/ai-before-reconnect-core.json').read_text())['operation']=='old-ai'


def test_reconnect_preflight_failure_preserves_original_ai_journal(tmp_path):
    e=prepare_reconnect(tmp_path);prior=(tmp_path/'ai-journal.json').read_text()
    FakeRemote.fail=('app','preflight')
    with pytest.raises(RuntimeError):e.run('ai')
    assert (tmp_path/'ai-journal.json').read_text()==prior
    assert all(a=='preflight' for r,a,op in FakeRemote.history)


def test_failed_link_can_resume_without_reinstalling_models(tmp_path):
    e=prepare_reconnect(tmp_path);FakeRemote.fail=('app','ai-reconnect')
    with pytest.raises(RuntimeError):e.run('ai')
    assert not json.loads((tmp_path/'journal.json').read_text())['install_ai']
    assert json.loads((tmp_path/'ai-journal.json').read_text())['status']=='failed'
    FakeRemote.fail=None;FakeRemote.history=[]
    assert e.run('ai')['status']=='complete'
    assert not any(a in ('install','bootstrap','stop') for r,a,op in FakeRemote.history)


def test_disabled_message_points_to_ai_tab(tmp_path):
    e=prepare_reconnect(tmp_path);e.c['install_ai']=False
    with pytest.raises(ValueError,match='«AI»'):e.run('ai')


def test_first_ai_install_after_disabled_core_can_configure_connection(tmp_path):
    e=prepare_reconnect(tmp_path);(tmp_path/'ai-journal.json').unlink()
    result=e.run('ai')
    assert result['status']=='complete'
    actions=[(r,a) for r,a,op in FakeRemote.history]
    assert ('db','ai-access') in actions and ('app','ai-reconnect') in actions
    assert actions.index(('db','ai-access'))<actions.index(('ai','install'))
    assert actions.index(('app','ai-reconnect'))<actions.index(('app','verify-ai'))
    assert not any(r.startswith('worker') for r,a in actions)
    assert json.loads((tmp_path/'journal.json').read_text())['install_ai']


def test_database_reconnect_adds_only_ai_access_without_restart(agent,monkeypatch):
    m=agent;m.P.update(role='db',operation='core');m.ST['version']='v0.2'
    path=m.ROOT/'db/config/pg_hba.conf';path.parent.mkdir(parents=True)
    original='local all postgres peer\nhostssl prodcast2 prodcast_app 192.0.2.1/32 scram-sha-256\nhost all all 0.0.0.0/0 reject\n'
    path.write_text(original);calls=[]
    def sql(text,*args):
        calls.append(text)
        return '0' if 'count(*)' in text else 't' if 'pg_reload_conf' in text else '1'
    monkeypatch.setattr(m,'psql',sql);monkeypatch.setattr(m,'firewall',lambda role:calls.append('firewall:'+role))
    assert not m.reconnect_ai_database()['database_restarted']
    once=path.read_text();m.reconnect_ai_database()
    assert path.read_text()==once
    assert 'hostssl prodcast2 prodcast_app 192.0.2.1/32 scram-sha-256' in once
    assert once.index('hostssl prodcast_ai')<once.index('host all all')
    assert not any('ALTER ROLE' in x or 'DROP' in x for x in calls)


def test_app_reconnect_changes_only_ai_flags_and_restores_on_failure(agent,monkeypatch):
    m=agent;m.P.update(role='app',operation='core');m.ST['version']='v0.2'
    m.S.update(AI_KEY='existing-key',AI_BASIC='existing-password')
    path=m.old_target()/'app/runtime/runtime.env';path.parent.mkdir(parents=True)
    original='PRODCAST_AI_ENABLED=false\nPRODCAST_AI_BASE_URL=\nPRODCAST_AI_API_KEY=existing-key\nPRODCAST_AI_BASIC_AUTH_PASSWORD=existing-password\nOTHER=keep\n'
    path.write_text(original);calls=[]
    monkeypatch.setattr(m,'healthy_services',lambda role:set(m.APIS+['celery-worker','celery-beat']))
    monkeypatch.setattr(m,'current_dc',lambda *args,**kw:calls.append(args))
    assert m.reconnect_ai_app()['app_restarted']
    assert 'OTHER=keep' in path.read_text() and 'PRODCAST_AI_ENABLED=true' in path.read_text()
    assert all('gateway' in args and 'frontend' not in args for args in calls)
    path.write_text(original);calls.clear()
    def fail_once(*args,**kw):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('simulated restart failure')
    monkeypatch.setattr(m,'current_dc',fail_once)
    with pytest.raises(RuntimeError):m.reconnect_ai_app()
    assert path.read_text()==original and len(calls)==2
