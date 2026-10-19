import copy,json,re,ast
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import pytest
from cryptography import x509
from test_manager import engine,FakeRemote
from test_remote_configuration import agent
from prodcast_manager.config import example,validate,topology_hash,worker_roles
from prodcast_manager.portable import runtime_config,profile_directory,save_config
from prodcast_manager.vault import Vault
from prodcast_manager.engine import plan,RESOURCES
from prodcast_manager.i18n import language,set_language,tr


def workers(c,count):
    template=copy.deepcopy(c['hosts']['worker1'])
    c['hosts']={r:h for r,h in c['hosts'].items() if not r.startswith('worker')}
    for n in range(1,count+1):c['hosts']['worker'+str(n)]={**template,'address':'192.168.31.'+str(150+n)}
    return c


def test_disabled_ai_allows_empty_fields_and_keeps_identity(tmp_path):
    c=example();c['install_ai']=False;c['hosts']['ai'].update(address='',port='',username='',auth='',key_path='')
    validate(c);before=topology_hash(c);folder=profile_directory(tmp_path,c)
    c['hosts']['ai'].update(address='192.168.31.143',port=22,username='ubuntu',auth='key');c['install_ai']=True
    validate(c)
    assert topology_hash(c)==before
    assert profile_directory(tmp_path,c)==folder
    c['hosts']['ai']['address']=''
    with pytest.raises(ValueError):validate(c)


def test_legacy_profile_enables_ai_without_replacing_identity_or_core_keys(tmp_path):
    c=example();c.pop('_ai_topology_address');c['install_ai']=False
    v=Vault(tmp_path/'secrets.json','test-password-long');v.initialize(c)
    before=copy.deepcopy(v.data)
    (tmp_path/'site.json').write_text(json.dumps(c))
    new=runtime_config(tmp_path/'site.json');new['install_ai']=True;new['hosts']['ai']['address']='192.168.31.143'
    v.initialize(new)
    assert v.data['installation_id']==before['installation_id']
    assert v.data['secrets']==before['secrets']
    assert topology_hash(new)==before['topology']
    for name in before['tls']:
        if name not in ('ai.crt','ai.key'):assert v.data['tls'][name]==before['tls'][name]
    leaf=x509.load_pem_x509_certificate(v.data['tls']['ai.crt'].encode())
    assert '192.168.31.143' in [str(x) for x in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)]
    ca=x509.load_pem_x509_certificate(v.data['tls']['ca.crt'].encode());leaf.verify_directly_issued_by(ca)
    new['hosts']['db']['address']='192.168.31.199'
    with pytest.raises(ValueError,match='Topology differs'):v.initialize(new)


@pytest.mark.parametrize('count',[1,3,16])
def test_install_without_ai_then_update_same_release_with_ai(tmp_path,count):
    e=engine(tmp_path);workers(e.c,count);e.c['install_ai']=False;e.c['hosts']['ai']['address']=''
    FakeRemote.installed=False;e.run('install')
    first=json.loads((tmp_path/'journal.json').read_text());assert first['status']=='complete'
    original=topology_hash(e.c);FakeRemote.history.clear();FakeRemote.installed=True
    e.c['install_ai']=True;e.c['hosts']['ai']['address']='192.168.31.143'
    class LaterAI(FakeRemote):
        def action(self,p,action,resources):
            if self.role=='ai':
                if action=='preflight':return {'managed':False,'version':''}
                assert p['mode']=='install'
            return super().action(p,action,resources)
    e.factory=LaterAI;e.run('update')
    second=json.loads((tmp_path/'journal.json').read_text());assert second['status']=='complete'
    assert first['manifest']==second['manifest'] and first['operation']!=second['operation']
    assert topology_hash(e.c)==original
    assert json.loads((tmp_path/'ai-journal.json').read_text())['status']=='complete'
    for role in worker_roles(e.c):assert any(r==role and a=='install' for r,a,_ in FakeRemote.history)
    assert any(r=='app' and a=='configure' for r,a,_ in FakeRemote.history)


@pytest.mark.parametrize('count',[1,3,16])
def test_worker_plan_and_secrets_are_scoped(tmp_path,count):
    e=engine(tmp_path);workers(e.c,count);validate(e.c)
    v=Vault(tmp_path/'vault.json','test-password-long');v.initialize(e.c);e.vault=v
    for i,role in enumerate(worker_roles(e.c),1):
        payload=e.payload(role,'op','install');n=f'{i:02}'
        assert set(payload['secrets'])=={'PW_W'+n,'REDIS_W'+n,'SVC_W'+n,'MEDIA_KEY','SIGNING_KEY'}
        assert payload['secrets']['SVC_W'+n].startswith('Pc!9')
        assert 'PG_ADMIN' not in payload['secrets']
    p=plan('update',False,worker_roles(e.c))
    assert len([1 for r,a in p if r.startswith('worker') and a=='install'])==count
    assert max(p.index((r,'stop')) for r in worker_roles(e.c))<p.index(('app','stop'))


@pytest.mark.parametrize('count',[1,3,16])
def test_db_rules_grants_and_app_verification_use_selected_workers(agent,monkeypatch,count):
    m=agent;workers(m.C,count);m.C['install_ai']=False;m.C['hosts']['ai']['address']=''
    for n in range(1,count+1):
        for prefix in ('PW_W','REDIS_W'):m.S[prefix+f'{n:02}']='a'*64
    monkeypatch.setattr(m,'run',lambda args,**kw:'999\n' if 'id' in args else '')
    monkeypatch.setattr(m,'firewall',lambda role:None)
    sql=[];monkeypatch.setattr(m,'psql',lambda text,db='postgres':sql.append(text) or '')
    monkeypatch.setattr(m,'dc',lambda *a,**kw:'PONG')
    m.install_db();hba=(m.ROOT/'db/config/pg_hba.conf').read_text();acl=(m.ROOT/'db/config/users.acl').read_text()
    assert 'prodcast_ai prodcast_ai' not in hba
    for n in range(1,count+1):
        assert f'prodcast_worker_{n:02} 192.168.31.{150+n}/32' in hba
        assert f'user prodcast_worker_{n:02}_scheduler ' in acl
        assert f'worker{n:02}.*' in acl
    grants=m.worker_grants()
    assert set(re.findall(r'prodcast_worker_\d+',grants))=={f'prodcast_worker_{n:02}' for n in range(1,count+1)}
    captured=[];monkeypatch.setattr(m,'app_python',lambda code:captured.append(code))
    monkeypatch.setattr(m,'request',lambda *a,**kw:None)
    assert m.verify()['workers']==count
    for n in range(1,count+1):assert f'prodcast-worker-{n:02}@' in captured[0]


def texts(widget):
    found=[]
    if isinstance(widget,(ttk.Label,ttk.Button,ttk.Checkbutton)):found.append(widget.cget('text'))
    if isinstance(widget,ttk.Notebook):found += [widget.tab(t,'text') for t in widget.tabs()]
    for child in widget.winfo_children():found+=texts(child)
    return found


def test_gui_language_dynamic_workers_and_optional_ai_preserve_input(tmp_path):
    from prodcast_manager.gui import App
    from prodcast_manager.activation_dialog import ActivationDialog
    root=tk.Tk();root.withdraw()
    try:
        app=App(root,log_dir=tmp_path/'logs',app_home=tmp_path/'manager')
        assert all(not h['address'].get() for h in app.hostvars.values()) and app.install_ai.get() is False
        configured=example();configured['install_ai']=False;configured['hosts']['ai']['address']=''
        app.populate(configured)
        # AI package selection is now independent from including AI in the site.
        assert 'ai' in app.pages and app.ai_update.get()=='keep'
        app.ai_ollama.set('C:/offline/ollama.zip')
        app.ai_model.set('C:/offline/model.gguf')
        app.worker_count.set('3');app.hostvars['worker3']['address'].set('192.168.31.153')
        assert len(worker_roles(app.config()))==3
        app.worker_count.set('1');assert len(worker_roles(app.config()))==1
        app.worker_count.set('3');assert app.hostvars['worker3']['address'].get()=='192.168.31.153'
        app.language_choice.set('English');app.change_language()
        content=texts(root)
        assert 'Include AI in this site' in content and 'Install / update' in content and 'Operations' in content
        assert not any(re.search('[А-Яа-я]',s) for s in content if s!='Language / Язык')
        assert app.hostvars['worker3']['address'].get()=='192.168.31.153'
        assert json.loads((tmp_path/'manager/prodcast-data/ui.json').read_text())['language']=='en'
        app.install_ai.set(True)
        assert app.ai_ollama.get()=='C:/offline/ollama.zip'
        assert app.ai_model.get()=='C:/offline/model.gguf'
        assert not any('v0.2' in s for s in texts(root))
        app.hostvars['ai']['address'].set('192.168.31.143');assert app.config()['install_ai'] is True
        dialog=ActivationDialog(root,'00000000-0000-0000-0000-000000000001',{},'company',lambda v:None)
        assert dialog.title()=='App activation connection';dialog.destroy()
        app.language_choice.set('Русский');app.change_language()
        assert 'Включить AI в этой площадке' in texts(root) and 'Операции' in texts(root)
        assert not hasattr(app,'gpu')
        app.populate(example());app.worker_count.set('3')
        assert app.hostvars['worker3']['address'].get()==''
    finally:root.destroy();set_language('ru')


def test_english_diagnostics_preserve_values():
    try:
        set_language('en')
        assert tr('{v0}: проверка SSH и прав').format(v0='worker16')=='worker16: checking SSH and permissions'
    finally:set_language('ru')


def test_worker_adapter_supports_all_numbers_before_importing_payload(tmp_path,monkeypatch):
    source=(RESOURCES/'worker-service-runner.py').read_text('utf-8').split('postgres_user =')[0]
    monkeypatch.setenv('PRODCAST_WORKER_HOME',str(tmp_path))
    for number in ('01','02','03','16','00','17'):
        c={k:'example.test' for k in ('postgres_host','postgres_db','postgres_user','redis_host','redis_user')}
        c.update(main_server_url='https://example.test',worker_number=number)
        (tmp_path/'site.json').write_text(json.dumps(c))
        if number in ('00','17'):
            with pytest.raises(RuntimeError,match='Unknown worker'):exec(source,{})
        else:exec(source,{})


def test_release_rejects_unknown_multi_worker_asset():
    from prodcast_manager.release import Release
    r=object.__new__(Release);r.files={'worker-windows-amd64':Path('worker.zip')}
    r.assets={'worker.zip':{'sha256':'unknown'}};r.doc={}
    r.validate_worker_count(2)
    with pytest.raises(ValueError,match='only two Workers'):r.validate_worker_count(3)
    r.assets['worker.zip']['sha256']='68a8ca761fe02fe0c8899b8e05823c787f661cba7ccd59ba83c8a7427014cd3e'
    r.validate_worker_count(16)
    r.assets['worker.zip']['sha256']='future';r.doc={'management':{'maximum_workers':4}}
    r.validate_worker_count(4)
    with pytest.raises(ValueError):r.validate_worker_count(5)


def test_ai_resume_cannot_silently_change_vm(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=True
    journal={'status':'failed','operation':'old-op','manifest':e.release.digest,
             'ai_address':'192.168.31.199','steps':[{'action':'claim'}]}
    path=tmp_path/'ai-journal.json';path.write_text(json.dumps(journal))
    FakeRemote.history.clear()
    result=e._optional_ai('update','new-op')
    assert result['status']=='warning' and 'original VM address' in result['error']
    assert json.loads(path.read_text())==journal
    assert not FakeRemote.history


def test_progress_translation_uses_selected_language():
    from prodcast_manager.ssh import ProgressLines
    messages=[]
    try:
        set_language('en');ProgressLines(messages.append).feed(b'MANAGER_PROGRESS:ai-model-download\n')
        assert messages and not re.search('[А-Яа-я]',messages[0])
    finally:set_language('ru')


def test_disabled_ai_retains_available_key_in_portable_profile(tmp_path):
    c=example();c['install_ai']=False;c['hosts']['ai']['address']=''
    key=tmp_path/'outside.key';key.write_text('synthetic SSH key')
    c['hosts']['ai']['key_path']=str(key)
    saved=save_config(tmp_path/'profile',c)
    copied=Path(saved['hosts']['ai']['key_path'])
    assert copied.is_relative_to(tmp_path/'profile') and copied.read_bytes()==key.read_bytes()
    c['hosts']['ai']['key_path']=str(tmp_path/'missing.key')
    save_config(tmp_path/'profile2',c)  # Missing optional credentials never block core setup.
