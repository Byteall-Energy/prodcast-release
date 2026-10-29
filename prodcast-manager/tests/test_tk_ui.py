"""Real Tk workspace controls with synthetic local profiles, no remote calls."""
import json
import sys
import tkinter as tk
from pathlib import Path
from unittest.mock import Mock
import pytest
from prodcast_manager.desktop_controller import DesktopController
from prodcast_manager.tk_ui import DesktopWindow


@pytest.fixture
def window(tmp_path):
    import gc
    gc.collect()  # Dispose prior Tk interpreters on the GUI thread before creating another.
    root=tk.Tk();root.withdraw();w=DesktopWindow(root,DesktopController(tmp_path))
    w.message=Mock();w.text_dialog=Mock();w.confirm=Mock(return_value=True)
    yield w
    if not w.closed:w.destroy()


def type_value(entry,text):entry.delete(0,'end');entry.insert(0,text)


def test_stale_worker_selection_is_removed_before_next_update(window):
    import copy
    from test_desktop_controller import completed_worker_removal,runtime_site
    old,target,stamp=completed_worker_removal(window.controller)
    window.controller.config=runtime_site(window.controller)
    window.controller.membership_stamp=window.controller._membership_stamp()
    window.config=copy.deepcopy(old);window.membership_stamp=stamp;window.role='worker2'
    window.navigate('servers');window.sync()
    assert 'worker2' not in window.config['hosts']
    assert window.role=='worker1'
    assert 'worker2' not in window.server_tree.get_children()
    window.controller._prepare()


def test_worker_service_form_keeps_ssh_password_separate(window):
    window.role='worker1';window.worker_services['worker1']={'mode':'custom','account':'EXAMPLE\\svc'}
    window.navigate('servers')
    assert 'service_mode' in window.fields
    assert 'service_password' in window.fields
    assert window.fields.get('password') is not window.fields['service_password']
    assert window.worker_services['worker1']['account']=='EXAMPLE\\svc'


@pytest.mark.parametrize('language',['en','ru'])
def test_export_success_shows_saved_file_instead_of_raw_json(window,language):
    window.config['language']=language
    window.result({'mode':'export','ok':True,'value':{'status':'complete','archive':'C:/backups/site.pcbackup','bytes':2048}})
    title,text=window.message.call_args.args[:2]
    assert 'site.pcbackup' in text and '2.0 KiB' in text
    assert '[]' not in text
    window.text_dialog.assert_not_called()


def test_server_backup_success_shows_server_paths(window):
    window.result({'mode':'backup','ok':True,'value':{'result':[{'role':'db','action':'backup','result':{'path':'/var/lib/prodcast-manager/backups/test'}}],'pending':[]}})
    assert '/var/lib/prodcast-manager/backups/test' in window.message.call_args.args[1]
    window.text_dialog.assert_not_called()


def test_overview_distinguishes_attention_and_blocking_and_updates_live(window):
    from prodcast_manager.config import atomic_json
    path=window.controller.directory/'maintenance-journal.json'
    atomic_json(path,{'mode':'export','status':'failed','operation':'failed-backup','services_recovered':True,'leases_released':True})
    window.refresh();window.navigate('overview')
    assert window.operation_counts['running'].cget('text')=='0'
    assert window.operation_counts['needs_attention'].cget('text')=='1'
    assert window.operation_counts['blocking'].cget('text')=='0'
    assert 'Does not block' in window.operation_state_tree.item('0','values')[2] or 'Не блокирует' in window.operation_state_tree.item('0','values')[2]
    atomic_json(path,{'mode':'export','status':'failed','operation':'failed-backup','services_recovered':False,'leases_released':True,'quiesced':True})
    window.poll()
    assert window.operation_counts['blocking'].cget('text')=='1'
    window.navigate('operations');window.inspect_operation_state()
    assert 'maintenance-journal.json' in window.text_dialog.call_args.args[1]


def test_preview_sections_and_settings_without_qt(window):
    for key,_,_ in window.sections:window.navigate(key)
    for key in ('general','https','directory','activation'):
        window.navigate('settings');window.settings_tab(key)
    assert len(window.nav)==7
    assert not any(name.startswith(('PySide6','shiboken6')) for name in sys.modules)
    assert hasattr(window.root,'_prodcast_icon')


def test_hints_are_not_values_and_access_save_is_encrypted(window):
    window.navigate('servers');assert window.fields['address'].get()==''
    assert window.fields['address'].example=='192.0.2.10'
    window.config['hosts']['app']['auth']='password';window.render()
    assert window.fields['password'].cget('show')
    assert 'key_path' not in window.fields
    type_value(window.fields['password'],'synthetic-tk-secret');window.save_access()
    assert window.controller._read_vault()['ssh']['app']['password']=='synthetic-tk-secret'
    assert window.fields['password'].get()==''
    window.controller.save_draft()
    assert 'synthetic-tk-secret' not in (window.controller.directory/'ui-draft.json').read_text('utf-8')


def test_server_overview_keeps_all_addresses_visible_and_live(window):
    for n,host in enumerate(window.config['hosts'].values(),10):host['address']='10.20.30.'+str(n)
    window.navigate('servers')
    assert len(window.server_tree.get_children())==len(window.config['hosts'])
    type_value(window.fields['address'],'10.20.30.99')
    assert window.server_tree.item('app','values')[0]=='10.20.30.99'
    window.server_tree.selection_set('worker2');window.select_server()
    assert window.role=='worker2'
    window.server_tree.selection_set('app');window.select_server()
    assert window.fields['address'].get()=='10.20.30.99'


def test_worker_actions_always_present_with_explanation(window):
    window.navigate('servers')
    assert set(window.worker_buttons)=={'add','install','repair','remove'}
    assert str(window.worker_buttons['add'].cget('state'))=='normal'
    assert str(window.worker_buttons['remove'].cget('state'))=='disabled'


def test_worker_count_and_auth_do_not_change_on_scroll(window):
    window.navigate('servers');count=window.worker_count.get();method=window.config['hosts']['app']['auth']
    window.fields['auth'].event_generate('<MouseWheel>',delta=-120)
    window.root.update()
    assert window.config['hosts']['app']['auth']==method and window.worker_count.get()==count
    assert not any(w.winfo_class() in ('TSpinbox','Spinbox') for w in window.root.winfo_children())


def test_deployment_has_four_preview_steps_and_keeps_values(window):
    window.navigate('deploy');type_value(window.fields['site_id'],'new-site')
    window.wizard_step(1);assert 'release' in window.fields
    window.wizard_step(1);assert 'ai_action' in window.fields
    window.wizard_step(1);assert window.step==3
    window.wizard_step(-3);assert window.fields['site_id'].get()=='new-site'


@pytest.mark.parametrize('scheme',['ldap','ldaps'])
def test_optional_search_account_survives_in_both_transports(window,scheme):
    window.directory.update(enabled=True,url=scheme+'://dc.example.com',allow_plain_ldap=True,
        domain='example.com',base_dn='DC=example,DC=com',group_dn='CN=Users,DC=example,DC=com',bind_dn='',bind_password='')
    window.navigate('settings');window.settings_tab('directory');window.save_directory()
    saved=window.controller._read_vault()['directory']
    assert saved['bind_dn']==saved['bind_password']==''
    assert saved['url'].startswith(scheme+'://')
    assert ('allow_plain_ldap' in window.fields)==(scheme=='ldap')


def test_clearing_search_account_removes_saved_secret(window):
    window.directory.update(enabled=True,url='ldaps://dc.example.com',domain='example.com',
        base_dn='DC=example,DC=com',group_dn='CN=Users,DC=example,DC=com',bind_dn='svc@example.com',bind_password='old-search-secret')
    window.navigate('settings');window.settings_tab('directory');window.save_directory()
    type_value(window.fields['bind_dn'],'');window.save_directory()
    assert window.controller._read_vault()['directory']['bind_password']==''


def test_test_user_password_is_ephemeral(window,monkeypatch):
    window.navigate('settings');window.settings_tab('directory')
    window.directory.update(test_user='person',test_password='temporary-test-secret')
    start=Mock();monkeypatch.setattr(window,'start',start);window.probe('user')
    payload=start.call_args.args[1]
    assert payload['password']=='temporary-test-secret' and 'test_password' not in payload['directory']
    assert not window.directory.get('test_password') and window.fields['test_password'].get()==''


def test_busy_locks_mutations_but_keeps_admin_and_navigation(window):
    window.controller.busy=True;window.state=window.controller.snapshot();window.navigate('servers')
    assert str(window.fields['address'].cget('state'))=='disabled'
    assert str(window.admin.cget('state'))!='disabled'
    assert all(str(b.cget('state'))!='disabled' for b in window.nav.values())
    window.controller.busy=False


def test_gpu_driver_control_remains_absent(window):
    window.navigate('ai');assert 'install_gpu_driver' not in window.fields
    assert 'install_ai' in window.fields


def test_linux_privilege_choice_is_conditional_and_root_is_not_assumed(window):
    window.navigate('servers');assert 'mode' in window.fields and 'sudo_password' not in window.fields
    choice=window.fields['mode'];choice.current(2);choice.event_generate('<<ComboboxSelected>>')
    assert 'sudo_password' in window.fields
    choice=window.fields['mode'];choice.current(0);choice.event_generate('<<ComboboxSelected>>')
    with pytest.raises(ValueError,match='root'):window.sync()
    window.role='worker1';window.render();assert 'mode' not in window.fields


def test_cancelled_worker_remove_has_no_dispatch(window,monkeypatch):
    window.confirm.return_value=False;start=Mock();monkeypatch.setattr(window,'start',start)
    window.worker_action('worker-remove');start.assert_not_called()


def test_fast_completion_displays_once(window,monkeypatch):
    result=Mock();monkeypatch.setattr(window,'result',result)
    window.controller.last_result={'operation':'instant','mode':'status','ok':True,'value':{}}
    window.poll();window.poll();assert result.call_count==1


def test_core_logs_and_target_filter(window):
    c=window.controller;item=c.history.begin('update',c.directory,('app','db'),'')
    c.history.append(item,'app: core update started');c.history.append(item,'worker1: repair started');c.history.finish(item)
    window.state=c.snapshot();window.navigate('operations')
    assert 'core update started' in window.log_view.get('1.0','end')
    window.target_value.set('app');window.update_logs()
    assert 'worker1: repair' not in window.log_view.get('1.0','end')


def test_qt_and_browser_dependencies_are_removed():
    root=Path(__file__).resolve().parents[1]
    for name in ('requirements.txt','requirements-build.lock.txt'):
        text=(root/name).read_text('utf-8');assert 'PySide6' not in text and 'shiboken6' not in text
    assert 'from .tk_ui import main' in (root/'prodcast_manager/__main__.py').read_text('utf-8')


def test_legacy_vault_prompt_retries_only_after_unlock(window,monkeypatch):
    from prodcast_manager.portable import LegacyPasswordRequired
    from prodcast_manager.tk_ui import simpledialog
    action=Mock(side_effect=[LegacyPasswordRequired('old vault'),{'saved':True}])
    unlock=Mock();monkeypatch.setattr(window.controller,'unlock_legacy',unlock)
    monkeypatch.setattr(simpledialog,'askstring',lambda *args,**kwargs:'synthetic-old-vault')
    assert window.guard(action)=={'saved':True}
    unlock.assert_called_once_with({'password':'synthetic-old-vault'})


def test_frozen_dependency_guard_requires_tk_and_rejects_qt():
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'release/check_tk_ui.py'
    spec=importlib.util.spec_from_file_location('check_tk_ui_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.verify_archive(['_tkinter.pyd','tcl86t.dll','tk86t.dll'])
    with pytest.raises(RuntimeError):module.verify_archive(['_tkinter.pyd','PySide6/Qt6Core.dll'])
    with pytest.raises(RuntimeError):module.verify_archive(['python311.dll'])
