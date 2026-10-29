import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from prodcast_manager.session_logs import SessionLog,application_directory
from prodcast_manager.portable import last_directory, data_directory
from prodcast_manager.ssh import Remote

def test_logs_follow_executable_not_extraction_dir(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'Manager.exe'))
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path/'unpack'),raising=False)
    log=SessionLog();log.append('Ошибка AI')
    assert log.directory==tmp_path/'prodcast-data'/'logs'
    assert 'Ошибка AI' in log.path.read_text('utf-8')

def test_old_data_and_logs_migrate_under_prodcast_data(tmp_path,monkeypatch):
    legacy=tmp_path/'data';site=legacy/'sites'/'existing';site.mkdir(parents=True)
    (site/'site.json').write_text('{}',encoding='utf-8')
    (legacy/'manager.json').write_text('{"site":"data/sites/existing"}',encoding='utf-8')
    old_logs=tmp_path/'logs';old_logs.mkdir();(old_logs/'old.log').write_text('preserve',encoding='utf-8')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'Manager.exe'))

    log=SessionLog()

    assert last_directory(tmp_path)==tmp_path/'prodcast-data'/'sites'/'existing'
    assert (log.directory/'old.log').read_text('utf-8')=='preserve'
    assert log.directory==tmp_path/'prodcast-data'/'logs'

def test_migration_keeps_existing_files_and_copies_missing_legacy_entries(tmp_path):
    current=tmp_path/'prodcast-data'/'sites'/'site';current.mkdir(parents=True)
    (current/'site.json').write_text('new',encoding='utf-8')
    legacy=tmp_path/'data'/'sites';(legacy/'site').mkdir(parents=True)
    (legacy/'site'/'site.json').write_text('old',encoding='utf-8')
    (legacy/'site'/'vault.json').write_text('legacy-vault',encoding='utf-8')
    (legacy/'other').mkdir();(legacy/'other'/'keep.txt').write_text('keep',encoding='utf-8')

    data_directory(tmp_path)
    assert (current/'site.json').read_text('utf-8')=='new'
    assert not (current/'vault.json').exists()
    assert (tmp_path/'prodcast-data'/'sites'/'other'/'keep.txt').read_text('utf-8')=='keep'
    assert (legacy/'site'/'site.json').read_text('utf-8')=='old'

def test_secrets_redacted_in_both_local_logs(tmp_path):
    log=SessionLog(tmp_path)
    log.protect({'secrets':{'PW_AI':'db-password-123'},'ssh':{'ai':{'password':'ssh-secret-123'}}})
    raw='Ошибка db-password-123 ssh-secret-123 postgresql://u:unknown@db/x https://host/x?sig=abc\nAPI_TOKEN=foo\nAuthorization: Bearer xyz\n-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----'
    clean=log.append(raw);path=log.remote('ai','install',raw)
    for secret in ('db-password-123','ssh-secret-123','unknown','sig=abc','foo','xyz','private-material'):
        assert secret not in clean and secret not in path.read_text('utf-8')
    assert 'Ошибка' in clean and 'postgresql://' in clean

def test_remote_error_downloads_redacted_diagnostics_and_preserves_failure(tmp_path):
    r=object.__new__(Remote);r.role='ai';r.diagnostics=SessionLog(tmp_path);messages=[];r.log=messages.append
    def fail(*a):raise RuntimeError('Ollama not ready')
    r._action=fail;r.collect_diagnostics=lambda:'secret-value Permission denied'
    with pytest.raises(RuntimeError,match='Ollama not ready'):
        r.action({'secrets':{'PW_AI':'secret-value'}},'install',None)
    path=next(tmp_path.glob('*-ai-install.log'))
    assert 'Permission denied' in path.read_text() and 'secret-value' not in path.read_text()
    assert str(path) in messages[0]
    def unavailable():raise OSError('SSH unavailable')
    r.collect_diagnostics=unavailable
    with pytest.raises(RuntimeError,match='Ollama not ready'):r.action({},'install',None)
    assert 'could not' in messages[-1].lower()


def test_operation_logs_stay_english_with_russian_ui_and_preserve_values(tmp_path):
    from prodcast_manager.i18n import set_language,language,tr
    previous=language()
    try:
        set_language('ru')
        log=SessionLog(tmp_path)
        path=r'C:\Пользователи\тест\prodcast-data\logs\worker.log'
        message='worker1: диагностика сервера сохранена на этом ПК: '+path
        clean=log.append(message)
        assert clean=='worker1: server diagnostics saved on this PC: '+path
        assert language()=='ru' and tr('ОШИБКА: ')=='ОШИБКА: '
        clean=log.append('ОШИБКА: Включите AI на вкладке «AI». Отсутствие пакетов не означает отключение установленного AI.')
        assert clean.startswith('ERROR: Enable AI on the AI tab.')
        set_language('en')
        assert log.append(message)=='worker1: server diagnostics saved on this PC: '+path
        assert path in log.path.read_text('utf-8')
    finally:set_language(previous)


def test_controller_history_and_session_log_share_english_messages(tmp_path):
    from prodcast_manager.desktop_controller import DesktopController
    from prodcast_manager.i18n import set_language,language
    previous=language()
    try:
        controller=DesktopController(tmp_path)
        set_language('ru')
        item=controller.history.begin('backup',controller.directory,('worker1',))
        controller.local.operation=item
        controller.emit('worker1: шаг выполняется, прошло 20 с')
        assert language()=='ru'
        events=controller.history.events(item['id'])
        assert events[-1]['text']=='worker1: step running, elapsed 20 s'
        assert 'worker1: step running, elapsed 20 s' in controller.log.path.read_text('utf-8')
    finally:set_language(previous)


def test_engine_and_transport_messages_are_english_even_for_cli_russian_ui(tmp_path):
    from prodcast_manager import engine,ssh
    from test_manager import engine as make_engine
    from prodcast_manager.i18n import set_language,language
    previous=language()
    try:
        set_language('ru')
        template=make_engine(tmp_path);messages=[]
        instance=engine.Engine(template.c,template.vault,template.release,tmp_path,log=messages.append)
        instance.log('worker1: шаг выполняется, прошло 20 с')
        assert messages==['worker1: step running, elapsed 20 s']
        assert engine.tr('ОШИБКА: ')=='ОШИБКА: '
        assert ssh.tr('{v0}: шаг выполняется, прошло {v1} с').format(v0='worker1',v1=20)=='worker1: step running, elapsed 20 s'
        assert language()=='ru'
    finally:set_language(previous)

def test_gui_copy_selection_all_and_cyrillic_shortcuts(tmp_path):
    import tkinter as tk
    from prodcast_manager.gui import App
    root=tk.Tk();root.withdraw();app=App(root,log_dir=tmp_path)
    try:
        app.session_log.protect({'secrets':{'test':'hidden-password'}})
        app.log('AI: ошибка hidden-password');app.poll();root.update()
        app.copy_log(all_text=True)
        assert 'AI: ошибка [REDACTED]' in root.clipboard_get()
        assert 'hidden-password' not in root.clipboard_get()
        app.output.tag_add('sel','1.0','1.6');app.copy_log()
        assert root.clipboard_get()==app.output.get('1.0','1.6')
        app.log_shortcut(SimpleNamespace(keycode=65,keysym='Cyrillic_ef'))
        app.log_shortcut(SimpleNamespace(keycode=67,keysym='Cyrillic_es'))
        assert 'AI: ошибка' in root.clipboard_get()
        assert app.output.cget('state')=='disabled'
    finally:root.destroy()

def test_gui_loads_saved_vm_and_site_addresses(tmp_path):
    import tkinter as tk
    import os
    from prodcast_manager.gui import App
    from prodcast_manager.config import atomic_json,example
    c=example();c['hosts']['app']['address']='192.168.31.23'
    c['public_url']='https://192.168.31.23';c['admin_ip']='192.168.31.225'
    from prodcast_manager.portable import profile_directory,remember
    home=tmp_path/'portable';directory=profile_directory(home,c)
    atomic_json(directory/'site.json',c);remember(home,directory)
    root=tk.Tk();root.withdraw();app=App(root,log_dir=tmp_path/'logs')
    try:
        assert app.hostvars['app']['address'].get()=='192.168.31.23'
        assert app.vars['public_url'].get()==c['public_url']
        assert app.vars['admin_ip'].get()==c['admin_ip']
    finally:root.destroy()
