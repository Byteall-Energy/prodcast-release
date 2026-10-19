import copy
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from prodcast_manager.app_certificate import read_pfx, store_app_certificate, validate_material, validate_for_site
from prodcast_manager.config import example, topology_hash
from prodcast_manager.engine import Engine
from prodcast_manager.session_logs import SessionLog
from prodcast_manager.vault import Vault, make_pki

PASSWORD='customer pfx secret'
URL='https://prodcast.customer.local'


@lru_cache
def credentials():
    c=example();c['public_url']=URL
    tls=make_pki(c)
    return (serialization.load_pem_private_key(tls['site.key'].encode(),None),
            x509.load_pem_x509_certificate(tls['site.crt'].encode()),
            x509.load_pem_x509_certificate(tls['ca.crt'].encode()),tls)


def pack(key=None,cert=None,ca=True):
    k,c,r,_=credentials()
    return pkcs12.serialize_key_and_certificates(b'ProdCast',key or k,cert or c,[r] if ca else None,
                                                serialization.BestAvailableEncryption(PASSWORD.encode()))


def material():return read_pfx(pack(),PASSWORD,URL)


def test_pfx_import_encrypts_key_and_preserves_internal_pki_on_update(tmp_path):
    site=example();v=Vault(tmp_path/'vault.json','long enough master password');v.initialize(site)
    original=copy.deepcopy(v.data);m=material();encrypted=v.path.read_bytes()
    store_app_certificate(v,site,m)
    assert v.data['tls']==original['tls'] and v.data['secrets']==original['secrets']
    assert v.data['topology']==topology_hash(site)
    assert v.data['app_tls']['url']==URL
    assert next((tmp_path/'history').glob('vault-before-app-pfx-*.json')).read_bytes()==encrypted
    assert m['private_key'] not in v.path.read_text() and PASSWORD not in v.path.read_text()
    reloaded=Vault(v.path,'long enough master password');reloaded.initialize(site)
    assert reloaded.data==v.data
    assert 'password' not in m and m['certificate'].count('BEGIN CERTIFICATE')==1
    logger=SessionLog(tmp_path/'logs');logger.protect(v.data)
    assert m['private_key'] not in logger.append(m['private_key'])


def test_import_before_initial_install_retained(tmp_path):
    v=Vault(tmp_path/'vault.json','long enough master password');m=material()
    store_app_certificate(v,example(),m);v.initialize(example())
    assert v.data['app_tls']==m


@pytest.mark.parametrize('password',[PASSWORD+'wrong',''])
def test_wrong_password_does_not_leak(password):
    with pytest.raises(ValueError) as error:read_pfx(pack(),password,URL)
    assert password not in str(error.value) if password else True
    assert 'PFX' in str(error.value)


@pytest.mark.parametrize('url',['https://other.customer.local','https://deep.prodcast.customer.local'])
def test_wrong_san_rejected(url):
    with pytest.raises(ValueError,match='SAN'):read_pfx(pack(),PASSWORD,url)


@pytest.mark.parametrize('url',['http://prodcast','https://prodcast/path','https://prodcast:8443','https://user:secret@prodcast','https://192.168.31.23','https://bad_host','https://prodcast/;x'])
def test_invalid_customer_origin(url):
    with pytest.raises(ValueError):read_pfx(pack(),PASSWORD,url)


def test_missing_ca_requires_explicit_chain_and_supports_supplement():
    data=pack(ca=False)
    with pytest.raises(ValueError,match='корневого CA'):read_pfx(data,PASSWORD,URL)
    ca=credentials()[2].public_bytes(serialization.Encoding.PEM)
    assert read_pfx(data,PASSWORD,URL,ca)['url']==URL


def test_cert_only_pfx_and_ca_private_key_rejected():
    _,leaf,ca,tls=credentials()
    data=pkcs12.serialize_key_and_certificates(b'cert',None,leaf,[ca],serialization.NoEncryption())
    with pytest.raises(ValueError,match='приватный ключ'):read_pfx(data,'',URL)
    key=serialization.load_pem_private_key(tls['ca.key'].encode(),None)
    data=pkcs12.serialize_key_and_certificates(b'ca',key,ca,None,serialization.NoEncryption())
    with pytest.raises(ValueError,match='ключ CA'):read_pfx(data,'',URL)


def signed_leaf(*,days=30,usage=ExtendedKeyUsageOID.SERVER_AUTH,hostname='prodcast.customer.local'):
    key,_,ca,tls=credentials();now=datetime.now(timezone.utc)
    ca_key=serialization.load_pem_private_key(tls['ca.key'].encode(),None)
    return (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,hostname)]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(days=10)).not_valid_after(now+timedelta(days=days))
            .add_extension(x509.BasicConstraints(False,None),True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]),False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),False)
            .add_extension(x509.ExtendedKeyUsage([usage]),False).sign(ca_key,hashes.SHA256()))


def test_expired_and_client_only_certificates_rejected():
    with pytest.raises(ValueError,match='истёк'):read_pfx(pack(cert=signed_leaf(days=-1)),PASSWORD,URL)
    with pytest.raises(ValueError,match='Server Authentication'):
        read_pfx(pack(cert=signed_leaf(usage=ExtendedKeyUsageOID.CLIENT_AUTH)),PASSWORD,URL)


def test_wildcard_only_covers_one_label():
    data=pack(cert=signed_leaf(hostname='*.customer.local'))
    assert read_pfx(data,PASSWORD,URL)['url']==URL
    with pytest.raises(ValueError):read_pfx(data,PASSWORD,'https://nested.prodcast.customer.local')


def test_short_internal_dns_name_supported():
    assert read_pfx(pack(cert=signed_leaf(hostname='prodcast')),PASSWORD,'https://prodcast')['url']=='https://prodcast'


def test_intermediate_chain_is_verified_and_ordered_for_nginx():
    leaf_key,_,old_root,tls=credentials();now=datetime.now(timezone.utc)
    root_key=serialization.load_pem_private_key(tls['ca.key'].encode(),None)
    intermediate_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def ca(subject,issuer,key,issuer_key,path_length):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=90))
                .add_extension(x509.BasicConstraints(True,path_length),True)
                .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),False)
                .sign(issuer_key,hashes.SHA256()))
    root=ca(old_root.subject,old_root.subject,root_key,root_key,1)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Customer issuing CA')])
    intermediate=ca(name,root.subject,intermediate_key,root_key,0)
    old=signed_leaf()
    builder=(x509.CertificateBuilder().subject_name(old.subject).issuer_name(name).public_key(leaf_key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(old.not_valid_before_utc).not_valid_after(old.not_valid_after_utc))
    for ext in old.extensions:
        if not isinstance(ext.value,x509.AuthorityKeyIdentifier):builder=builder.add_extension(ext.value,ext.critical)
    leaf=builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(intermediate_key.public_key()),False).sign(intermediate_key,hashes.SHA256())
    data=pkcs12.serialize_key_and_certificates(b'ProdCast',leaf_key,leaf,[root,intermediate],serialization.BestAvailableEncryption(PASSWORD.encode()))
    m=read_pfx(data,PASSWORD,URL)
    assert x509.load_pem_x509_certificates(m['certificate'].encode())==[leaf,intermediate]
    assert x509.load_pem_x509_certificates(m['ca'].encode())==[root]
    assert validate_material(m)==[leaf,intermediate,root]


def test_key_mismatch_and_unrelated_root_rejected():
    m=material();other=make_pki(example())
    m['private_key']=other['site.key']
    with pytest.raises(ValueError,match='не соответствуют'):validate_material(m)
    m=material();m['ca']=other['ca.crt']
    with pytest.raises(ValueError):validate_material(m)


def test_same_internal_name_rejected():
    c=example();c['public_url']=URL
    with pytest.raises(ValueError,match='отличаться'):validate_for_site(material(),c)


@pytest.mark.parametrize('journal',['journal.json','app-tls-journal.json'])
def test_pending_operation_blocks_replacement(tmp_path,journal):
    v=Vault(tmp_path/'vault.json','long enough master password');v.initialize(example());before=v.path.read_bytes()
    (tmp_path/journal).write_text(json.dumps({'status':'failed'}))
    with pytest.raises(ValueError,match='прерванную'):store_app_certificate(v,example(),material())
    assert v.path.read_bytes()==before and 'app_tls' not in v.data


def live_site():
    c=example()
    c['public_url']='https://192.168.31.23';c['admin_ip']='192.168.31.225'
    for h in c['hosts'].values():
        h['address']=h['address'].replace('192.0.2.','192.168.31.')
        h['fingerprint']='SHA256:'+'A'*43
    return c


def test_customer_key_only_sent_to_app(tmp_path):
    c=live_site();v=Vault(tmp_path/'vault.json','long enough master password');v.initialize(c)
    store_app_certificate(v,c,material());e=Engine(c,v,None,tmp_path)
    assert e.payload('app','op','app-tls')['app_tls']['private_key']==v.data['app_tls']['private_key']
    for r in ('db','ai','worker1','worker2'):
        p=e.payload(r,'op','status');assert p['app_tls'] is None and 'ca.key' not in p['tls']


def test_app_tls_operation_only_connects_app_and_resumes(tmp_path):
    c=live_site();v=Vault(tmp_path/'vault.json','long enough master password');v.initialize(c)
    store_app_certificate(v,c,material());calls=[];fail=[True]
    class Remote:
        def __init__(self,role,*args):assert role=='app'
        def probe(self):return {}
        def prepare_stage(self):pass
        def put_bytes(self,name,*args):assert name=='linux.py'
        def action(self,payload,action,*args):
            calls.append((action,payload['operation']))
            if action=='preflight':return {'managed':True,'version':'v0.2'}
            if fail[0]:raise RuntimeError('simulated failure')
            return {'https':True}
        def cleanup(self):pass
        def close(self):pass
    e=Engine(c,v,None,tmp_path,log=lambda s:None,remote_factory=Remote)
    with pytest.raises(RuntimeError):e.run('app-tls')
    fail[0]=False;e.run('app-tls')
    assert {action for action,_ in calls}=={'preflight','app-tls'}
    assert len({op for _,op in calls})==1
    assert json.loads((tmp_path/'app-tls-journal.json').read_text())['status']=='complete'


def test_gui_pfx_import_and_reopen_without_password_persistence(tmp_path,monkeypatch):
    import tkinter as tk
    from prodcast_manager.gui import App
    pfx=tmp_path/'customer.pfx';pfx.write_bytes(pack())
    root=tk.Tk();root.withdraw();app=App(root,log_dir=tmp_path/'logs',app_home=tmp_path/'manager')
    try:
        app.populate(example())
        app.directory=tmp_path/'site';app.master.set('long enough master password')
        app.pfx_path.set(str(pfx));app.pfx_password.set(PASSWORD);app.pfx_url.set(URL)
        errors=[];monkeypatch.setattr('prodcast_manager.gui.messagebox.showerror',lambda *a:errors.append(a))
        app.import_pfx(True)
        assert not errors and app.pfx_password.get()==''
        v=app.vault();assert v.data['app_tls']['url']==URL
        app.load_certificate_info();assert URL in app.certificate_info.get()
        assert app.vars['public_url'].get()==example()['public_url']
        for path in [app.directory/'site.json',app.directory/'secrets.json',app.session_log.path]:
            text=path.read_text('utf-8');assert PASSWORD not in text and 'BEGIN PRIVATE KEY' not in text
        app.busy=True;app.import_pfx(True);assert not errors
    finally:root.destroy()
