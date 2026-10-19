import copy
import json
from pathlib import Path
import pytest
from test_manager import engine, FakeRemote, FakeRelease
from prodcast_manager.config import CORE_ROLES, topology_hash, example
from prodcast_manager.engine import plan, RESOURCES
from prodcast_manager.portable import save_config


def report(folder):
    journal=json.loads((folder/'journal.json').read_text('utf-8'))
    assert journal['status']=='complete'
    result=json.loads((folder/('report-'+journal['operation']+'.json')).read_text('utf-8'))
    assert result['status']==result['core_status']=='complete'
    return result


def test_disabled_ai_never_connects_or_checks_assets(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False;e.c['hosts']['ai']['fingerprint']=''
    class NoAI(FakeRelease):
        def for_role(self,role):
            assert role!='ai'
            return {}
    e.release=NoAI()
    e.run('update')
    assert not any(role=='ai' for role,_,_ in FakeRemote.history)
    assert report(tmp_path)['ai']['status']=='disabled'
    assert not (tmp_path/'ai-journal.json').exists()


@pytest.mark.parametrize('failure',[('ai','preflight'),('ai','claim'),('ai','bootstrap'),('ai','backup'),('db','ai-documents'),('ai','install'),('app','verify-ai'),('ai','commit')])
def test_ai_failures_leave_core_complete_and_retry_without_core_work(tmp_path,failure):
    e=engine(tmp_path);FakeRemote.fail=failure;e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'
    state=json.loads((tmp_path/'ai-journal.json').read_text('utf-8'))
    assert state['status']=='failed'
    FakeRemote.history=[];FakeRemote.fail=None
    assert e.run('ai')['status']=='complete'
    assert all(role=='ai' or (role,action) in (('app','verify-ai'),('db','ai-documents')) for role,action,_ in FakeRemote.history)
    assert all(op==state['operation'] for _,_,op in FakeRemote.history)
    assert json.loads((tmp_path/'ai-journal.json').read_text('utf-8'))['status']=='complete'
    assert json.loads((tmp_path/'journal.json').read_text('utf-8'))['status']=='complete'
    assert report(tmp_path)['ai']['status']=='complete'


def test_ai_connection_is_after_all_core_commits(tmp_path):
    e=engine(tmp_path)
    class Observe(FakeRemote):
        def __init__(self,role,*args):
            if role=='ai':
                assert report(tmp_path)['core_status']=='complete'
                assert all(any(r==core and action=='commit' for r,action,_ in self.history) for core in CORE_ROLES)
            super().__init__(role,*args)
    e.factory=Observe;e.run('update')
    assert report(tmp_path)['ai']['status']=='complete'
    for mode in ('install','update'):
        p=plan(mode)
        assert p.index(('ai','preflight'))>max(p.index((r,'commit')) for r in CORE_ROLES)


def test_ai_ssh_failure_is_warning(tmp_path):
    e=engine(tmp_path)
    class Offline(FakeRemote):
        def probe(self):
            if self.role=='ai':raise TimeoutError('AI SSH timeout')
            return super().probe()
    e.factory=Offline;e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'


def test_core_failure_does_not_attempt_ai(tmp_path):
    e=engine(tmp_path);FakeRemote.fail=('app','migrate')
    with pytest.raises(RuntimeError):e.run('update')
    assert not any(r=='ai' for r,_,_ in FakeRemote.history)


def test_invalid_ai_fingerprint_does_not_block_core(tmp_path):
    e=engine(tmp_path);e.c['hosts']['ai']['fingerprint']='';e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'
    assert not any(r=='ai' for r,_,_ in FakeRemote.history)


def test_unexpected_optional_exception_does_not_revert_core(tmp_path,monkeypatch):
    e=engine(tmp_path)
    def fail(*a):raise RuntimeError('unexpected optional exception')
    monkeypatch.setattr(e,'_optional_ai',fail);e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'


def test_bad_optional_artifact_is_deferred(tmp_path):
    e=engine(tmp_path)
    class BadAI(FakeRelease):
        def for_role(self,role):
            if role=='ai':raise ValueError('Corrupt optional asset')
            return {}
    e.release=BadAI();e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'


def test_pending_ai_cannot_switch_release_but_core_can(tmp_path):
    e=engine(tmp_path);FakeRemote.fail=('ai','install');e.run('update')
    old=json.loads((tmp_path/'ai-journal.json').read_text('utf-8'))
    FakeRemote.fail=None;e.release.digest='another-verified-release';e.run('update')
    assert report(tmp_path)['ai']['status']=='warning'
    assert json.loads((tmp_path/'ai-journal.json').read_text('utf-8'))==old


def test_legacy_pending_ai_survives_finishing_core_with_ai_disabled(tmp_path):
    from prodcast_manager.config import atomic_json
    e=engine(tmp_path);e.c['install_ai']=False
    atomic_json(tmp_path/'journal.json',{'status':'failed','operation':'legacy-operation','mode':'update','manifest':e.release.digest,'phase':'applying','steps':[{'role':'ai','action':'claim','result':{}}],'inflight':{'role':'ai','action':'install'}})
    e.run('update')
    assert report(tmp_path)['ai']['status']=='disabled'
    pending=json.loads((tmp_path/'ai-journal.json').read_text('utf-8'))
    assert pending['operation']=='legacy-operation' and pending['inflight']=='install'
    e.c['install_ai']=True;FakeRemote.history=[];e.run('update')
    ai_ops=[op for role,action,op in FakeRemote.history if role=='ai']
    assert ai_ops and set(ai_ops)=={'legacy-operation'}
    assert report(tmp_path)['ai']['status']=='complete'


def test_optional_setting_preserves_legacy_topology():
    c=example();c.pop('install_ai');old=topology_hash(c)
    for enabled in (True,False):
        c['install_ai']=enabled;assert topology_hash(c)==old


def test_missing_optional_key_does_not_prevent_saving_profile(tmp_path):
    c=example();c['hosts']['ai']['key_path']=str(tmp_path/'missing.key')
    assert save_config(tmp_path/'profile',c)['hosts']['ai']['key_path'].endswith('missing.key')


def test_core_probe_and_optional_probe_are_separate():
    import ast
    tree=ast.parse((RESOURCES/'linux.py').read_text('utf-8'))
    functions={f.name:f for f in tree.body if isinstance(f,ast.FunctionDef)}
    core=ast.unparse(functions['verify']);optional=ast.unparse(functions['verify_ai'])
    assert 'PRODCAST_AI_BASE_URL' not in core and '/api/chat' not in core
    assert 'PRODCAST_AI_BASE_URL' in optional and 'current=True' in optional
    assert 'PRODCAST_AI_ENABLED' in ast.unparse(functions['configure_app'])
    assert 'install_ai' in ast.unparse(functions['tls_revision'])


def test_gui_ai_setting_roundtrip_and_icon(tmp_path):
    import tkinter as tk
    from prodcast_manager.gui import App
    root=tk.Tk();root.withdraw()
    try:
        app=App(root,log_dir=tmp_path/'logs',app_home=tmp_path/'manager')
        app.populate(example())
        app.install_ai.set(False)
        assert app.config()['install_ai'] is False
        assert (RESOURCES/'prodcast-manager.ico').read_bytes()[:4]==b'\x00\x00\x01\x00'
    finally:root.destroy()
