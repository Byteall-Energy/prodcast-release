import json
import shutil
import tkinter as tk
import pytest
from cryptography.fernet import InvalidToken
from prodcast_manager.config import example, atomic_json
from prodcast_manager.portable import (open_credentials, import_site, save_config,
    runtime_config, profile_directory, LegacyPasswordRequired, remember, last_directory,
    data_directory)
from prodcast_manager.vault import Vault


def test_passwordless_credentials_reopen_and_move(tmp_path):
    first=tmp_path/'first';v=open_credentials(first)
    v.data={'installation_id':'keep-id','secrets':{'ADMIN_PASSWORD':'keep-password'}};v.save()
    shutil.copytree(first,tmp_path/'moved')
    assert open_credentials(tmp_path/'moved').data==v.data
    assert 'keep-password' not in (first/'secrets.json').read_text()
    (tmp_path/'moved/secrets.key').unlink()
    with pytest.raises(ValueError,match='secrets.key'):open_credentials(tmp_path/'moved')
    assert not (tmp_path/'moved/secrets.key').exists()


def test_legacy_import_preserves_original_and_requires_password_once(tmp_path):
    source=tmp_path/'old';source.mkdir();c=example();atomic_json(source/'site.json',c)
    v=Vault(source/'vault.json','original-password');v.data={'installation_id':'original','ssh':{},'secrets':{'x':'secret'}};v.save()
    original=v.path.read_bytes();atomic_json(source/'journal.json',{'status':'failed','steps':['db']})
    dest,_=import_site(source/'site.json',tmp_path/'manager')
    with pytest.raises(LegacyPasswordRequired):open_credentials(dest)
    with pytest.raises(InvalidToken):open_credentials(dest,'incorrect-password')
    assert not (dest/'secrets.key').exists()
    assert open_credentials(dest,'original-password').data==v.data
    assert open_credentials(dest).data==v.data
    assert v.path.read_bytes()==original==(dest/'vault.json').read_bytes()
    assert json.loads((dest/'journal.json').read_text())['steps']==['db']


def test_relative_ssh_keys_and_last_site_survive_move(tmp_path):
    c=example();key=tmp_path/'input.key';key.write_text('synthetic-key')
    for host in c['hosts'].values():host['key_path']=str(key)
    home=tmp_path/'manager';folder=profile_directory(home,c)
    save_config(folder,c);remember(home,folder)
    stored=json.loads((folder/'site.json').read_text())
    assert all(h['key_path'].startswith('ssh/') for h in stored['hosts'].values())
    moved=tmp_path/'new-location';shutil.copytree(home,moved);key.unlink()
    loaded=runtime_config(last_directory(moved)/'site.json')
    assert all(Path(h['key_path']).read_text()=='synthetic-key' for h in loaded['hosts'].values())
    assert Path(json.loads((data_directory(home)/'manager.json').read_text())['site']).parts[0]=='sites'


from pathlib import Path

def test_gui_changing_vm_addresses_creates_separate_credentials(tmp_path):
    from prodcast_manager.gui import App
    root=tk.Tk();root.withdraw();app=App(root,log_dir=tmp_path/'logs',app_home=tmp_path/'manager')
    try:
        app.populate(example())
        first=app.vault();first.data['installation_id']='production';first.save()
        old=app.directory;original=(old/'secrets.json').read_bytes()
        app.hostvars['app']['address'].set('192.168.31.141')
        second=app.vault()
        assert app.directory!=old and 'installation_id' not in second.data
        assert (old/'secrets.json').read_bytes()==original
        assert app.vars['public_url'].get()=='https://192.168.31.141'
    finally:root.destroy()
