#!/usr/bin/env python3
"""Uploaded agent. Standard library only. Never prints command output or secrets."""
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import re
import shutil
import shlex
import socket
import ssl
import stat
import string
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

PG='pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff'
REDIS='redis:8.2.9-alpine@sha256:30abb90e62f14b737010746def3ba99cc79fe19dcdb3d37b41f21fc62e7da19d'
OLLAMA_SHA='cf95886728959aa09910bb34de5cca1cc5a8f68003b5597197d3f2c2d57c0804'
APIS=[x+'-service' for x in ('admin','identity','catalog','data','workflow','scenario','analytics','integration')]
ROOT=Path('/opt/prodcast-manager'); STATE=Path('/var/lib/prodcast-manager')
OLLAMA_HOME=Path('/var/lib/prodcast-ollama')
EMBEDDING_MODEL='qwen3-embedding:0.6b'
EMBEDDING_DIGEST='ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d'
P={}; C={}; S={}; ST={}; LOG=None

def worker_roles():
    return sorted((r for r in C['hosts'] if re.fullmatch(r'worker[1-9][0-9]*',r)),key=lambda r:int(r[6:]))

def worker_numbers():return [f'{int(r[6:]):02}' for r in worker_roles()]

def worker_grants():
    sql=(Path(P['stage'])/'worker-grants.sql').read_text()
    targets=','.join('prodcast_worker_'+n for n in worker_numbers())
    return re.sub(r'prodcast_worker_01,\s*prodcast_worker_02',targets,sql)

def write(path,text,mode=0o600):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        os.chmod(tmp,mode); f.write(text); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)

def save(): write(STATE/'state.json',json.dumps(ST,indent=2))

def run(args,input=None,cwd=None,timeout=7200):
    # Every action is a fresh SSH process: bootstrap's environment does not carry
    # into install. Never let debconf/MOK/needrestart wait for an invisible prompt.
    env=dict(os.environ,DEBIAN_FRONTEND='noninteractive',APT_LISTCHANGES_FRONTEND='none',NEEDRESTART_MODE='l')
    args=[str(a) for a in args]
    if Path(args[0]).name=='apt-get':
        args[1:1]=['-o','Dpkg::Options::=--force-confdef','-o','Dpkg::Options::=--force-confold']
    p=subprocess.run(args,input='' if input is None else input,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if LOG:
        # Protected server log. Client diagnostic copies are redacted before saving.
        LOG.write(p.stdout); LOG.write(p.stderr); LOG.flush()
    if p.returncode: raise RuntimeError('Command failed: '+str(args[0])+'; inspect /var/lib/prodcast-manager/operation.log')
    return p.stdout

def dc(role,*args,input=None):
    if role=='db': folder=ROOT/'db'; cmd=['docker','compose','-p','prodcast-managed-db','-f','compose.json']
    elif role=='ai': folder=target()/'ai'; cmd=['docker','compose','-p','prodcast-managed-ai','-f','compose.json']
    else:
        folder=target()/role/'runtime'
        cmd=['docker','compose','-p','prodcast-managed-'+role,'--env-file','../packages.site.env']
        if role=='app': cmd+=['--env-file','site.env']
        cmd+=['-f','compose.yaml','-f','site.compose.json']
    return run(cmd+list(args),cwd=folder,input=input)

def target(): return ROOT/'releases'/P.get('version','')
def old_target(): return ROOT/'releases'/ST['version']

def current_dc(role,*args,input=None):
    v=P['version']; P['version']=ST['version']
    try: return dc(role,*args,input=input)
    finally: P['version']=v

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def request(url,data=None,headers=None,ca=None,timeout=180):
    hdr=dict(headers or {})
    if data is not None: hdr['Content-Type']='application/json'
    req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers=hdr)
    context=ssl.create_default_context(cafile=ca) if ca else None
    try:response=urllib.request.urlopen(req,context=context,timeout=timeout)
    except urllib.error.URLError as e:
        # Old Manager certificates lack AKI/SKI. Keep CA and hostname verification;
        # match pre-Python-3.13 validation only for this explicit legacy error.
        reason=getattr(e,'reason',None)
        legacy=isinstance(reason,ssl.SSLCertVerificationError) and getattr(reason,'verify_message','') in ('Missing Authority Key Identifier','Missing Subject Key Identifier')
        if not (ca and legacy and context.verify_flags & ssl.VERIFY_X509_STRICT):raise
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        response=urllib.request.urlopen(req,context=context,timeout=timeout)
    with response as r:
        raw=r.read(); return json.loads(raw) if raw else {}

def wait_http(url,service='HTTP service',**kw):
    for n in range(60):
        try: return request(url,**kw)
        except Exception as e:
            if n==59: raise RuntimeError(service+' did not become ready ('+type(e).__name__+'); inspect the server operation log') from e
            time.sleep(3)

def prepare_ollama_runtime(ollama):
    # This tree contains only public release binaries, never configuration or keys.
    ollama=Path(ollama)
    if ollama.is_symlink() or ollama.parent.is_symlink():
        raise RuntimeError('Unexpected symlink in Ollama runtime path')
    root=ollama.resolve(strict=True)
    entries=[root,*root.rglob('*')]
    for path in entries:
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise RuntimeError('Ollama runtime symlink escapes installation directory')
        elif not (path.is_dir() or path.is_file()):
            raise RuntimeError('Unexpected file type in Ollama runtime')
    ollama.parent.chmod(0o755)
    for path in entries:
        if path.is_symlink():continue
        mode=path.stat().st_mode
        path.chmod(0o755 if stat.S_ISDIR(mode) or mode & 0o111 else 0o644)


def prepare_ollama_home(home,uid,gid):
    # The account can survive a reset while its home is removed. useradd is then
    # skipped, so directory creation and ownership must be repaired independently.
    home=Path(home);models=home/'models'
    for path in (home,models):
        if path.is_symlink():raise RuntimeError('Unexpected symlink in Ollama data directory')
    for path in (home,models):
        path.mkdir(parents=True,exist_ok=True)
        path.chmod(0o750);os.chown(path,uid,gid)
    return models

def linux_os_release():
    if hasattr(platform,'freedesktop_os_release'):return platform.freedesktop_os_release()
    # RHEL 9 ships Python 3.9, before platform.freedesktop_os_release was added.
    result={}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1);tokens=shlex.split(value,comments=True)
        result[key]=tokens[0] if tokens else ''
    return result


def supported_os(osinfo=None):
    osinfo = osinfo or linux_os_release()
    distro, version = osinfo.get('ID'), osinfo.get('VERSION_ID', '').split('.')[0]
    ubuntu = {'22.04': 'jammy', '24.04': 'noble', '26.04': 'resolute'}
    debian = {'12': 'bookworm', '13': 'trixie'}
    if distro=='rhel' and version=='9':return 'rhel','9'
    codename = ubuntu.get(osinfo.get('VERSION_ID')) if distro == 'ubuntu' else debian.get(version) if distro == 'debian' else None
    if not codename:
        raise RuntimeError('Supported OS: Ubuntu Server 22.04/24.04/26.04 LTS, Debian 12/13 or RHEL 9 amd64; detected '+str(distro)+' '+osinfo.get('VERSION_ID','unknown'))
    return distro, codename

def preflight():
    if os.geteuid()!=0 or platform.machine()!='x86_64': raise RuntimeError('root and Linux amd64 required')
    supported_os()
    if (STATE/'state.json').exists():
        state=json.loads((STATE/'state.json').read_text())
        if state['site']!=C['site_id'] or state['topology']!=P['topology'] or state['role']!=P['role']: raise RuntimeError('Managed topology mismatch')
        if P.get('installation_id') and state.get('installation_id')!=P['installation_id']: raise RuntimeError('This VM belongs to another vault; use the original encrypted installation state')
        if P['role']=='db' and state.get('worker_hosts'):
            roster={r:C['hosts'][r]['address'] for r in worker_roles()}
            previous=P.get('worker_expansion',{}).get('previous_workers')
            retiring=P.get('worker_action',{})
            retirement_target={r:h['address'] for r,h in retiring.get('target',{}).get('hosts',{}).items() if r.startswith('worker')}
            retirement_receipt=STATE/'worker-retirement.json'
            retired=json.loads(retirement_receipt.read_text()) if retirement_receipt.exists() else {}
            retirement_ok=(P['mode']=='worker-remove' and (state.get('maintenance')==P['operation'] or
                           (retired.get('operation')==P['operation'] and retired.get('complete') and retired.get('target')==retirement_target))
                           and retiring.get('previous_workers')==roster and retirement_target==state['worker_hosts'])
            if roster!=state['worker_hosts'] and not retirement_ok and not (P['mode']=='add-workers' and previous==state['worker_hosts']):
                raise RuntimeError('Use the updated installation profile with the complete Worker roster')
        if state.get('maintenance') and state['maintenance']!=P['operation'] and P['action'] not in ('preflight','release-operation'):raise RuntimeError('A maintenance operation owns this VM; resume it from the original profile')
        if P['mode'] in ('install','update','repair') and state.get('version')==P['version'] and state.get('manifest')!=P['manifest']: raise RuntimeError('Same release version has a different manifest; refusing replacement')
        if (state.get('operation') and state['operation']!=P['operation'] and P['mode'] in ('install','update','repair','app-tls')
                and P['action']!='release-operation' and P.get('previous_operation')!=state.get('operation')): raise RuntimeError('Another operation owns this host; resume its journal')
        transaction=STATE/'app-tls-transaction.json'
        if P['role']=='app' and P['mode'] in ('install','update','repair','app-tls') and transaction.exists():
            pending=json.loads(transaction.read_text())
            if not pending.get('complete') and (P['mode']!='app-tls' or pending['operation']!=P['operation']):
                raise RuntimeError('Resume the pending customer HTTPS operation before updating App')
        return dict(managed=True,version=state.get('version',''),hostname=platform.node(),
                    operation=state.get('operation'),manifest=state.get('manifest'),maintenance=state.get('maintenance'))
    # Refuse adoption of an unmanaged installation or listeners on service ports.
    if ROOT.exists() and any(ROOT.iterdir()): raise RuntimeError('Unmanaged destination exists')
    if shutil.which('docker'):
        containers=run(['docker','ps','-a','--format','{{.Names}}'])
        if 'prodcast' in containers.lower(): raise RuntimeError('Unmanaged ProdCast containers found')
    ports={'db':[5432,6380,6379],'app':[80,443,8100,8443],'ai':[8443,11434]}[P['role']]
    for port in ports:
        with socket.socket() as sock:
            try: sock.bind(('0.0.0.0',port))
            except OSError: raise RuntimeError('Service port already occupied: '+str(port))
    own=[a['local'] for i in json.loads(run(['ip','-j','-4','addr'])) for a in i.get('addr_info',[])]
    if C['hosts'][P['role']]['address'] not in own: raise RuntimeError('Configured IPv4 is not assigned to this VM')
    return dict(managed=False,version='',hostname=platform.node())

def claim():
    ST.update(site=C['site_id'],topology=P['topology'],installation_id=P['installation_id'],role=P['role'],operation=P['operation'])
    ST.setdefault('steps',{}); save(); ROOT.mkdir(exist_ok=True); ROOT.chmod(0o755)
    return {}

def release_operation():
    """Release only the exact stale deployment owner named by the Manager."""
    if ST.get('operation') not in ('',None,P['operation']):
        raise RuntimeError('The VM is owned by a different operation; refusing to release it')
    if ST.get('operation')==P['operation']:
        ST['operation']=''; save()
    return {'released':True,'operation':P['operation']}

def offline_asset(name):
    entry=next((a for a in P['files'].values() if a['name']==name),None)
    if not entry or Path(name).name!=name:raise RuntimeError('Offline asset is missing from this server payload')
    path=Path(P['stage'])/name
    if not path.is_file() or digest(path)!=entry['sha256']:raise RuntimeError('Offline asset checksum mismatch: '+name)
    return path


def extract_offline(archive,destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    root=destination.resolve();total=0
    with tarfile.open(archive,'r:*') as tar:
        for member in tar:
            name=Path(member.name)
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):raise RuntimeError('Unsafe offline archive entry')
            path=destination/name
            if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(destination)):
                raise RuntimeError('Unsafe offline extraction destination')
            total+=member.size
            if total>8*1024**3:raise RuntimeError('Offline archive exceeds extraction limit')
            if member.isdir():path.mkdir(parents=True,exist_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name(path.name+'.offline-tmp')
                with tar.extractfile(member) as src,temporary.open('wb') as dst:shutil.copyfileobj(src,dst)
                temporary.chmod(0o644);temporary.replace(path)


def bootstrap_offline():
    osinfo=linux_os_release();supported_os(osinfo)
    key=osinfo['ID']+':'+osinfo['VERSION_ID']
    if osinfo['ID']=='rhel':key='rhel:9'
    name=P['offline']['linux_packages'].get(key)
    if not name:raise RuntimeError('Offline dependencies not supplied for '+key)
    archive=offline_asset(name)
    repo=STATE/'offline-packages'/digest(archive)
    if not (repo/'.complete').is_file():
        extract_offline(archive,repo);write(repo/'.complete','ok')
    if osinfo['ID']=='rhel':return bootstrap_rhel(repo)
    # Isolated local repository and cache: system Internet sources are never used.
    source=repo/'sources.list';write(source,'deb [trusted=yes] file:'+str(repo)+' ./\n',0o644)
    lists=repo/'lists';(lists/'partial').mkdir(parents=True,exist_ok=True)
    options=['-o','Dir::Etc::sourcelist='+str(source),'-o','Dir::Etc::sourceparts=-',
             '-o','Dir::State::lists='+str(lists),'-o','Dir::Cache::archives='+str(repo),
             '-o','APT::Get::List-Cleanup=0','-o','Acquire::Languages=none','-o','APT::Sandbox::User=root']
    docker_ready=False
    if shutil.which('docker'):
        try:
            version=run(['docker','compose','version','--short']).strip().lstrip('v')
            run(['docker','info','--format','{{.ServerVersion}}'])
            docker_ready=tuple(map(int,version.split('.')[:2]))>=(2,30)
        except (RuntimeError,ValueError):pass
    packages=['ca-certificates','curl','gnupg','zstd','iptables','tar','passwd','util-linux','python3-minimal']
    if not docker_ready:
        packages+=['docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin']
        # Ubuntu's Compose package owns the same plugin path as Docker CE's.
        # Remove only installed conflicting packages in the same APT transaction.
        installed=run(['dpkg-query','-W','-f=${binary:Package}\\t${Status}\\n'])
        for line in installed.splitlines():
            name,_,status=line.partition('\t')
            if name in ('docker-compose-v2','docker-compose') and status.endswith(' installed'):
                packages.append(name+'-')
    run(['apt-get',*options,'update'])
    run(['apt-get',*options,'install','--no-download','--no-install-recommends','-y',*packages])
    run(['systemctl','enable','--now','docker'])
    version=run(['docker','compose','version','--short']).strip().lstrip('v')
    if tuple(map(int,version.split('.')[:2]))<(2,30):raise RuntimeError('Offline Docker Compose is too old')
    return {'compose':version,'offline':True,'platform':key}


def selinux_enabled():
    return Path('/sys/fs/selinux/enforce').is_file()


def rpm_installed(name):
    try:return bool(run(['rpm','-q','--qf','%{NAME}',name]).strip())
    except RuntimeError:return False


def docker_ready():
    if not shutil.which('docker'):return False
    try:
        version=run(['docker','compose','version','--short']).strip().lstrip('v')
        run(['docker','info','--format','{{.ServerVersion}}'])
        return tuple(map(int,version.split('.')[:2]))>=(2,30)
    except (RuntimeError,ValueError):return False


def bootstrap_rhel(repo=None):
    ready=docker_ready()
    packages=['ca-certificates','curl' if rpm_installed('curl') else 'curl-minimal','gnupg2','zstd','iptables-nft','tar','shadow-utils','util-linux','python3','iproute','container-selinux','policycoreutils-python-utils','selinux-policy-targeted']
    # Asking DNF to install an installed name can select a newer build. The
    # offline UBI repository is not a full OS upgrade repository: preserve the
    # host's paired packages (iproute/iproute-tc, curl/OpenLDAP, audit/audit-libs).
    packages=[name for name in packages if not rpm_installed(name)]
    if not ready:
        if rpm_installed('podman') or rpm_installed('podman-docker'):
            raise RuntimeError('RHEL: Podman conflicts with Docker. Prepare a dedicated VM without Podman/podman-docker, then repeat the same operation.')
        packages+=['docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin']
    if repo:
        if not (repo/'repodata/repomd.xml').is_file():raise RuntimeError('Offline RHEL RPM repository metadata is missing')
        for name in ('redhat.asc','docker.asc'):run(['rpm','--import',repo/'keys'/name])
        rpms=sorted((repo/'rpms').glob('*.rpm'))
        if not rpms:raise RuntimeError('Offline RHEL RPM repository is empty')
        for rpm in rpms:run(['rpmkeys','--checksig',rpm])
        if 'policycoreutils-python-utils' in packages and not rpm_installed('python3-audit'):
            audit_version=run(['rpm','-q','--qf','%{VERSION}-%{RELEASE}.%{ARCH}','audit-libs']).strip()
            if audit_version=='3.0.7-101.el9_0.2.x86_64':
                # Signed bindings from the RHEL 9.0 DVD supplied with Manager.
                # Their exact audit-libs dependency keeps auditd unchanged.
                binding=Path(P['stage'])/'python3-audit-3.0.7-101.el9_0.2.x86_64.rpm'
                if not binding.is_file() or digest(binding)!='8a762e499ec4c12ac44243b3727ab079115519ed495caa76cee6b6c109edaf4c':
                    raise RuntimeError('RHEL 9.0 offline audit bindings are missing or damaged; reinstall the complete Manager package and repeat the same operation.')
                run(['rpmkeys','--checksig',binding])
                packages.append(str(binding))
        options=['--noplugins','--disablerepo=*','--repofrompath=prodcast-offline,file://'+str(repo),'--enablerepo=prodcast-offline',
                 '--setopt=prodcast-offline.gpgcheck=1','--setopt=prodcast-offline.repo_gpgcheck=0',
                 '--setopt=localpkg_gpgcheck=1','--setopt=prodcast-offline.metadata_expire=-1','--setopt=cachedir='+str(repo/'dnf-cache'),'--setopt=install_weak_deps=False']
        if packages:run(['dnf',*options,'install','-y',*packages])
    else:
        run(['dnf','install','-y','dnf-plugins-core','gnupg2','curl-minimal' if not rpm_installed('curl') else 'curl'])
        with urllib.request.urlopen('https://download.docker.com/linux/rhel/gpg',timeout=60) as response:key=response.read()
        keypath=STATE/'docker-rhel.asc';write(keypath,key.decode(),0o644)
        if '060A61C51B558A7F742B77AAC52FEB6B621E9F35' not in run(['gpg','--show-keys','--with-colons',keypath]):raise RuntimeError('Docker RHEL signing key mismatch')
        run(['rpm','--import',keypath])
        run(['dnf','config-manager','--add-repo','https://download.docker.com/linux/rhel/docker-ce.repo'])
        if packages:run(['dnf','install','-y','--setopt=install_weak_deps=False',*packages])
    restart=False
    if selinux_enabled():
        path=Path('/etc/docker/daemon.json');config=json.loads(path.read_text()) if path.exists() else {}
        if config.get('selinux-enabled') is not True:
            if ready and run(['docker','ps','-q']).strip():raise RuntimeError('Enable SELinux support in the existing Docker daemon before deploying to this RHEL VM; running containers were left unchanged.')
            config['selinux-enabled']=True;write(path,json.dumps(config,indent=2),0o644);restart=True
    run(['systemctl','enable','--now','docker'])
    if restart:run(['systemctl','restart','docker'])
    version=run(['docker','compose','version','--short']).strip().lstrip('v')
    if tuple(map(int,version.split('.')[:2]))<(2,30):raise RuntimeError('Docker Compose is too old')
    return {'compose':version,'offline':repo is not None,'platform':'rhel:9','selinux_enabled':selinux_enabled()}


def install_offline_model(uid,gid):
    spec=P['offline'];archive=offline_asset(spec['model_archive'])
    # Public model files only. Never replace the Ollama account or its private key.
    extract_offline(archive,OLLAMA_HOME/'models')
    bundle=P.get('files',{}).get('offline-model_bundle')
    if bundle:
        source=offline_asset(bundle['name'])
        with tarfile.open(source) as tar:
            for member in tar.getmembers():
                if not member.isfile() or not (re.fullmatch(r'blobs/sha256-[0-9a-f]{64}',member.name) or member.name in (
                    'manifests/registry.ollama.ai/library/qwen3/4b-instruct',
                    'manifests/registry.ollama.ai/library/qwen3-embedding/0.6b')):
                    raise RuntimeError('Unexpected file in AI model bundle')
        extract_offline(source,OLLAMA_HOME/'models')
    manifest=OLLAMA_HOME/'models/manifests/registry.ollama.ai/library/qwen3/4b-instruct'
    if 'sha256:'+digest(manifest)!=spec['model_digest']:raise RuntimeError('Bundled model manifest mismatch')
    model=json.loads(manifest.read_text())
    external=spec.get('external_model')
    if external and not bundle:
        source=offline_asset(P['files']['offline-model_blob']['name'])
        if source.stat().st_size!=external['bytes'] or digest(source)!=external['sha256']:raise RuntimeError('External AI model checksum mismatch')
        blobs=OLLAMA_HOME/'models/blobs';blobs.mkdir(exist_ok=True)
        destination=blobs/('sha256-'+external['sha256']);temporary=destination.with_name(destination.name+'.offline-tmp')
        shutil.copyfile(source,temporary);temporary.chmod(0o640);temporary.replace(destination)
    for item in [model['config'],*model['layers']]:
        if not re.fullmatch('sha256:[0-9a-f]{64}',item['digest']):raise RuntimeError('Unsafe model blob name')
        path=OLLAMA_HOME/'models/blobs'/item['digest'].replace(':','-')
        if path.stat().st_size!=item['size'] or 'sha256:'+digest(path)!=item['digest']:raise RuntimeError('Bundled model blob checksum mismatch')
    if bundle:verify_embedding_cache()
    for path in (OLLAMA_HOME/'models').rglob('*'):
        if path.is_symlink():raise RuntimeError('Unexpected symlink in offline model cache')
        os.chown(path,uid,gid);path.chmod(0o750 if path.is_dir() else 0o640)


def verify_embedding_cache():
    manifest=OLLAMA_HOME/'models/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b'
    if not manifest.is_file() or manifest.is_symlink() or digest(manifest)!=EMBEDDING_DIGEST:
        raise RuntimeError('Missing/corrupt qwen3-embedding:0.6b. Select an AI models ZIP containing both required models; no offline download will be attempted')
    model=json.loads(manifest.read_text())
    for item in [model['config'],*model['layers']]:
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',item['digest']):raise RuntimeError('Unsafe embedding blob name')
        blob=OLLAMA_HOME/'models/blobs'/item['digest'].replace(':','-')
        if blob.is_symlink() or not blob.is_file() or blob.stat().st_size!=item['size'] or digest(blob)!=item['digest'][7:]:raise RuntimeError('Embedding model blob checksum mismatch')


def same_model_digest(actual,expected):
    # Ollama's API reports a bare SHA256; registry manifests use sha256:<hex>.
    return actual.removeprefix('sha256:')==expected.removeprefix('sha256:')


def unpack_ollama(archive,folder):
    if str(archive).endswith('.xz'):
        import lzma
        plain=Path(P['stage'])/'ollama-runtime-unpacked.tar'
        try:
            with lzma.open(archive,'rb') as src,plain.open('wb') as dst:shutil.copyfileobj(src,dst)
            run(['tar','-xf',plain,'-C',folder])
        finally:plain.unlink(missing_ok=True)
    else:run(['tar','--zstd','-xf',archive,'-C',folder])


def bootstrap():
    if P.get('offline'):return bootstrap_offline()
    distro,codename=supported_os()
    if distro=='rhel':return bootstrap_rhel()
    os.environ['DEBIAN_FRONTEND']='noninteractive'
    compose_ok=False
    if shutil.which('docker'):
        try: compose_ok=tuple(map(int,run(['docker','compose','version','--short']).strip().lstrip('v').split('.')[:2])) >= (2,30)
        except (RuntimeError,ValueError): pass
    if not compose_ok:
        run(['apt-get','update']); run(['apt-get','install','-y','ca-certificates','curl','gnupg','zstd','iptables'])
        Path('/etc/apt/keyrings').mkdir(exist_ok=True)
        with urllib.request.urlopen('https://download.docker.com/linux/'+distro+'/gpg',timeout=60) as r: key=r.read()
        write('/etc/apt/keyrings/prodcast-docker.asc',key.decode(),0o644)
        keys=run(['gpg','--show-keys','--with-colons','/etc/apt/keyrings/prodcast-docker.asc'])
        if '9DC858229FC7DD38854AE2D88D81803C0EBFCD88' not in keys: raise RuntimeError('Docker repository key mismatch')
        write('/etc/apt/sources.list.d/prodcast-docker.sources',f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {codename}\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/prodcast-docker.asc\n',0o644)
        run(['apt-get','update']); run(['apt-get','install','-y','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin'])
    run(['systemctl','enable','--now','docker'])
    v=run(['docker','compose','version','--short']).strip().lstrip('v')
    if tuple(map(int,v.split('.')[:2]))<(2,30): raise RuntimeError('Docker Compose >=2.30 required')
    if not shutil.which('zstd') or not shutil.which('iptables'):
        run(['apt-get','update']); run(['apt-get','install','-y','zstd','iptables'])
    return {'compose':v}

def certs(folder,names,uids=None):
    folder.mkdir(parents=True,exist_ok=True); folder.chmod(0o755)
    for name in names:
        write(folder/name,P['tls'][name],0o600 if name.endswith('.key') else 0o644)
        if uids and name in uids: os.chown(folder/name,*uids[name])

def firewalld_active():
    if not shutil.which('firewall-cmd'):return False
    try:return run(['firewall-cmd','--state']).strip()=='running'
    except RuntimeError:return False


def firewalld_rules(role):
    address=C['hosts'][role]['address']
    interfaces=json.loads(run(['ip','-j','-4','addr']))
    interface=next((i['ifname'] for i in interfaces if any(a.get('local')==address for a in i.get('addr_info',[]))),None)
    if not interface:raise RuntimeError('Cannot identify the managed network interface for firewalld')
    try:zone=run(['firewall-cmd','--get-zone-of-interface='+interface]).strip()
    except RuntimeError:zone=''
    if not zone or zone=='no zone':zone=run(['firewall-cmd','--get-default-zone']).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',zone):raise RuntimeError('Invalid firewalld zone')
    rules=[]
    ports=[80,443] if role=='app' else [5432,6380] if role=='db' else [8443]
    for port in ports:
        if role=='app':sources=[None]
        elif role=='db':sources=[C['hosts'][r]['address'] for r in ['app']+worker_roles()]+([C['hosts']['ai']['address']] if port==5432 and C.get('install_ai',True) else [])
        else:sources=[C['hosts'][r]['address'] for r in ('app','ai')]
        for source in sorted(set(sources),key=str):
            prefix='rule family="ipv4" priority="-30000" '
            if source:prefix+='source address="'+source+'/32" '
            rules.append(prefix+'destination address="'+address+'/32" port port="'+str(port)+'" protocol="tcp" accept')
        if role!='app':rules.append('rule family="ipv4" priority="-29999" destination address="'+address+'/32" port port="'+str(port)+'" protocol="tcp" drop')
    state=STATE/'firewalld-rules.json';old=json.loads(state.read_text()) if state.exists() else {'rules':[]}
    for rule in rules:
        for scope in ([],['--permanent']):run(['firewall-cmd',*scope,'--zone='+zone,'--add-rich-rule='+rule])
    for rule in old['rules']:
        if old.get('zone')!=zone or rule not in rules:
            for scope in ([],['--permanent']):run(['firewall-cmd',*scope,'--zone='+old['zone'],'--remove-rich-rule='+rule])
    write(state,json.dumps({'zone':zone,'rules':rules}))


def selinux_json_mounts(services):
    if not selinux_enabled():return
    for service in services.values():
        service['volumes']=[v+(',z' if v.count(':')>=2 else ':z') if isinstance(v,str) and v.startswith(('./','/')) else v for v in service.get('volumes',[])]


def selinux_app_mounts(role='app'):
    if not selinux_enabled():return
    base=target()/role/'runtime';path=base/'site.compose.json'
    override=json.loads(path.read_text())
    # Only inspect mount declarations. Do not expand or copy env_file secrets.
    rendered=json.loads(dc(role,'config','--format','json','--no-env-resolution'))
    for name,service in rendered['services'].items():
        binds=[]
        for volume in service.get('volumes',[]):
            if volume.get('type')!='bind':continue
            source=Path(volume['source']).resolve()
            if not any(source.is_relative_to(root.resolve()) for root in (ROOT,STATE/'app-activation')):
                raise RuntimeError('Refusing to relabel a bind mount outside managed ProdCast directories')
            volume.setdefault('bind',{})['selinux']='z';binds.append(volume)
        if binds:override.setdefault('services',{}).setdefault(name,{})['volumes']=binds
    write(path,json.dumps(override))


def firewall(role):
    if firewalld_active():return firewalld_rules(role)
    if role=='app':return
    chain='PCMAN-'+role.upper(); ips=C['hosts']
    ports='5432,6380' if role=='db' else '8443'
    allowed=[ips[r]['address'] for r in (['app']+worker_roles() if role=='db' else ['app','ai'])]
    # Only our dedicated chain is flushed. It never intercepts SSH.
    script=f'#!/bin/sh\nset -eu\niptables -N {chain} 2>/dev/null || true\niptables -F {chain}\niptables -A {chain} -i lo -j ACCEPT\n'
    for ip in allowed: script+=f'iptables -A {chain} -s {ip}/32 -j ACCEPT\n'
    if role=='db' and C.get('install_ai',True): script+=f"iptables -A {chain} -s {ips['ai']['address']}/32 -p tcp --dport 5432 -j ACCEPT\n"
    script+=f'iptables -A {chain} -j DROP\niptables -C INPUT -p tcp -m multiport --dports {ports} -j {chain} 2>/dev/null || iptables -I INPUT 1 -p tcp -m multiport --dports {ports} -j {chain}\n'
    write(STATE/'firewall.sh',script,0o700)
    write('/etc/systemd/system/prodcast-manager-firewall.service','[Unit]\nDescription=ProdCast managed service port ACL\nBefore=docker.service\nAfter=network-pre.target\n[Service]\nType=oneshot\nExecStart=/bin/sh /var/lib/prodcast-manager/firewall.sh\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n',0o644)
    run(['systemctl','daemon-reload']); run(['systemctl','enable','prodcast-manager-firewall']); run([STATE/'firewall.sh'])

def psql(sql,db='postgres'):
    return dc('db','exec','-T','--user','postgres','postgres','psql','-X','-v','ON_ERROR_STOP=1','-At','-d',db,input=sql)


def configure_ai_documents():
    if P['role']!='db' or not ST.get('version') or ST.get('operation') not in ('',None,P['operation']):
        raise RuntimeError('AI Documents migration requires an installed, idle DB from the original profile')
    sql=(Path(P['stage'])/'ai-documents.sql').read_text()
    ownership="""DO $$ DECLARE t text; o text; BEGIN
      FOREACH t IN ARRAY ARRAY['documents','document_chunks'] LOOP
        SELECT pg_get_userbyid(relowner) INTO o FROM pg_class WHERE oid=to_regclass('public.' || t);
        IF o IS NOT NULL AND o NOT IN ('postgres','prodcast_ai') THEN RAISE EXCEPTION 'Unexpected AI document table owner'; END IF;
        IF o='postgres' THEN EXECUTE format('ALTER TABLE public.%I OWNER TO prodcast_ai',t); END IF;
      END LOOP; END $$;
    """
    psql('BEGIN; CREATE EXTENSION IF NOT EXISTS vector; GRANT USAGE,CREATE ON SCHEMA public TO prodcast_ai; '+ownership+
         'SET LOCAL ROLE prodcast_ai; SET LOCAL search_path=public; '+sql+' COMMIT;','prodcast_ai')
    return {'documents_schema':True,'embedding_dimension':1024}

def install_db():
    require_database_data()
    images={'postgres':PG,'redis':REDIS}
    if P.get('offline'):
        archive=offline_asset(P['offline']['db_images']);run(['docker','load','-i',archive])
        for name in images:
            meta=P['offline']['database_images'][name]
            inspected=json.loads(run(['docker','image','inspect',meta['tag']]))[0]
            if inspected['Id']!=meta['image_id']:raise RuntimeError('Offline database image identity mismatch')
            images[name]=meta['tag']
    b=ROOT/'db'; b.mkdir(exist_ok=True); b.chmod(0o755)
    for n in ('config','certs','data'): (b/n).mkdir(exist_ok=True); (b/n).chmod(0o755)
    ids={}
    for image,user in ((images['postgres'],'postgres'),(images['redis'],'redis')):
        if not P.get('offline'):run(['docker','pull',image])
        ids[user]=tuple(int(run(['docker','run','--rm','--network','none','--entrypoint','id',image,f'-{x}',user]).strip()) for x in ('u','g'))
        d=b/'data'/user; d.mkdir(exist_ok=True); d.chmod(0o700); os.chown(d,*ids[user])
    certs(b/'certs',['ca.crt','postgres.crt','postgres.key','redis.crt','redis.key'],{'postgres.key':ids['postgres'],'redis.key':ids['redis']})
    write(b/'config/postgres-admin-password',S['PG_ADMIN']); os.chown(b/'config/postgres-admin-password',*ids['postgres'])
    ip=C['hosts']['db']['address']
    write(b/'config/postgresql.conf',f"listen_addresses='127.0.0.1,{ip}'\nport=5432\nunix_socket_directories='/var/run/postgresql'\nhba_file='/etc/prodcast-config/pg_hba.conf'\npassword_encryption='scram-sha-256'\nssl=on\nssl_cert_file='/etc/prodcast-certs/postgres.crt'\nssl_key_file='/etc/prodcast-certs/postgres.key'\nssl_ca_file='/etc/prodcast-certs/ca.crt'\nssl_min_protocol_version='TLSv1.2'\nmax_connections=150\nshared_buffers='1GB'\ntimezone='UTC'\n",0o644)
    hba='local all postgres peer\nlocal all all reject\nhostnossl all all 0.0.0.0/0 reject\n'
    roles=[('prodcast_app','PW_APP','prodcast2','app'),('license_app','PW_LICENSE','license_db','app'),('prodcast_ai','PW_AI','prodcast_ai','ai')]+[(f'prodcast_worker_{int(r[6:]):02}',f'PW_W{int(r[6:]):02}','prodcast2',r) for r in worker_roles()]
    if P.get('external_activation') and not ST.get('version'):
        roles=[r for r in roles if r[0]!='license_app']
    for user,key,db,role in roles:
        if role=='ai' and not C.get('install_ai',True):continue
        hba+=f"hostssl {db} {user} {C['hosts'][role]['address']}/32 scram-sha-256\n"
    hba+='host all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n'; write(b/'config/pg_hba.conf',hba,0o644)
    write(b/'config/redis.conf',f'bind 127.0.0.1 {ip}\nprotected-mode yes\nport 0\ntls-port 6380\ntls-cert-file /etc/prodcast-certs/redis.crt\ntls-key-file /etc/prodcast-certs/redis.key\ntls-ca-cert-file /etc/prodcast-certs/ca.crt\ntls-auth-clients no\naclfile /etc/prodcast-config/users.acl\ndir /data\nappendonly yes\nappendfsync everysec\nsave 900 1\nmaxmemory 2gb\nmaxmemory-policy noeviction\n',0o644)
    password=lambda k:'#'+hashlib.sha256(S[k].encode()).hexdigest()
    acl=['user default off','user db_admin on '+password('REDIS_ADMIN')+' ~* &* +@all','user prodcast_app on '+password('REDIS_APP')+' ~* &* +@all -config -acl -debug -module -shutdown -flushall -flushdb -replicaof -slaveof -migrate']
    cmds='ping hello auth select client|setname client|setinfo quit get set setex psetex mget mset del exists expire pexpire ttl pttl incr incrby decr decrby lpush rpush lpop rpop brpop blpop llen lrange lrem sadd srem smembers scard hset hget hgetall hdel hlen hincrby zadd zrem zrange zrangebyscore zrevrangebyscore zscore zcard publish subscribe psubscribe unsubscribe punsubscribe multi exec discard watch unwatch eval evalsha script|load'
    for n in worker_numbers(): acl.append(f'user prodcast_worker_{n}_scheduler on '+password('REDIS_W'+n)+f' ~workflows* ~scenarios* ~_kombu.binding.* ~*celery.pidbox* ~celery-task-meta-* ~worker{n}.* ~celeryev* &* -@all '+' '.join('+'+x for x in cmds.split()))
    write(b/'config/users.acl','\n'.join(acl)+'\n'); os.chown(b/'config/users.acl',*ids['redis'])
    services={}
    for name,img in images.items():
        services[name]={'image':img,'network_mode':'host','restart':'unless-stopped','volumes':['./config:/etc/prodcast-config:ro','./certs:/etc/prodcast-certs:ro'],'logging':{'driver':'json-file','options':{'max-size':'10m','max-file':'5'}}}
    services['postgres'].update(environment={'POSTGRES_USER':'postgres','POSTGRES_DB':'postgres','POSTGRES_PASSWORD_FILE':'/etc/prodcast-config/postgres-admin-password','POSTGRES_INITDB_ARGS':'--auth-local=peer --auth-host=scram-sha-256','PGDATA':'/var/lib/postgresql/data/pgdata'},command=['postgres','-c','config_file=/etc/prodcast-config/postgresql.conf'],healthcheck={'test':['CMD','pg_isready','-h','/var/run/postgresql','-U','postgres'],'interval':'5s','timeout':'5s','retries':40})
    services['postgres']['volumes'].append('./data/postgres:/var/lib/postgresql/data')
    services['redis'].update(command=['redis-server','/etc/prodcast-config/redis.conf']); services['redis']['volumes'].append('./data/redis:/data')
    selinux_json_mounts(services)
    write(b/'compose.json',json.dumps({'services':services})); firewall('db')
    dc('db','up','-d','--force-recreate','--wait','--wait-timeout','240')
    # Query only after PostgreSQL has been restored; its container may be absent.
    if P.get('external_activation') and ST.get('version') and not psql("SELECT 1 FROM pg_database WHERE datname='license_db';").strip():
        roles=[r for r in roles if r[0]!='license_app']
    ST['database_initialized']=True;save()
    for user,key,db,role in roles:
        if not psql(f"SELECT 1 FROM pg_roles WHERE rolname='{user}';").strip(): psql(f"CREATE ROLE {user} LOGIN PASSWORD '{S[key]}' NOSUPERUSER NOCREATEDB NOCREATEROLE;")
    for db,owner in [(db,user) for user,key,db,role in roles if role in ('app','ai')]:
        if not psql(f"SELECT 1 FROM pg_database WHERE datname='{db}';").strip(): psql(f'CREATE DATABASE {db} OWNER {owner};')
        psql(f'REVOKE ALL ON DATABASE {db} FROM PUBLIC; GRANT CONNECT ON DATABASE {db} TO {owner};')
        psql('REVOKE CREATE ON SCHEMA public FROM PUBLIC;',db)
    psql('GRANT CONNECT ON DATABASE prodcast2 TO '+','.join('prodcast_worker_'+n for n in worker_numbers())+';')
    psql('CREATE EXTENSION IF NOT EXISTS vector;','prodcast_ai')
    # redis-cli receives password through stdin, never command-line arguments.
    pong=dc('db','exec','-T','redis','sh','-c','read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --tls --cacert /etc/prodcast-certs/ca.crt -h 127.0.0.1 -p 6380 --user db_admin PING',input=S['REDIS_ADMIN']+'\n')
    if 'PONG' not in pong: raise RuntimeError('Redis TLS check failed')
    return {'postgres':18,'redis_tls':True}

def load_packages():
    assets=Path(P['stage'])
    for k,a in P['files'].items():
        if k.startswith('offline-'):continue
        file=assets/a['name']
        if digest(file)!=a['sha256']: raise RuntimeError('Remote asset checksum mismatch')
        if k.endswith('deployment'):
            folder=target()/k.split('-')[0]; folder.mkdir(parents=True,exist_ok=True); folder.chmod(0o755)
            with tarfile.open(file) as t:
                members=t.getmembers()
                if any(not (m.isfile() or m.isdir()) or m.name.startswith('/') or '..' in Path(m.name).parts for m in members): raise RuntimeError('Unsafe deployment archive')
                if sum(m.size for m in members)>100*1024**2: raise RuntimeError('Deployment archive too large')
                # Python 3.10 on Ubuntu 22.04 may not provide tarfile's data filter.
                for member in members:
                    dest=folder/member.name
                    if not dest.resolve().is_relative_to(folder.resolve()) or any(x.is_symlink() for x in [dest,*dest.parents] if x.is_relative_to(folder)):
                        raise RuntimeError('Unsafe deployment destination')
                    if member.isdir(): dest.mkdir(parents=True,exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        with t.extractfile(member) as src,dest.open('wb') as dst: shutil.copyfileobj(src,dst)
                        dest.chmod(member.mode & 0o777 & ~0o022)
        else:
            run(['docker','load','-i',file])
            comp=k.split('-')[0]; info=P['images']['prodcast-'+comp]
            with tarfile.open(file) as t:
                meta=json.load(t.extractfile('manifest.json'))[0]; cfgbytes=t.extractfile(meta['Config']).read(); cfg=json.loads(cfgbytes)
            if 'sha256:'+hashlib.sha256(cfgbytes).hexdigest()!=info['image_id']: raise RuntimeError('Image configuration digest mismatch')
            actual=json.loads(run(['docker','image','inspect',info['transport_tag']]))[0]
            if actual['Architecture']!='amd64' or actual['Os']!='linux' or actual['RootFS']['Layers']!=cfg['rootfs']['diff_ids']: raise RuntimeError('Loaded image mismatch')
            for key in ('Cmd','Entrypoint','Env','User','WorkingDir','Labels'):
                if actual['Config'].get(key)!=cfg['config'].get(key): raise RuntimeError('Loaded image config differs')
    for part in ('app','license'):
        folder=target()/part
        if not folder.exists(): continue
        comps=('backend','frontend','gateway') if part=='app' else ('license',)
        write(folder/'packages.site.env',''.join('PRODCAST_'+n.upper()+'_IMAGE='+P['images']['prodcast-'+n]['transport_tag']+'\n' for n in comps))
    for folder in [ROOT/'releases',target()]: folder.chmod(0o755)

def ai_environment(model):
    # Pydantic decodes collection-valued env fields as JSON before validators run.
    return {'APP_ENV':'production','LOG_LEVEL':'INFO','LLM_PROVIDER':'ollama','LLM_MODEL':model,'OLLAMA_BASE_URL':'http://127.0.0.1:11434/v1','LLM_TIMEOUT_SECONDS':'90','AGENT_TIMEOUT_SECONDS':'120','RAG_ENABLED':'true','EMBEDDING_PROVIDER':'ollama','EMBEDDING_MODEL':EMBEDDING_MODEL,'EMBEDDING_DIMENSION':'1024','DOCUMENT_STORAGE_ROOT':'/app/data/documents','MCP_ALLOWED_SERVERS':json.dumps([]),'MCP_CODE_ENABLED':'false','MCP_ENGINEERING_ENABLED':'false','MCP_TEMPLATES_ENABLED':'false','BROWSER_AUTH_USERNAME':'prodcast-app','BROWSER_AUTH_PASSWORD_HASH':S['AI_HASH'],'NOTEBOOK_API_KEY':S['AI_KEY'],'DATABASE_URL':f"postgresql://prodcast_ai:{S['PW_AI']}@{C['hosts']['db']['address']}:5432/prodcast_ai?sslmode=verify-full&sslrootcert=/etc/prodcast/certs/ca.crt"}

def model_compute(active):
    gpu=any(m.get('size_vram',0)>0 for m in active)
    print('Ollama compute: '+('GPU' if gpu else 'CPU; NVIDIA GPU is not required for installation'),flush=True)
    return gpu

def install_ai():
    print('MANAGER_PROGRESS:ai-images',flush=True)
    load_packages(); base=target()/'ai'; base.mkdir(exist_ok=True); base.chmod(0o755)
    ollama=Path('/opt/prodcast-ollama/0.34.0')
    if not (ollama/'bin/ollama').exists():
        archive=offline_asset(P['offline']['ollama_runtime']) if P.get('offline') else STATE/'ollama-0.34.0.tar.zst'
        if not P.get('offline') and (not archive.exists() or digest(archive)!=OLLAMA_SHA):
            print('MANAGER_PROGRESS:ai-runtime-download',flush=True)
            with urllib.request.urlopen('https://github.com/ollama/ollama/releases/download/v0.34.0/ollama-linux-amd64.tar.zst',timeout=180) as r,archive.open('wb') as f: shutil.copyfileobj(r,f)
        if not P.get('offline') and digest(archive)!=OLLAMA_SHA: raise RuntimeError('Ollama checksum mismatch')
        ollama.mkdir(parents=True,exist_ok=True);unpack_ollama(archive,ollama)
    prepare_ollama_runtime(ollama)
    if selinux_enabled():
        for pattern,kind in [('/opt/prodcast-ollama/[^/]+/bin(/.*)?','bin_t'),('/opt/prodcast-ollama/[^/]+/lib(/.*)?','lib_t')]:
            # Modify only the two records owned by this installer; retries are safe.
            custom=run(['semanage','fcontext','-l','-C'])
            flag='-m' if any(line.split()[0]==pattern for line in custom.splitlines() if line.split()) else '-a'
            run(['semanage','fcontext',flag,'-t',kind,pattern])
        run(['restorecon','-RF',ollama])
    import pwd,grp
    try: u=pwd.getpwnam('prodcast-ollama')
    except KeyError:
        run(['useradd','--system','--create-home','--home-dir','/var/lib/prodcast-ollama','--shell','/usr/sbin/nologin','prodcast-ollama']); u=pwd.getpwnam('prodcast-ollama')
    for group in ('video','render'):
        try: grp.getgrnam(group)
        except KeyError: run(['groupadd','--system',group])
    run(['usermod','-aG','video,render','prodcast-ollama'])
    prepare_ollama_home(OLLAMA_HOME,u.pw_uid,u.pw_gid)
    if P.get('offline'):install_offline_model(u.pw_uid,u.pw_gid)
    run(['runuser','-u','prodcast-ollama','--','test','-x',ollama/'bin/ollama'])
    write('/etc/systemd/system/prodcast-managed-ollama.service','[Unit]\nDescription=ProdCast Ollama\nAfter=network-online.target\nWants=network-online.target\n[Service]\nUser=prodcast-ollama\nGroup=prodcast-ollama\nSupplementaryGroups=video render\nEnvironment=HOME=/var/lib/prodcast-ollama\nEnvironment=OLLAMA_HOST=127.0.0.1:11434\nEnvironment=OLLAMA_MODELS=/var/lib/prodcast-ollama/models\nEnvironment=OLLAMA_NO_CLOUD=1\nEnvironment=OLLAMA_NUM_PARALLEL=1\nEnvironment=OLLAMA_CONTEXT_LENGTH=4096\nExecStart=/opt/prodcast-ollama/0.34.0/bin/ollama serve\nRestart=on-failure\nRestartSec=5\nNoNewPrivileges=true\nUMask=0027\n[Install]\nWantedBy=multi-user.target\n',0o644)
    run(['systemctl','daemon-reload']); run(['systemctl','enable','--now','prodcast-managed-ollama'])
    wait_http('http://127.0.0.1:11434/api/version',service='Ollama on AI VM (127.0.0.1:11434)')
    tags=request('http://127.0.0.1:11434/api/tags')
    model='qwen3:4b-instruct'
    if not any(m['name']==model for m in tags['models']):
        if P.get('offline'):raise RuntimeError('Bundled offline AI model was not recognized; no network download will be attempted')
        print('MANAGER_PROGRESS:ai-model-download',flush=True)
        run([ollama/'bin/ollama','pull',model])
    tags=request('http://127.0.0.1:11434/api/tags')
    if not any(m['name']==EMBEDDING_MODEL for m in tags['models']):
        if P.get('offline'):raise RuntimeError('Missing qwen3-embedding:0.6b. Select a ZIP containing both AI models and retry AI; no offline download will be attempted')
        run([ollama/'bin/ollama','pull',EMBEDDING_MODEL])
    verify_embedding_cache()
    embedding=request('http://127.0.0.1:11434/api/embed',{'model':EMBEDDING_MODEL,'input':'ProdCast document library check','keep_alive':0},timeout=600)
    vectors=embedding.get('embeddings',[])
    import math
    if len(vectors)!=1 or len(vectors[0])!=1024 or any(not isinstance(v,(float,int)) or not math.isfinite(v) for v in vectors[0]):raise RuntimeError('Embedding model must return a finite 1024-dimensional vector')
    print('MANAGER_PROGRESS:ai-generation',flush=True)
    answer=request('http://127.0.0.1:11434/api/generate',{'model':model,'prompt':'Reply with OK only.','stream':False,'keep_alive':'10m'},timeout=600)
    if not answer.get('response'): raise RuntimeError('Ollama returned empty generation')
    active=request('http://127.0.0.1:11434/api/ps')['models']
    gpu=model_compute(active)
    model_digest=next(m['digest'] for m in request('http://127.0.0.1:11434/api/tags')['models'] if m['name']==model)
    if P.get('offline') and not same_model_digest(model_digest,P['offline']['model_digest']):raise RuntimeError('Offline model digest mismatch')
    if ST.get('model_digest') and not same_model_digest(ST['model_digest'],model_digest): raise RuntimeError('Existing model digest changed; restore original model cache')
    ST['model_digest']=model_digest; save()
    certs(base/'certs',['ca.crt','ai.crt','ai.key'],{'ai.key':(1000,1000)})
    env=ai_environment(model)
    write(base/'runtime.env',''.join(k+'='+v+'\n' for k,v in env.items()))
    documents=ROOT/'ai-data/documents'
    if any(p.is_symlink() for p in (ROOT,documents.parent,documents)):raise RuntimeError('Unsafe AI document storage path')
    documents.mkdir(parents=True,exist_ok=True);os.chown(documents,1000,1000);documents.chmod(0o750)
    service={'image':P['images']['prodcast-ai']['transport_tag'],'pull_policy':'never','platform':'linux/amd64','user':'1000:1000','network_mode':'host','env_file':[{'path':'./runtime.env','format':'raw'}],'command':['uvicorn','app.main:app','--host',C['hosts']['ai']['address'],'--port','8443','--ssl-certfile','/etc/prodcast/certs/ai.crt','--ssl-keyfile','/etc/prodcast/certs/ai.key'],'volumes':['./certs:/etc/prodcast/certs:ro'],'restart':'unless-stopped','security_opt':['no-new-privileges:true'],'mem_limit':'2g','cpus':2,'logging':{'driver':'json-file','options':{'max-size':'10m','max-file':'5'}}}
    service['volumes'].append(str(documents)+':/app/data/documents')
    selinux_json_mounts({'api':service})
    write(base/'compose.json',json.dumps({'services':{'api':service}})); firewall('ai'); dc('ai','up','-d','--force-recreate','--pull','never')
    response=wait_http('https://'+C['hosts']['ai']['address']+':8443/readyz',service='ProdCast AI API on AI VM (8443)',headers={'X-API-Key':S['AI_KEY']},ca=str(base/'certs/ca.crt'))
    if response.get('status')!='ready': raise RuntimeError('AI not ready')
    dc('ai','exec','-T','api','python','-c',"import tempfile; from pathlib import Path; p=Path('/app/data/documents'); t=tempfile.TemporaryDirectory(prefix='.manager-check-',dir=p); t.cleanup()")
    request('https://'+C['hosts']['ai']['address']+':8443/v1/documents',headers={'X-API-Key':S['AI_KEY']},ca=str(base/'certs/ca.crt'))
    return {'model':model,'embedding_model':EMBEDDING_MODEL,'documents':True,'digest':model_digest,'gpu':gpu,'compute':'gpu' if gpu else 'cpu'}

def configure_activation(b,env,override):
    from uuid import UUID
    a=P.get('activation') or {}
    if a.get('mode') not in ('online','offline'):raise RuntimeError('App activation is not configured')
    identity=str(UUID(P['installation_id']))
    persistent=STATE/'app-activation';persistent.mkdir(exist_ok=True);persistent.chmod(0o755)
    identity_file=persistent/'installation-id'
    if identity_file.exists() and identity_file.read_text().strip()!=identity:raise RuntimeError('Persistent App identity differs; restore original installation state')
    if not identity_file.exists():write(identity_file,identity+'\n',0o644)
    settings={'LICENSE_MODE':a['mode'],'LICENSE_ENVIRONMENT':'PROD','LICENSE_TENANT_ID':a['tenant_id'],
              'LICENSE_ENFORCEMENT_ENABLED':'true','LICENSE_SERVICE_FAIL_OPEN':'false',
              'INSTALLATION_ID_PATH':'/run/prodcast-activation/installation-id'}
    if a['mode']=='online':
        settings.update(LICENSE_SERVICE_URL=a['url'],LICENSE_SERVICE_TOKEN=a['token'])
        if a.get('ca_pem'):
            bundle=b/'certs/requests-ca-bundle.crt'
            write(bundle,bundle.read_text()+a['ca_pem'],0o644)
    else:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',a['key_id']):raise RuntimeError('Invalid public key ID')
        folder=b/'license-files';folder.mkdir(exist_ok=True);folder.chmod(0o755)
        keys=b/'license-public-keys';keys.mkdir(exist_ok=True);keys.chmod(0o755)
        write(folder/'prodcast.license',a['document'],0o644)
        write(keys/(a['key_id']+'.pem'),a['public_key'],0o644)
        settings.update(LICENSE_SERVICE_URL='',LICENSE_SERVICE_TOKEN='',LICENSE_FILE_PATH='/run/prodcast-license/prodcast.license',LICENSE_PUBLIC_KEYS_PATH='/app/license-public-keys')
    override['services']={n:{'volumes':[str(persistent)+':/run/prodcast-activation:ro']} for n in APIS+['migrate','celery-worker','celery-beat']}
    write(b/'site.compose.json',json.dumps(override))
    present={line.split('=',1)[0] for line in env.splitlines()}
    env=env.rstrip('\n')+'\n'+''.join(k+'=\n' for k in settings if k not in present)
    write(b/'runtime.env',replace_env(env,settings))


def configure_app():
    firewall('app')
    if ST.get('version') or any(k.endswith(':start') for k in ST.get('steps',{})):
        run(['docker','volume','inspect','prodcast-managed-media'])
    load_packages(); net=ipaddress.ip_network(C['app_subnet']); gateway=str(net.network_address+1)
    nets=json.loads(run(['docker','network','ls','--format','json']).replace('\n',',').rstrip(',').join(['[',']']))
    existing=[x for x in nets if x['Name']=='prodcast-managed-app']
    if not existing:
        for n in nets:
            info=json.loads(run(['docker','network','inspect',n['ID']]))[0]
            for subnet in info.get('IPAM',{}).get('Config') or []:
                if subnet.get('Subnet') and ipaddress.ip_network(subnet['Subnet']).version==4 and net.overlaps(ipaddress.ip_network(subnet['Subnet'])): raise RuntimeError('Docker subnet overlaps an existing network')
        for route in json.loads(run(['ip','-j','-4','route'])):
            if route.get('dst') and route['dst']!='default' and net.overlaps(ipaddress.ip_network(route['dst'],strict=False)): raise RuntimeError('Docker subnet overlaps host route')
        run(['docker','network','create','--subnet',str(net),'--gateway',gateway,'prodcast-managed-app'])
    else:
        info=json.loads(run(['docker','network','inspect','prodcast-managed-app']))[0]
        if info['IPAM']['Config'][0]['Subnet']!=str(net): raise RuntimeError('Existing network subnet mismatch')
    for comp in (('app',) if P.get('external_activation') else ('app','license')):
        b=target()/comp/'runtime'; b.chmod(0o755)
        certs(b/'certs',['ca.crt','site.crt','site.key','license.crt','license.key'])
        for name in ('db-ca.crt','ai-ca.crt','license-ca.crt'): write(b/'certs'/name,P['tls']['ca.crt'],0o644)
        (b/'certs/upstream-ca').mkdir(exist_ok=True); (b/'certs/upstream-ca').chmod(0o755)
        write(b/'certs/upstream-ca/prodcast-ca.crt',P['tls']['ca.crt'],0o644)
    b=target()/'app/runtime'
    system_ca=run(['docker','run','--rm','--network','none','--entrypoint','cat',P['images']['prodcast-backend']['transport_tag'],'/etc/ssl/certs/ca-certificates.crt'])
    write(b/'certs/requests-ca-bundle.crt',system_ca+P['tls']['ca.crt'],0o644)
    values=dict(S,APP_HOST=urlsplit(C['public_url']).hostname,DB_IP=C['hosts']['db']['address'],AI_IP=C['hosts']['ai']['address'],APP_IP=C['hosts']['app']['address'],TENANT=C['tenant_id'])
    env=string.Template((Path(P['stage'])/'app.env.in').read_text()).substitute(values)
    env=replace_env(env,{'PRODCAST_AI_ENABLED':'true' if C.get('install_ai',True) else 'false','PRODCAST_AI_BASE_URL':'https://'+C['hosts']['ai']['address']+':8443' if C.get('install_ai',True) else ''})
    write(b/'runtime.env',env)
    write(b/'site.env',f"PRODCAST_PUBLIC_URL={C['public_url'].rstrip('/')}\nPRODCAST_ADMIN_CLIENT_IP={C['admin_ip']}\nPRODCAST_ADMIN_DOCKER_GATEWAY_IP={gateway}\nMODEL_STORAGE_ENABLED=false\nNODEALL_ENABLED=false\nNODEALL_BASE_URL=https://disabled.example.invalid\n")
    override={'networks':{'default':{'external':True,'name':'prodcast-managed-app'}},'volumes':{'media_data':{'name':'prodcast-managed-media'},'static_data':{'name':'prodcast-managed-static'}}}
    write(b/'site.compose.json',json.dumps(override))
    if P.get('external_activation'):
        configure_activation(b,env,override)
        if P.get('app_tls'):configure_customer_https(b,P['images']['prodcast-gateway']['transport_tag'])
        selinux_app_mounts()
        dc('app','config','--quiet')
        return {'configured':True,'activation_mode':P['activation']['mode']}
    b=target()/'license/runtime'
    write(b/'runtime.env',f"DATABASE_URL=postgresql+psycopg://license_app:{S['PW_LICENSE']}@{C['hosts']['db']['address']}:5432/license_db?sslmode=verify-full&sslrootcert=/etc/prodcast/certs/db-ca.crt\nTOKEN_HASH_SALT={S['LICENSE_SALT']}\nADMIN_BOOTSTRAP_TOKEN={S['LICENSE_BOOTSTRAP']}\nADMIN_BOOTSTRAP_TOKEN_NAME=manager-bootstrap\nAUTO_MIGRATE=false\nENABLE_API_DOCS=false\nJSON_LOGS=true\nLOG_DIR=/app/logs\nLOG_LEVEL=INFO\n")
    write(b/'nginx.conf','events {}\nhttp { server_tokens off; server { listen 8443 ssl; ssl_certificate /etc/prodcast/certs/license.crt; ssl_certificate_key /etc/prodcast/certs/license.key; ssl_protocols TLSv1.2 TLSv1.3; location / { proxy_pass http://api:8100; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; } } }\n',0o644)
    proxy={'image':P['images']['prodcast-gateway']['transport_tag'],'entrypoint':['nginx'],'command':['-c','/etc/nginx/nginx.conf','-g','daemon off;'],'volumes':['./nginx.conf:/etc/nginx/nginx.conf:ro','./certs:/etc/prodcast/certs:ro'],'depends_on':{'api':{'condition':'service_healthy'}},'restart':'unless-stopped'}
    write(b/'site.compose.json',json.dumps({'services':{'license-proxy':proxy},'networks':{'default':{'external':True,'name':'prodcast-managed-app'}}}))
    selinux_app_mounts('license')
    dc('license','config','--quiet'); dc('license','run','--rm','--pull','never','migrate'); dc('license','up','-d','--force-recreate','--pull','never','--wait','--wait-timeout','180','api','license-proxy')
    headers={'Authorization':'Bearer '+S['LICENSE_BOOTSTRAP']}; url='http://127.0.0.1:8100'
    if not ST.get('company'):
        import urllib.error
        try: request(url+'/admin/companies/'+C['tenant_id'],headers=headers)
        except urllib.error.HTTPError as e:
            if e.code!=404: raise
            company={'tenant_id':C['tenant_id'],'company_name':C['company_name'],'status':'active','max_concurrent_users':C['license_users'],'max_sessions_per_user':C['license_sessions']}
            if C.get('license_expires_at'): company['expires_at']=C['license_expires_at']
            request(url+'/admin/companies',company,headers)
        ST['company']=True; save()
    token_file=STATE/'license-client.json'
    if not token_file.exists():
        if ST.get('token_request_pending'): raise RuntimeError('License token response was interrupted. Recover/revoke token using License admin before clearing token_request_pending')
        ST['token_request_pending']=True; save()
        token=request(url+'/admin/tokens',{'token_name':'manager-app','token_type':'client','allowed_tenant_id':C['tenant_id']},headers)
        write(token_file,json.dumps(token)); ST['token_request_pending']=False; save()
    token=json.loads(token_file.read_text())['token']
    if '\n' in token or '\r' in token: raise RuntimeError('Invalid client token')
    write(target()/'app/runtime/runtime.env',env+'LICENSE_SERVICE_TOKEN='+token+'\n')
    if P.get('app_tls'):
        configure_customer_https(target()/'app/runtime',P['images']['prodcast-gateway']['transport_tag'])
    selinux_app_mounts()
    dc('app','config','--quiet')
    return {'configured':True}

def migrate():
    dc('app','run','--rm','--pull','never','migrate')
    if P.get('directory') is not None:
        config=P['directory']
        code="import os,django\nos.environ.setdefault('DJANGO_SETTINGS_MODULE','mainapp.settings')\ndjango.setup()\nfrom django.db import transaction\nfrom apiapp.domains.identity.models import DirectoryConfiguration\nwith transaction.atomic():\n obj,_=DirectoryConfiguration.objects.get_or_create(pk=1)\n for key,value in "+repr(config)+".items(): setattr(obj,key,value)\n obj.full_clean()\n obj.save()\n"
        app_python(code)
    # Refresh the installed plugin catalog on upgrades as well as first install.
    dc('app','run','--rm','--no-deps','--pull','never','--entrypoint','python',
       'admin-service','manage.pyc','sync_integration_plugins','--package','prodcast-petex-plugins')
    if not ST.get('initialized'):
        for cmd in (['init_data'],['load_units']):
            dc('app','run','--rm','--no-deps','--pull','never','--entrypoint','python','admin-service','manage.pyc',*cmd)
        script="import os,django\nos.environ.setdefault('DJANGO_SETTINGS_MODULE','mainapp.settings')\ndjango.setup()\nfrom django.contrib.auth import get_user_model\nU=get_user_model()\nv="+repr({'username':C['admin_username'],'email':C['admin_email'],'password':S['ADMIN_PASSWORD']})+"\nif not U.objects.filter(username=v['username']).exists(): U.objects.create_superuser(**v)\n"
        dc('app','run','--rm','--no-deps','-T','--pull','never','--entrypoint','python','admin-service','-',input=script)
        ST['initialized']=True; save()
    return {'migrations':True}

def app_python(code,current=False):
    fn=current_dc if current else dc
    return fn('app','run','--rm','--no-deps','-T','--pull','never','--entrypoint','python','admin-service','-',input=code)

def verify():
    code="""import os,requests,psycopg2,redis
from mainapp.celery import app
with psycopg2.connect(os.environ['DATABASE_URL'],connect_timeout=10) as c:
 with c.cursor() as q:
  q.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'); assert q.fetchone()[0]
assert redis.Redis.from_url(os.environ['CELERY_BROKER_URL']).ping()
r=requests.get(os.environ['LICENSE_SERVICE_URL'],params={'tenant_id':os.environ['LICENSE_TENANT_ID']},headers={'Authorization':'Bearer '+os.environ['LICENSE_SERVICE_TOKEN']},timeout=15);r.raise_for_status();assert r.json()['status']=='active', 'License inactive'
nodes=app.control.inspect(timeout=15).active_queues() or {}
for prefix in WORKER_PREFIXES:
 matches=[q for n,q in nodes.items() if n.startswith(prefix)]
 assert len(matches)==1, 'Worker absent/duplicate'
 assert {'workflows','workflows_data','scenarios'} <= {q['name'] for q in matches[0]}
assert any(any(q['name']=='default' for q in qs) for qs in nodes.values())
"""
    code=code.replace('WORKER_PREFIXES',repr(tuple('prodcast-worker-'+n+'@' for n in worker_numbers())))
    if P.get('external_activation'):
        code='\n'.join(line for line in code.splitlines() if not line.startswith("r=requests.get(os.environ['LICENSE_SERVICE_URL']"))
        code+='\nimport os,django\nos.environ.setdefault("DJANGO_SETTINGS_MODULE","mainapp.settings")\ndjango.setup()\nfrom apiapp.domains.identity.license_guard import get_license_constraints\na=get_license_constraints()\nassert a.enabled and a.allow_login, "App activation is invalid or unavailable"\n'
    app_python(code)
    request(C['public_url'].rstrip('/')+'/readyz',ca=str(target()/'app/runtime/certs/ca.crt'))
    if P.get('app_tls'): verify_customer_https(target()/'app/runtime')
    return {'db_tls':True,'redis':True,'activation_verified':True,'workers':len(worker_roles()),'ai_check':'separate_optional_stage'}


def verify_ai():
    code="""import os,requests
assert os.environ.get('PRODCAST_AI_ENABLED','').lower()=='true', 'Enable AI in App through Install/Update first'
r=requests.get(os.environ['PRODCAST_AI_BASE_URL']+'/readyz',headers={'X-API-Key':os.environ['PRODCAST_AI_API_KEY']},verify=os.environ['PRODCAST_AI_CA_BUNDLE'],timeout=120);r.raise_for_status();assert r.json()['status']=='ready'
r=requests.get(os.environ['PRODCAST_AI_BASE_URL']+'/v1/documents',headers={'X-API-Key':os.environ['PRODCAST_AI_API_KEY']},verify=os.environ['PRODCAST_AI_CA_BUNDLE'],timeout=120);r.raise_for_status()
r=requests.post(os.environ['PRODCAST_AI_BASE_URL']+'/api/chat',auth=(os.environ['PRODCAST_AI_BASIC_AUTH_USERNAME'],os.environ['PRODCAST_AI_BASIC_AUTH_PASSWORD']),verify=os.environ['PRODCAST_AI_CA_BUNDLE'],json={'message':'Reply with OK only.'},timeout=180);r.raise_for_status();assert r.json().get('answer')
"""
    app_python(code,current=True)
    return {'ai_chat':True,'ai_documents':True}


def customer_gateway(template,hostname):
    """Add an SNI virtual host while retaining the original internal/admin listeners."""
    if not re.fullmatch(r'[a-z0-9.-]{1,253}',hostname):
        raise RuntimeError('Invalid customer HTTPS hostname')
    if 'server_names_hash_bucket_size' not in template:
        template=template.replace('http {','http {\n  server_names_hash_bucket_size 512;',1)
    start='  server {\n    listen 443 ssl;\n'
    end='  # Loopback/admin-channel listener'
    if template.count(start)!=1 or template.count(end)!=1:
        raise RuntimeError('Unsupported gateway template for customer HTTPS; release adapter update required')
    before,body=template.split(start)
    server,after=body.split(end)
    if 'ssl_certificate /etc/nginx/certs/site.crt;' not in server or 'ssl_certificate_key /etc/nginx/certs/site.key;' not in server:
        raise RuntimeError('Unsupported gateway certificate directives')
    customer=(start+server).replace('server_name _;','server_name '+hostname+';',1)
    customer=customer.replace('/etc/nginx/certs/site.crt','/etc/nginx/certs/customer.crt').replace('/etc/nginx/certs/site.key','/etc/nginx/certs/customer.key')
    customer=customer.replace('${prodcast_public_host}',hostname)
    # HTTP entry points redirect browsers to the customer name, while internal HTTPS retains its own origin.
    before=before.replace('https://${prodcast_public_host}', 'https://'+hostname)
    return before+start+server+customer+end+after


def replace_env(text,changes):
    lines=text.splitlines();found=set();out=[]
    for line in lines:
        key=line.split('=',1)[0]
        if key in changes:
            if key in found:raise RuntimeError('Duplicate managed environment setting: '+key)
            out.append(key+'='+changes[key]);found.add(key)
        else:out.append(line)
    if found!=set(changes):raise RuntimeError('Required managed HTTPS environment settings are missing')
    return '\n'.join(out)+'\n'


def configure_customer_https(base,image):
    material=P['app_tls'];host=urlsplit(material['url']).hostname
    template=run(['docker','run','--rm','--network','none','--entrypoint','cat',image,'/etc/nginx/nginx.conf.template'])
    gateway=customer_gateway(template,host)
    internal=C['public_url'].rstrip('/');internal_host=urlsplit(internal).hostname
    env=replace_env((base/'runtime.env').read_text(),{
        'PRODCAST_PUBLIC_URL':material['url'],
        'DJANGO_ALLOWED_HOSTS':','.join(dict.fromkeys([host,internal_host,C['hosts']['app']['address'],'localhost','127.0.0.1'])),
        'CORS_ALLOWED_ORIGINS':material['url']+','+internal,
        'CSRF_TRUSTED_ORIGINS':material['url']+','+internal})
    override=json.loads((base/'site.compose.json').read_text())
    gateway_service=override.setdefault('services',{}).setdefault('gateway',{})
    mount='./gateway-customer.conf:/etc/nginx/nginx.conf.template:ro'
    volumes=gateway_service.setdefault('volumes',[])
    volumes[:]=[v for v in volumes if not (isinstance(v,str) and ':/etc/nginx/nginx.conf.template:' in v)]
    volumes.append(mount)
    write(base/'gateway-customer.conf',gateway,0o644)
    write(base/'runtime.env',env)
    for name,key in [('customer.crt','certificate'),('customer.key','private_key'),('customer-ca.crt','ca')]:
        write(base/'certs'/name,material[key],0o600 if name.endswith('.key') else 0o644)
    write(base/'site.compose.json',json.dumps(override))


def verify_customer_https(base):
    material=P['app_tls'];host=urlsplit(material['url']).hostname
    context=ssl.create_default_context(cafile=str(base/'certs/customer-ca.crt'))
    # Connect to this VM, but verify the customer's DNS name and send SNI/Host.
    # This tests the actual certificate without modifying OS DNS or bypassing TLS verification.
    connection=http.client.HTTPSConnection(host,443,context=context,timeout=30)
    try:
        sock=socket.create_connection((C['hosts']['app']['address'],443),timeout=30)
        try:connection.sock=context.wrap_socket(sock,server_hostname=host)
        except Exception:sock.close();raise
        connection.request('GET','/readyz')
        response=connection.getresponse();body=response.read()
        if response.status!=200 or json.loads(body).get('status') not in ('ok','ready'):
            raise RuntimeError('Customer HTTPS readiness failed')
    finally:connection.close()
    return {'https':True,'url':material['url'],'dns_checked':False}


def apply_app_tls():
    if P['role']!='app' or not ST.get('version') or not P.get('app_tls'):
        raise RuntimeError('Customer HTTPS requires an installed App and imported PFX')
    if not re.fullmatch(r'[a-f0-9]{32}',P['operation']):
        raise RuntimeError('Invalid customer HTTPS operation ID')
    P['version']=ST['version'];base=target()/'app/runtime'
    files=['runtime.env','site.compose.json','gateway-customer.conf',
           'certs/customer.crt','certs/customer.key','certs/customer-ca.crt']
    transaction_path=STATE/'app-tls-transaction.json'
    previous=json.loads(transaction_path.read_text()) if transaction_path.exists() else {}
    if previous and not previous.get('complete') and previous['operation']!=P['operation']:
        raise RuntimeError('Resume the original customer HTTPS operation first')
    folder=STATE/'app-tls-backups'/P['operation']
    folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o700)
    manifest=folder/'files.json'
    if not manifest.exists():
        entries={}
        for name in files:
            src=base/name;entries[name]=src.exists()
            if src.exists():write(folder/name,src.read_text(),0o600)
        write(manifest,json.dumps(entries))
    entries=json.loads(manifest.read_text())
    write(transaction_path,json.dumps({'operation':P['operation'],'complete':False}))
    def restore():
        for name,exists in entries.items():
            if exists:write(base/name,(folder/name).read_text(),0o644 if name.endswith(('.crt','.conf')) else 0o600)
            elif (base/name).exists():(base/name).unlink()
    services=[*APIS,'gateway']
    def restart():dc('app','up','-d','--force-recreate','--no-deps','--pull','never','--wait','--wait-timeout','240',*services)
    try:
        container=dc('app','ps','-q','gateway').strip()
        if not re.fullmatch(r'[a-f0-9]{12,64}',container):raise RuntimeError('Running managed gateway not found')
        image=run(['docker','inspect','--format','{{.Image}}',container]).strip()
        command=json.loads(run(['docker','inspect','--format','{{json .Config.Cmd}}',container]))
        if len(command)!=3 or command[:2]!=['/bin/sh','-c'] or command[2].count("nginx -g 'daemon off;'")!=1:
            raise RuntimeError('Unsupported gateway startup command')
        configure_customer_https(base,image)
        selinux_app_mounts()
        dc('app','config','--quiet')
        # Run the real entrypoint/envsubst and syntax check before replacing live containers.
        check=command[2].replace("nginx -g 'daemon off;'",'nginx -t')
        dc('app','run','--rm','--no-deps','--pull','never','gateway','/bin/sh','-c',check)
        restart()
        result=verify_customer_https(base)
        request(C['public_url'].rstrip('/')+'/readyz',ca=str(base/'certs/ca.crt'))
    except Exception:
        if LOG:traceback.print_exc(file=LOG)
        restore()
        try:restart()
        except Exception as rollback_error:
            raise RuntimeError('HTTPS apply failed; previous files restored but App restart failed. Retry customer HTTPS and inspect operation.log') from rollback_error
        raise RuntimeError('HTTPS apply failed; previous App configuration restored. Inspect operation.log and retry')
    write(transaction_path,json.dumps({'operation':P['operation'],'complete':True}))
    return result

def pause():
    current_dc('app','stop','celery-beat','gateway')
    return {'scheduler_and_ingress':'stopped'}

def drain():
    code="""import time,os,redis
from mainapp.celery import app
r=redis.Redis.from_url(os.environ['CELERY_BROKER_URL'])
for attempt in range(120):
 i=app.control.inspect(timeout=10)
 queues=i.active_queues() or {}
 core={name for name,items in queues.items() if any(q['name']=='default' for q in items)}
 assert core, 'App consumer must respond'
 active=i.active(); reserved=i.reserved(); scheduled=i.scheduled()
 assert active and reserved is not None and scheduled is not None, 'Missing consumer responses'
 for responses in (active,reserved,scheduled):
  for prefix in WORKER_PREFIXES:
   assert sum(name.startswith(prefix) for name in responses)==1, 'Missing or duplicate configured Worker response'
  assert core <= set(responses), 'App consumer must respond'
 pending=any(active.values()) or any(reserved.values()) or any(scheduled.values())
 for q in ('default','workflows','workflows_data','scenarios'):
  for key in r.scan_iter(match=q+'*'):
   if r.type(key)==b'list' and r.llen(key): pending=True
 if not pending: break
 time.sleep(5)
else: raise RuntimeError('Queues did not drain; manual review required; no jobs were discarded')
"""
    code=code.replace('WORKER_PREFIXES',repr(tuple('prodcast-worker-'+n+'@' for n in worker_numbers())))
    app_python(code,True); return {'queues':'drained'}


def selected_worker_consumers(resume=False):
    roles=P.get('changed_workers',[])
    if not roles or any(not re.fullmatch(r'worker(?:[1-9]|1[0-6])',r) or r not in C['hosts'] for r in roles):
        raise RuntimeError('Invalid selected Worker drain scope')
    code="""import time
from mainapp.celery import app
prefixes=PREFIXES
queues=('workflows','workflows_data','scenarios')
nodes=app.control.inspect(timeout=15).active_queues() or {}
selected=[]
for prefix in prefixes:
 matches=[name for name in nodes if name.startswith(prefix)]
 assert len(matches)==1, 'Selected Worker absent/duplicate'
 selected.append(matches[0])
def consumers(resume):
 for node in selected:
  for queue in queues:
   result=app.control.broadcast('add_consumer' if resume else 'cancel_consumer',arguments={'queue':queue},destination=[node],reply=True,timeout=15)
   assert any('ok' in item.get(node,{}) for item in result), 'Worker consumer control not confirmed'
if RESUME:
 consumers(True)
else:
 try:
  consumers(False)
  for attempt in range(60):
   check=app.control.inspect(destination=selected,timeout=15)
   replies=[check.active(),check.reserved(),check.scheduled()]
   assert all(reply is not None and all(node in reply for node in selected) for reply in replies), 'Selected Worker did not respond'
   if not any(reply[node] for reply in replies for node in selected):break
   time.sleep(5)
  else:raise RuntimeError('Selected Worker jobs have not drained; no jobs were discarded')
 except Exception:
  consumers(True)
  raise
"""
    code=code.replace('PREFIXES',repr(tuple('prodcast-worker-'+f'{int(r[6:]):02}'+'@' for r in roles))).replace('RESUME',repr(resume))
    app_python(code,True)
    return {'selected_workers':roles,'consumers_resumed':resume,'app_restarted':False}

def backup():
    folder=STATE/'backups'/P['operation']; folder.mkdir(parents=True,exist_ok=True); folder.chmod(0o700)
    role=P['role']
    if role=='db':
        databases=psql('SELECT datname FROM pg_database;').split()
        for db in ('prodcast2','license_db','prodcast_ai'):
            if db not in databases:continue
            file=folder/(db+'.dump')
            with file.open('wb') as f:
                subprocess.run(['docker','compose','-p','prodcast-managed-db','-f',str(ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres','pg_dump','-Fc','-d',db],stdout=f,stderr=LOG,check=True)
            # Parse the entire archive, not just its existence. Restore drill remains a separate acceptance task.
            with file.open('rb') as src:
                subprocess.run(['docker','compose','-p','prodcast-managed-db','-f',str(ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres','pg_restore','--list'],stdin=src,stdout=subprocess.DEVNULL,stderr=LOG,check=True)
        write(folder/'roles.sql',dc('db','exec','-T','--user','postgres','postgres','pg_dumpall','--roles-only'))
        # A point-in-time Redis RDB is generated without copying a live append-only directory.
        dc('db','exec','-T','redis','sh','-c','read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --tls --cacert /etc/prodcast-certs/ca.crt -h 127.0.0.1 -p 6380 --user db_admin SAVE',input=S['REDIS_ADMIN']+'\n')
        shutil.copy2(ROOT/'db/data/redis/dump.rdb',folder/'redis.rdb')
        run(['tar','-czf',folder/'configuration.tar.gz','--exclude=data','-C',ROOT,'db'])
    else:
        if not ST.get('version'): raise RuntimeError('No installed version to back up')
        run(['tar','-czf',folder/'configuration.tar.gz','-C',old_target(),role])
        if role=='app':
            if (old_target()/'license').is_dir():run(['tar','-czf',folder/'license.tar.gz','-C',old_target(),'license'])
            if (STATE/'app-activation').is_dir():run(['tar','-czf',folder/'activation.tar.gz','-C',STATE,'app-activation'])
            volume=json.loads(run(['docker','volume','inspect','prodcast-managed-media']))[0]['Mountpoint']
            run(['tar','-czf',folder/'media.tar.gz','-C',volume,'.'])
            if (STATE/'license-client.json').exists(): shutil.copy2(STATE/'license-client.json',folder/'license-client.json')
        if role=='ai' and (ROOT/'ai-data/documents').is_dir():
            run(['tar','-czf',folder/'documents.tar.gz','-C',ROOT/'ai-data','documents'])
    shutil.copy2(STATE/'state.json',folder/'state.json')
    hashes={p.name:digest(p) for p in folder.iterdir() if p.is_file() and p.name!='checksums.json'}
    write(folder/'checksums.json',json.dumps(hashes)); return {'path':str(folder),'sha256':hashes,'model_cache_excluded':role=='ai'}

def status():
    if not ST.get('version'): return {'version':'','initialized':False}
    role=P['role']
    if role=='db': out=dc('db','ps','--format','json')
    else: out=current_dc(role,'ps','--format','json')
    # Avoid returning environment or credentials from inspect.
    return {'version':ST['version'],'containers':[{'service':j['Service'],'state':j['State'],'health':j.get('Health','')} for j in (json.loads(l) for l in out.splitlines() if l.strip())]}

def require_database_data():
    """Never turn recovery of a known database into an empty installation."""
    known=ST.get('version') or ST.get('database_initialized') or any(k.endswith(':install') for k in ST.get('steps',{}))
    if not known:return
    data=ROOT/'db/data/postgres/pgdata'
    if not all((data/name).is_file() for name in ('PG_VERSION','global/pg_control')) or not (data/'base').is_dir():
        raise RuntimeError('Existing PostgreSQL data is missing. Mount the original data disk or restore a backup; an empty database will not be created.')
    if (data/'PG_VERSION').read_text().strip()!='18':raise RuntimeError('Unexpected PostgreSQL data version; automatic recovery refused')
    redis=ROOT/'db/data/redis'
    if not redis.is_dir() or not any(redis.iterdir()):raise RuntimeError('Existing Redis data is missing. Restore its data disk before recovery; queued jobs must be preserved.')


def healthy_services(role):
    raw=dc(role,'ps','--all','--format','json').strip()
    rows=json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines() if line.strip()]
    return {row['Service'] for row in rows if row.get('State')=='running' and row.get('Health','') in ('','healthy')}


def restore_containers(role,services):
    # Recreate only missing/failed services. Named volumes and bind mounts remain.
    failed=set(services)-healthy_services(role)
    # nginx may retain old upstream IPs after a backend/frontend is recreated.
    if role=='app' and failed.intersection(APIS+['frontend']) and 'gateway' in services:failed.add('gateway')
    if failed:dc(role,'up','-d','--force-recreate','--no-deps','--pull','never','--wait','--wait-timeout','240',*sorted(failed))
    if not set(services)<=healthy_services(role):raise RuntimeError(role+': containers are not healthy after recovery; correct the cause and retry')
    return {'recreated':sorted(failed),'data_preserved':True}


def database_ready():
    psql('SELECT 1;')
    pong=dc('db','exec','-T','redis','sh','-c','read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --tls --cacert /etc/prodcast-certs/ca.crt -h 127.0.0.1 -p 6380 --user db_admin PING',input=S['REDIS_ADMIN']+'\n')
    if 'PONG' not in pong:raise RuntimeError('Redis is not ready after recovery')


def installation_healthcheck():
    """Read-only readiness of the expected runtime, not a reinstall/repair."""
    if not ST or ST.get('operation') not in ('',None,P['operation']) or ST.get('maintenance'):
        raise RuntimeError('Healthcheck ownership conflict')
    role=P['role'];steps=ST.get('steps',{})
    if ST.get('version')!=P['version'] or ST.get('manifest')!=P['manifest']:
        required=('install',) if role=='db' else ('configure','migrate','start')
        if ST.get('operation')!=P['operation'] or not all(P['operation']+':'+step in steps for step in required):
            raise RuntimeError('Expected release was not fully installed; healthcheck will not declare an older runtime complete')
    if role=='db':
        require_database_data()
        if not {'postgres','redis'}<=healthy_services('db'):raise RuntimeError('Database services are not healthy')
        database_ready()
        images={'postgres':P.get('offline',{}).get('database_images',{}).get('postgres',{}).get('tag',PG),
                'redis':P.get('offline',{}).get('database_images',{}).get('redis',{}).get('tag',REDIS)}
    elif role=='app':
        configured=[value for key,value in ST.get('step_tls',{}).items() if key.endswith(':configure')]
        if not configured or configured[-1]!=tls_revision():raise RuntimeError('Saved App configuration differs from deployed settings; restore the installed settings before reconciliation')
        required=set(APIS+['frontend','gateway','celery-worker','celery-beat'])
        if not required<=healthy_services('app'):raise RuntimeError('App services (including scheduler) are not all healthy; no services were started by this check')
        images={service:P['images']['prodcast-backend']['transport_tag'] for service in APIS+['celery-worker','celery-beat']}
        images.update(frontend=P['images']['prodcast-frontend']['transport_tag'],gateway=P['images']['prodcast-gateway']['transport_tag'])
        verify()
        if not P.get('external_activation') and not {'api','license-proxy'}<=healthy_services('license'):
            raise RuntimeError('License services are not healthy')
    else:raise RuntimeError('Unsupported core healthcheck role')
    # A healthy old container is not proof that the selected release is running.
    for service,image in images.items():
        expected=run(['docker','image','inspect','--format','{{.Id}}',image]).strip()
        ids=dc(role,'ps','-q',service).split()
        if len(ids)!=1 or run(['docker','inspect','--format','{{.Image}}',ids[0]]).strip()!=expected:
            raise RuntimeError(service+': running image does not match the selected installed release')
    return {'healthy':True,'version':P['version'],'manifest':P['manifest'],'services_restarted':False}


def repair():
    if ST.get('version')!=P['version'] or ST.get('manifest')!=P['manifest']:
        raise RuntimeError('Recovery requires the exact installed release and original profile')
    role=P['role']
    if role=='db':
        require_database_data()
        result=restore_containers('db',('postgres','redis'))
        database_ready()
        return result
    if role=='app':
        # Require the original volume; Compose must not silently make an empty one.
        run(['docker','volume','inspect','prodcast-managed-media'])
        if not P.get('external_activation'):restore_containers('license',('api','license-proxy'))
        return restore_containers('app',APIS+['frontend','gateway','celery-worker'])
    if role=='ai':return install_ai()
    raise RuntimeError('Unsupported recovery role')


def dispatch():
    action=P['action']; role=P['role']
    if action in ('healthcheck','reconcile'):
        result=installation_healthcheck()
        if action=='reconcile':
            ST.update(version=P['version'],manifest=P['manifest'],operation='');save()
        return result
    if action.startswith('worker-retire-'):
        from worker_lifecycle_linux import dispatch as lifecycle_dispatch
        return lifecycle_dispatch(sys.modules[__name__])
    if action.startswith('workers-'):
        from workers_linux import dispatch as workers_dispatch
        return workers_dispatch(sys.modules[__name__])
    if action.startswith('data-') or action.startswith('maintenance-') or action=='reset':
        from maintenance_linux import dispatch as maintenance_dispatch
        return maintenance_dispatch(sys.modules[__name__])
    if action=='app-tls':return apply_app_tls()
    if action=='ai-documents':return configure_ai_documents()
    if action=='claim': return claim()
    if action=='release-operation': return release_operation()
    if action=='bootstrap': return bootstrap()
    if action=='repair': return repair()
    if action=='install': return install_db() if role=='db' else install_ai()
    if action=='configure': return configure_app()
    if action=='migrate': return migrate()
    if action=='grants': psql(worker_grants(),'prodcast2'); return {'grants':True}
    if action=='start': dc('app','up','-d','--force-recreate','--no-deps','--pull','never','--wait','--wait-timeout','240',*APIS,'frontend','gateway','celery-worker'); return {}
    if action=='verify': return verify()
    if action=='verify-ai': return verify_ai()
    if action=='beat': return restore_containers('app',('celery-beat',))
    if action=='pause': return pause()
    if action=='drain': return drain()
    if action=='drain-workers':return selected_worker_consumers(False)
    if action=='resume-workers':return selected_worker_consumers(True)
    if action=='stop':
        current_dc('app','stop')
        if not P.get('external_activation') and (old_target()/'license').is_dir():current_dc('license','stop')
        return {}
    if action=='backup': return backup()
    if action=='status': return status()
    if action=='commit': ST.update(version=P['version'],manifest=P['manifest'],operation=''); save(); return {'version':P['version']}
    raise RuntimeError('Unsupported action')

def tls_revision():
    revision={k:v for k,v in P.get('tls',{}).items() if k.endswith('.crt')}
    if P.get('role')=='ai':revision['documents_adapter']='ai-documents-gguf-v2'
    if P.get('role')=='app':
        revision['install_ai']=C.get('install_ai',True)
        revision['ai_address']=C['hosts']['ai']['address'] if C.get('install_ai',True) else ''
        revision['activation']=P.get('activation')
        revision['external_activation']=P.get('external_activation',False)
        revision['directory']=P.get('directory')
    if P.get('app_tls'):
        revision['customer']={k:v for k,v in P['app_tls'].items() if k!='private_key'}
    return hashlib.sha256(json.dumps(revision,sort_keys=True).encode()).hexdigest()

def cached_runtime_present():
    role=P['role'];action=P['action']
    if action in ('bootstrap','repair','ai-documents','verify-ai'):return False
    if action in ('install','configure','start','migrate') and ST.get('step_tls',{}).get(P['operation']+':'+action)!=tls_revision():return False
    if action=='install' and role in ('db','ai'):
        if role=='ai' and not (OLLAMA_HOME/'models').is_dir():return False
        expected={'postgres','redis'} if role=='db' else {'api'}
        if not expected.issubset(healthy_services(role)):return False
        if role=='ai':
            try:
                response=request('https://'+C['hosts']['ai']['address']+':8443/readyz',headers={'X-API-Key':S['AI_KEY']},ca=str(target()/'ai/certs/ca.crt'),timeout=10)
                return response.get('status')=='ready'
            except Exception:return False
        try:database_ready()
        except Exception:return False
        return True
    if role=='app' and action=='configure':
        if P.get('external_activation'):return (target()/'app/runtime/runtime.env').is_file() and (STATE/'app-activation/installation-id').is_file()
        return {'api','license-proxy'}.issubset(healthy_services('license'))
    if role=='app' and action in ('start','beat'):
        expected=set(APIS+['frontend','gateway','celery-worker']) if action=='start' else {'celery-beat'}
        return expected.issubset(healthy_services('app'))
    return True

def main():
    global P,C,S,ST,LOG
    os.umask(0o077); P=json.loads(Path(sys.argv[1]).read_text()); C=P['site']; S=P['secrets']
    if P['action']=='preflight': result=preflight()
    else:
        preflight(); STATE.mkdir(exist_ok=True); STATE.chmod(0o700)
        with (STATE/'lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            ST=json.loads((STATE/'state.json').read_text()) if (STATE/'state.json').exists() else {}
            key=P['operation']+':'+P['action']
            with (STATE/'operation.log').open('a') as log:
                LOG=log
                try:cached=key in ST.get('steps',{}) and P['action'] not in ('status','claim','verify','commit','healthcheck','reconcile') and cached_runtime_present()
                except (RuntimeError,OSError,ValueError):cached=False
                if cached:
                    result=ST['steps'][key]
                else:
                    try: result=dispatch()
                    except Exception:
                        traceback.print_exc(file=log); raise
                    if P['mode'] in ('install','update','repair'):
                        ST.setdefault('steps',{})[key]=result
                        ST.setdefault('step_tls',{})[key]=tls_revision();save()
    print('MANAGER_RESULT:'+json.dumps(result))

if __name__=='__main__':
    try: main()
    except Exception as e:
        # Only errors generated by this adapter are safe to show verbatim.
        msg=str(e) if type(e) is RuntimeError else type(e).__name__+'; inspect /var/lib/prodcast-manager/operation.log'
        print('MANAGER_ERROR:'+msg,flush=True); sys.exit(1)
