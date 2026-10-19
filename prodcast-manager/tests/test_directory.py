import importlib.util
import json
import socket
import ssl
import sys
import threading
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from prodcast_manager.config import example, topology_hash
from prodcast_manager.directory import RESOURCES, validate_directory, read_ca, save_directory, probe_directory
from prodcast_manager.directory_dialog import DirectoryDialog, NOTICE, ERRORS
from prodcast_manager.i18n import set_language, tr
from prodcast_manager.session_logs import SessionLog
from prodcast_manager.vault import make_pki

sys.path[:0] = [str(p) for p in (RESOURCES/'ldap_deps').glob('*.whl')]
spec=importlib.util.spec_from_file_location('directory_probe_test',RESOURCES/'directory_probe.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


def settings():
    return dict(url='ldaps://dc01.example.com:636',domain='example.com',base_dn='DC=example,DC=com',
                group_dn='CN=ProdCast-Users,OU=Groups,DC=example,DC=com',bind_dn='svc@example.com',bind_password='S3rvice-test!',ca_pem='')


@pytest.fixture(scope='module')
def pki():
    c=example();c['public_url']='https://localhost';return make_pki(c)


@pytest.mark.parametrize('url',['ldap://dc01.example.com','ldaps://192.168.31.250','https://dc01.example.com','ldaps://user:pass@dc.test','ldaps://dc.test/base','ldaps://dc.test?x=1','ldaps://dc.test:0','ldaps://dc.test:65536','ldaps://dc..test','ldaps://-dc.test'])
def test_reject_unsafe_endpoints(url):
    with pytest.raises(ValueError):validate_directory(dict(settings(),url=url))


def test_saved_value_excludes_ephemeral_password_and_preserves_identity():
    c=example();before=topology_hash(c)
    value=dict(settings(),test_password='DO-NOT-SAVE',username='test')
    vault=SimpleNamespace(data={'installation_id':'existing','topology':before,'activation':{'unchanged':True}},save=lambda:None)
    save_directory(vault,value)
    assert vault.data['installation_id']=='existing' and vault.data['topology']==topology_hash(c)
    assert 'DO-NOT-SAVE' not in json.dumps(vault.data)
    assert vault.data['directory']['bind_password']=='S3rvice-test!'
    assert vault.data['activation']=={'unchanged':True}


def test_connection_probe_does_not_require_group_or_account():
    assert validate_directory({'url':'ldaps://DC01.example.com'},True)['url']=='ldaps://dc01.example.com:636'
    with pytest.raises(ValueError):validate_directory({'url':'ldaps://dc01.example.com'})


@pytest.mark.parametrize('scheme',['ldap','ldaps'])
def test_search_account_is_optional_for_both_transports(scheme):
    from prodcast_manager.directory import app_directory
    value=dict(settings(),url=scheme+'://dc01.example.com',allow_plain_ldap=True,
               bind_dn='',bind_password='',enabled=True)
    assert validate_directory(value)['bind_dn']==''
    deployed=app_directory(value)
    assert deployed['mode']==scheme and deployed['group_filter_enabled']
    assert deployed['user_dn_template']=='%(user)s@example.com'


@pytest.mark.parametrize('missing',['bind_dn','bind_password'])
def test_partial_search_credentials_are_rejected(missing):
    with pytest.raises(ValueError,match='both search account'):
        validate_directory(dict(settings(),**{missing:''}))


def test_ca_accepts_der_and_pem_rejects_leaf_and_private_key(pki):
    ca=x509.load_pem_x509_certificate(pki['ca.crt'].encode())
    assert read_ca(ca.public_bytes(serialization.Encoding.DER))==pki['ca.crt']
    assert read_ca(pki['ca.crt'].encode())==pki['ca.crt']
    for material in (pki['site.crt'],pki['ca.key'],pki['ca.crt']+pki['ca.key'],'invalid'):
        with pytest.raises(ValueError):read_ca(material.encode())


def test_directory_passwords_are_redacted(tmp_path):
    log=SessionLog(tmp_path);log.protect({'directory':settings(),'directory_test_password':'Ephemeral-secret'})
    assert 'S3rvice-test!' not in log.redact('password S3rvice-test!')
    assert 'Ephemeral-secret' not in log.redact('test Ephemeral-secret')


class FakeRemote:
    instances=[]
    def __init__(self,*args):self.files={};self.commands=[];self.stage='/var/tmp/prodcast-manager-test';self.cleaned=False;self.closed=False;self.instances.append(self)
    def prepare_stage(self):pass
    def put_bytes(self,name,data):self.files[name]=data
    def command(self,cmd,**kwargs):self.commands.append(cmd);self.stdin=kwargs.get('stdin');return 'login banner\nDIRECTORY_RESULT:{"ok":true,"certificate_verified":true}\n'
    def cleanup(self):self.cleaned=True
    def close(self):self.closed=True


def test_probe_uses_app_ssh_no_sudo_install_or_docker_and_cleans():
    h={'fingerprint':'SHA256:'+'A'*43}
    result=probe_directory(h,{},settings(),'user','person','one-time-secret',remote_factory=FakeRemote)
    r=FakeRemote.instances[-1]
    assert result['ok'] and r.cleaned and r.closed
    assert len(r.commands)==1 and r.commands[0].startswith('python3 ')
    assert not any(x in r.commands[0] for x in ('docker','sudo','pip','one-time-secret'))
    assert len([n for n in r.files if n.endswith('.whl')])==2
    assert json.loads(r.stdin)['password']=='one-time-secret'
    assert not any(b'one-time-secret' in content for content in r.files.values())


def test_untrusted_app_is_rejected_before_ssh():
    with pytest.raises(ValueError):probe_directory({'fingerprint':''},{},settings(),remote_factory=lambda *a:pytest.fail('SSH called'))


def test_remote_failure_still_cleans():
    class Failed(FakeRemote):
        def command(self,*a,**kw):raise OSError('failure')
    with pytest.raises(OSError):probe_directory({'fingerprint':'SHA256:'+'A'*43},{},settings(),remote_factory=Failed)
    r=FakeRemote.instances[-1];assert r.cleaned and r.closed


@pytest.mark.parametrize('error,code',[(socket.gaierror(),'dns'),(socket.timeout(),'timeout'),(ssl.SSLCertVerificationError(),'certificate'),(ConnectionResetError(),'tls'),(ConnectionRefusedError(),'refused'),(RuntimeError('contains secret'),'directory_error')])
def test_probe_errors_do_not_leak_details(monkeypatch,error,code):
    def fail(*a):raise error
    monkeypatch.setattr(probe,'tls_connection',fail)
    assert probe.run_probe({'config':settings(),'mode':'connection'})=={'ok':False,'code':code}


def mock_directory(monkeypatch,member=True,disabled=False,login=True,secure=True):
    import ldap3
    connections=[];searches=[]
    class Conn:
        def __init__(self,server,**kwargs):
            assert kwargs['auto_referrals'] is False and kwargs['read_only'] is True
            self.kw=kwargs;self.closed=False;self.entries=[];connections.append(self)
        def bind(self):return login or len(connections)==1
        def unbind(self):self.closed=True
        def search(self,base,flt,**kw):
            searches.append((base,flt,kw))
            found=member if 'memberOf:' in flt else True
            self.entries=[SimpleNamespace(entry_dn='CN=Person,DC=example,DC=com',entry_attributes_as_dict={'userAccountControl':[2 if disabled else 512]})] if found else []
            return found
    def server(host,**kwargs):
        assert kwargs['use_ssl'] is secure
        if secure:
            assert kwargs['tls'].validate==ssl.CERT_REQUIRED
            assert kwargs['tls'].sni==host
        else: assert kwargs['tls'] is None
        return object()
    monkeypatch.setattr(ldap3,'Server',server);monkeypatch.setattr(ldap3,'Connection',Conn)
    return connections,searches


def test_user_group_and_password_all_required(monkeypatch):
    connections,searches=mock_directory(monkeypatch)
    r=probe.directory_check(settings(),'person','secret',True)
    assert r['allowed_group'] and r['user_bind']
    assert len(connections)==2 and all(c.closed for c in connections)
    assert connections[1].kw['user']=='CN=Person,DC=example,DC=com'
    assert any('1.2.840.113556.1.4.1941' in s[1] for s in searches)


@pytest.mark.parametrize('test_user',[False,True])
@pytest.mark.parametrize('secure',[False,True])
def test_search_uses_temporary_user_when_no_service_account(monkeypatch,test_user,secure):
    connections,searches=mock_directory(monkeypatch,secure=secure)
    value=dict(settings(),bind_dn='',bind_password='',allow_plain_ldap=not secure,
               url=('ldaps' if secure else 'ldap')+'://dc01.example.com')
    result=probe.directory_check(value,'person','secret',test_user)
    assert result['service_bind'] is False and result['user_bind'] is True
    assert connections[0].kw['user']=='person@example.com'
    assert connections[0].kw['password']=='secret'
    assert all(c.closed for c in connections)
    if test_user: assert result['allowed_group']


def test_no_service_account_never_falls_back_to_anonymous(monkeypatch):
    connections,_=mock_directory(monkeypatch)
    with pytest.raises(probe.ProbeFailure,match='test_credentials_required'):
        probe.directory_check(dict(settings(),bind_dn='',bind_password=''))
    assert not connections


def test_failed_direct_user_bind_cannot_pass(monkeypatch):
    import ldap3
    connections,_=mock_directory(monkeypatch)
    original=ldap3.Connection
    def denied(*args,**kwargs):
        conn=original(*args,**kwargs);conn.bind=lambda:False;return conn
    monkeypatch.setattr(ldap3,'Connection',denied)
    with pytest.raises(probe.ProbeFailure,match='bind_failed'):
        probe.directory_check(dict(settings(),bind_dn='',bind_password=''),'person','bad',True)
    assert len(connections)==1 and connections[0].closed


@pytest.mark.parametrize('opts,code',[({'member':False},'not_in_group'),({'disabled':True},'user_disabled_or_locked'),({'login':False},'bind_failed')])
def test_denied_user_is_not_success(monkeypatch,opts,code):
    connections,_=mock_directory(monkeypatch,**opts)
    with pytest.raises(probe.ProbeFailure,match=code):probe.directory_check(settings(),'person','secret',True)
    assert all(c.closed for c in connections)


def test_search_escapes_username_not_ldap_filter(monkeypatch):
    _,searches=mock_directory(monkeypatch)
    probe.directory_check(settings(),'person*)(objectClass=*)','secret',True)
    assert any(r'person\2a\29\28objectClass=\2a\29' in row[1] for row in searches)


def test_foreign_upn_not_sent_as_user_bind(monkeypatch):
    connections,_=mock_directory(monkeypatch)
    with pytest.raises(probe.ProbeFailure,match='wrong_domain'):probe.directory_check(settings(),'person@other.test','secret',True)
    assert len(connections)==1


def test_real_tls_rejects_untrusted_ca_and_accepts_trusted(tmp_path,pki):
    # Dispose Tk objects from preceding GUI tests on the main thread, not in
    # this test's TLS listener thread (Tk destruction is thread-affine).
    import gc
    gc.collect()
    for name in ('site.crt','site.key'):(tmp_path/name).write_text(pki[name])
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(tmp_path/'site.crt'),str(tmp_path/'site.key'))
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(20)
    port=listener.getsockname()[1]
    def serve():
        for _ in range(2):
            try:
                conn,_=listener.accept()
                with conn:
                    try:
                        with context.wrap_socket(conn,server_side=True):pass
                    except OSError:pass
            except OSError:break
        listener.close()
    thread=threading.Thread(target=serve);thread.start()
    try:
        assert probe.tls_connection(dict(settings(),url=f'ldaps://localhost:{port}',ca_pem=pki['ca.crt']))['certificate_verified']
        with pytest.raises(ssl.SSLCertVerificationError):probe.tls_connection(dict(settings(),url=f'ldaps://localhost:{port}'))
    finally:thread.join(timeout=6);listener.close()


def test_dialog_saved_fields_do_not_include_test_identity():
    root=tk.Tk();root.withdraw();saved=[]
    try:
        d=DirectoryDialog(root,settings(),saved.append,lambda *a:{'ok':True});d.withdraw()
        d.test_user.set('test');d.test_password.set('ephemeral');d.save()
        assert len(saved)==1 and 'ephemeral' not in json.dumps(saved)
        assert d.test_password.get()==''
        assert 'чтобы применить' in d.status.get()
        d.close()
    finally:root.destroy()


def test_english_notice_and_errors():
    try:
        set_language('en')
        assert 'applied' in tr(NOTICE)
        for message in ERRORS.values():assert tr(message)!=message
    finally:set_language('ru')
