from .i18n import tr, english_log
import argparse
import getpass
import json
import sys
from pathlib import Path
from .config import example, validate, atomic_json, roles, worker_roles
from .release import Release
from .engine import Engine, plan, RESOURCES
from .ssh import scan
from .session_logs import SessionLog, application_directory
from .portable import (last_directory, import_site, runtime_config, save_config,
                       open_credentials, LegacyPasswordRequired, remember)

def main():
    # Windows pipes may default to an ANSI code page. Deployment messages and
    # client paths contain Unicode; a logging error must not interrupt a step.
    for stream in (sys.stdout,sys.stderr):
        if stream is not None and hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    parser=argparse.ArgumentParser(description='ProdCast deployment manager')
    parser.add_argument('action',nargs='?',choices=['gui','init','trust','credentials','plan','check','install','update','repair','ai','status','backup','import-pfx','app-tls','add-workers','self-test'],default='gui')
    parser.add_argument('--workers-site',help='Proposed site.json with additional Workers; --site remains the installed profile')
    parser.add_argument('--site',default=None);parser.add_argument('--release');parser.add_argument('--manifest-sha256',default='');parser.add_argument('--yes',action='store_true');parser.add_argument('--role',choices=('app','db','ai')+tuple('worker'+str(n) for n in range(1,17)))
    parser.add_argument('--gui-smoke',action='store_true',help='With self-test: initialize and close the hidden GUI without network access')
    parser.add_argument('--pfx',help='Customer PFX/P12 file; its password is requested interactively')
    parser.add_argument('--app-url',help='Customer HTTPS origin matching the PFX SAN')
    parser.add_argument('--ca-chain',help='Optional PEM CA chain including root, if absent from PFX')
    parser.add_argument('--ai-model',default='',help='AI models ZIP with chat and embedding models (legacy official chat GGUF also accepted)')
    parser.add_argument('--ollama-components',default='',help='Separate Ollama components ZIP from the offline release')
    a=parser.parse_args();home=application_directory();path=Path(a.site).resolve() if a.site else last_directory(home)/'site.json';directory=path.parent
    if a.action=='gui':
        from .tk_ui import main as gui
        gui();return
    if a.action=='self-test':
        for name in ('linux.py','worker.ps1','worker-service-runner.py','app.env.in','worker-grants.sql','directory_probe.py','maintenance_linux.py','restore_compatibility.py','workers_linux.py','ai-documents.sql'):assert (RESOURCES/name).stat().st_size>100
        assert len(list((RESOURCES/'ldap_deps').glob('*.whl')))==2
        compile((RESOURCES/'linux.py').read_text('utf-8'),'linux.py','exec')
        validate(example())
        # Exercise the bundled PKCS#12/verification code with synthetic material only.
        from .app_certificate import read_pfx
        from .vault import make_pki
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        c=example();c['public_url']='https://prodcast.example.test';tls=make_pki(c)
        data=pkcs12.serialize_key_and_certificates(b'self-test',
            serialization.load_pem_private_key(tls['site.key'].encode(),None),
            x509.load_pem_x509_certificate(tls['site.crt'].encode()),
            [x509.load_pem_x509_certificate(tls['ca.crt'].encode())],serialization.NoEncryption())
        assert read_pfx(data,'',c['public_url'])['url']==c['public_url']
        if a.gui_smoke:
            from .tk_ui import smoke_test
            smoke_test(home)
        print('PASS: bundled resources, configuration and PFX import'+(' and portable credentials / GUI initialization' if a.gui_smoke else ''));return
    if a.action=='init':
        if path.exists():raise ValueError('Site file already exists')
        atomic_json(path,example());print('Edit VM addresses and site settings in '+str(path));return
    directory,c=import_site(path,home);path=directory/'site.json';remember(home,directory)
    if a.action=='plan':print(json.dumps(plan('update' if a.release else 'install',c.get('install_ai',True),worker_roles(c)),indent=2));return
    if a.action=='trust':
        for r in ([a.role] if a.role else [r for r in roles(c) if r!='ai' or c.get('install_ai',True)]):
            fp=scan(c['hosts'][r]);print(r,c['hosts'][r]['address'],fp)
            if input('Verify through VM console; type the full fingerprint to trust: ').strip()!=fp:raise ValueError('Fingerprint not confirmed')
            c['hosts'][r]['fingerprint']=fp;save_config(directory,c)
        return
    try:v=open_credentials(directory)
    except LegacyPasswordRequired:v=open_credentials(directory,getpass.getpass('Original vault password (one-time import): '))
    if a.action=='import-pfx':
        from .app_certificate import read_pfx,store_app_certificate
        if not a.pfx or not a.app_url:raise ValueError('Provide --pfx and --app-url')
        material=read_pfx(Path(a.pfx).read_bytes(),getpass.getpass('PFX password: '),a.app_url,
                          Path(a.ca_chain).read_bytes() if a.ca_chain else b'')
        store_app_certificate(v,c,material)
        print(json.dumps({'url':material['url'],**material['info'],'saved_to_vault':True,'server_changed':False},ensure_ascii=False,indent=2))
        return
    if a.action=='credentials':
        for r in ([a.role] if a.role else [r for r in roles(c) if r!='ai' or c.get('install_ai',True)]):
            auth={};h=c['hosts'][r]
            auth['password' if h['auth']=='password' else 'passphrase']=getpass.getpass(r+': SSH password / key passphrase: ')
            if not r.startswith('worker') and h['username']!='root':auth['sudo_password']=getpass.getpass(r+': sudo password (empty for NOPASSWD): ')
            v.data.setdefault('ssh',{})[r]=auth
        v.save();return
    if a.action in ('install','update','repair','backup','app-tls','ai','add-workers') and not a.yes:raise ValueError('Review plan and provide --yes to apply this operation')
    release=Release(a.release,directory/'cache',a.manifest_sha256,validate_ai=False,model_path=a.ai_model,ollama_path=a.ollama_components) if a.release else None
    logger=SessionLog();logger.protect(v.data)
    def log(message):print(logger.append(message),flush=True)
    log(tr('Локальный журнал: ')+str(logger.path))
    try:
        if a.action=='add-workers':
            from .workers import add_workers
            if not a.workers_site or not release:raise ValueError('Provide --workers-site and the exact installed --release')
            proposed=runtime_config(Path(a.workers_site).resolve());validate(proposed,True)
            for role in worker_roles(proposed):
                if role not in v.data.get('ssh',{}):
                    host=proposed['hosts'][role]
                    v.data.setdefault('ssh',{})[role]={'password' if host['auth']=='password' else 'passphrase':getpass.getpass(role+': SSH password / key passphrase: ')}
            v.save()
            add_workers(directory,proposed,v,release,log,diagnostics=logger)
        else:Engine(c,v,release,directory,log,diagnostics=logger).run(a.action)
    except Exception as e:
        log(tr('ОШИБКА: ')+str(e));raise

if __name__=='__main__':
    try:main()
    except Exception as e:print('ERROR:',english_log(str(e)));raise SystemExit(1)
