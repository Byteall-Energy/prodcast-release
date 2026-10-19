import json
import time
from unittest.mock import Mock
import tkinter as tk
import pytest
from prodcast_manager.gui import App
from prodcast_manager.config import example, atomic_json
from prodcast_manager.portable import save_config, open_credentials, profile_directory, remember


@pytest.fixture
def workspace(tmp_path):
    root = tk.Tk(); root.withdraw()
    app = App(root, app_home=tmp_path / 'portable', log_dir=tmp_path / 'logs')
    yield root, app
    root.destroy()


def test_conditional_credentials_and_windows_have_no_sudo(workspace):
    root, app = workspace
    app.edit_server('worker2')
    app.hostvars['worker2']['auth'].set('password'); app.draw_auth_fields()
    assert len(app.auth_fields.winfo_children()) == 1
    assert 'password' in str(app.secret_vars)
    assert not any('Linux' in str(w.cget('text')) for w in app.connection_editor.winfo_children() if isinstance(w, tk.ttk.LabelFrame))
    app.secret_vars['password'].set('synthetic-password')
    app.capture_access()
    app.hostvars['worker2']['auth'].set('key'); app.draw_auth_fields()
    assert len(app.auth_fields.winfo_children()) == 2
    app.hostvars['worker2']['auth'].set('password'); app.draw_auth_fields()
    assert app.secret_vars['password'].get() == 'synthetic-password'


def test_saved_password_is_loaded_before_any_config_capture(tmp_path):
    home = tmp_path / 'portable'; c = example(); directory = profile_directory(home, c)
    save_config(directory, c); remember(home, directory)
    vault = open_credentials(directory); vault.data['ssh'] = {'app': {'password': 'keep-this-secret'}}; vault.save()
    root = tk.Tk(); root.withdraw()
    try:
        app = App(root, app_home=home, log_dir=tmp_path / 'logs')
        app.config()
        assert app.auth['app']['password'] == 'keep-this-secret'
    finally: root.destroy()


def test_incomplete_draft_has_no_passwords_and_does_not_replace_engine_profile(workspace, monkeypatch):
    _, app = workspace
    monkeypatch.setattr('prodcast_manager.workspace.messagebox.showinfo', Mock())
    app.edit_server('app'); app.secret_vars['password'].set('never-in-draft')
    app.hostvars['app']['address'].set('')
    app.save_draft()
    draft = app.directory / 'ui-draft.json'
    assert draft.exists() and 'never-in-draft' not in draft.read_text('utf-8')
    assert not (app.directory / 'site.json').exists()
    app.vars['site_id'].set('changed'); app.restore_draft()
    assert app.vars['site_id'].get() == 'prodcast-production'


def test_update_keep_ai_omits_packages_for_captured_start_only(workspace):
    _, app = workspace
    app.ai_model.set('models.zip'); app.ai_ollama.set('runtime.zip'); app.deploy_mode.set('update')
    seen = []
    app.start = lambda mode: seen.append((mode, app.ai_model.get(), app.ai_ollama.get()))
    app.run_reviewed()
    assert seen == [('update', '', '')]
    assert (app.ai_model.get(), app.ai_ollama.get()) == ('models.zip', 'runtime.zip')


def test_operation_opens_and_records_core_logs_and_filters(workspace):
    root, app = workspace
    app.busy = True
    app.launch_operation('update', lambda: (app.log('app: migrate'), app.log('db: backup')))
    for _ in range(100):
        app.poll(); root.update()
        if not app.busy: break
        time.sleep(.01)
    assert not app.busy
    assert app.current_page == 'operations'
    item = app.history.list(app.directory)[0]
    assert item['mode'] == 'update' and item['status'] == 'finished'
    app.log_target.set('app'); app.refresh_log()
    text = app.output.get('1.0', 'end')
    assert 'app: migrate' in text and 'db: backup' not in text
    assert app.output.cget('state') == 'disabled'


def test_installed_core_identity_cannot_silently_change_site(workspace):
    _, app = workspace
    c = example(); save_config(app.directory, c)
    vault = open_credentials(app.directory); vault.data['topology'] = 'installed'; vault.save()
    app.hostvars['app']['address'].set('192.168.31.151')
    with pytest.raises(ValueError, match='installed|установленный'): app.prepare_profile()


def test_root_selection_requires_root_username(workspace):
    _, app = workspace
    app.edit_server('app'); app.sudo_mode.set('root')
    with pytest.raises(ValueError, match='root'): app.config()


def test_all_pages_available_and_admin_does_not_lock_with_operations(workspace):
    _, app = workspace
    assert set(app.pages) == {'overview', 'servers', 'deploy', 'ai', 'settings', 'backups', 'operations'}
    for name in app.pages: app.show_page(name)
    app.lock_editors(True)
    assert str(app.target_selector.cget('state')) == 'readonly'
    app.lock_editors(False)


def test_deployment_receipt_marks_later_settings_changes_without_exposing_credentials(workspace):
    from types import SimpleNamespace
    _, app = workspace
    config = save_config(app.directory, example())
    vault = open_credentials(app.directory)
    vault.data['directory'] = {'enabled': False, 'bind_password': 'not-in-public-receipt'}
    vault.save()
    atomic_json(app.directory / 'journal.json', {'status': 'complete', 'operation': 'test'})
    app.record_applied('update', config, vault, SimpleNamespace(version='v0.6.6', digest='digest'), app.directory)
    receipt = (app.directory / 'ui-deployed.json').read_text('utf-8')
    assert 'not-in-public-receipt' not in receipt
    app.refresh_overview()
    assert 'совпадают' in app.overview_text.get() or 'match' in app.overview_text.get()
    vault.data['directory']['enabled'] = True; vault.save()
    app.refresh_overview()
    assert 'отличаются' in app.overview_text.get() or 'differ' in app.overview_text.get()


def test_server_check_does_not_require_other_hosts_or_stage_an_agent(workspace, monkeypatch):
    root, app = workspace
    app.edit_server('worker2')
    app.hostvars['worker2']['address'].set('192.168.31.155')
    app.hostvars['worker2']['fingerprint'].set('SHA256:' + 'a' * 43)
    app.hostvars['db']['address'].set('unconfigured')
    remote = Mock(); remote.probe.return_value = {'hostname': 'WORKER2'}
    monkeypatch.setattr('prodcast_manager.ssh.Remote', Mock(return_value=remote))
    app.check_server()
    for _ in range(100):
        app.poll(); root.update()
        if not app.busy: break
        time.sleep(.01)
    remote.probe.assert_called_once(); remote.close.assert_called_once()
    remote.prepare_stage.assert_not_called()
    assert 'WORKER2' in app.output.get('1.0', 'end')


def test_ldap_form_transport_controls_and_result_invalidation(workspace):
    from prodcast_manager.directory_dialog import DirectoryDialog
    root, app = workspace
    dialog = DirectoryDialog(root, {}, lambda value: None, lambda *args: {'ok': True})
    try:
        dialog.withdraw()
        assert dialog.values['url'].get() == 'ldaps://:636'
        dialog.test_results['connection'].set('previous result')
        dialog.values['domain'].set('example.com')
        assert 'previous result' != dialog.test_results['connection'].get()
        assert str(dialog.lookup.get()) == 'userPrincipalName'
    finally: dialog.close()
