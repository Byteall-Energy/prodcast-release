import socket
from types import SimpleNamespace
import pytest
from test_directory import settings,probe,mock_directory
from prodcast_manager.directory import validate_directory,app_directory,save_directory


def plain(**kwargs):
    return dict(settings(),url='ldap://DC01.example.com',allow_plain_ldap=True,**kwargs)


def test_plain_requires_explicit_boolean_acknowledgement():
    for flag in (None,False,'true',1):
        with pytest.raises(ValueError):validate_directory(dict(settings(),url='ldap://dc01.example.com',allow_plain_ldap=flag))
    assert validate_directory(plain())['url']=='ldap://dc01.example.com:389'


def test_plain_mapping_and_saved_consent():
    vault=SimpleNamespace(data={},save=lambda:None)
    save_directory(vault,dict(plain(),enabled=True))
    assert vault.data['directory']['allow_plain_ldap'] is True
    mapped=app_directory(vault.data['directory'])
    assert mapped['mode']=='ldap' and mapped['server_uri']=='ldap://dc01.example.com:389'
    assert mapped['ca_certificate']==''
    assert 'bind_password' not in mapped and 'allow_plain_ldap' not in mapped


def test_plain_connection_probe_never_uses_tls(monkeypatch):
    calls=[]
    class Sock:
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(probe.socket,'create_connection',lambda address,**kw:calls.append(address) or Sock())
    monkeypatch.setattr(probe.ssl,'create_default_context',lambda **kw:pytest.fail('TLS unexpectedly used'))
    result=probe.tls_connection(plain())
    assert calls==[('dc01.example.com',389)]
    assert result['encrypted'] is False and result['certificate_verified'] is False


def test_plain_user_and_group_checks_preserved(monkeypatch):
    import ldap3
    connections,searches=mock_directory(monkeypatch)
    def server(host,**kwargs):
        assert kwargs['port']==389 and kwargs['use_ssl'] is False and kwargs['tls'] is None
        return object()
    monkeypatch.setattr(ldap3,'Server',server)
    result=probe.directory_check(plain(),'person','ephemeral',True)
    assert result['allowed_group'] and result['user_bind']
    assert len(connections)==2 and all(c.closed for c in connections)


def test_probe_without_acknowledgement_never_connects(monkeypatch):
    monkeypatch.setattr(probe.socket,'create_connection',lambda *a,**kw:pytest.fail('Network called'))
    result=probe.run_probe({'config':dict(settings(),url='ldap://dc01.example.com'),'mode':'connection'})
    assert result=={'ok':False,'code':'plaintext_not_allowed'}


def test_ldaps_never_falls_back_to_plain(monkeypatch):
    def fail(**kw):raise probe.ssl.SSLCertVerificationError('untrusted issuer')
    monkeypatch.setattr(probe.ssl,'create_default_context',fail)
    monkeypatch.setattr(probe.socket,'create_connection',lambda *a,**kw:pytest.fail('Fallback attempted'))
    result=probe.run_probe({'config':dict(settings(),allow_plain_ldap=True),'mode':'connection'})
    assert result=={'ok':False,'code':'certificate'}
