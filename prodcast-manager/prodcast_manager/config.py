"""The topology is deliberately narrower than arbitrary remote execution."""
from .i18n import tr
import hashlib
import ipaddress
import json
import re
import contextlib
from pathlib import Path
from urllib.parse import urlsplit

ROLES = ('app', 'db', 'ai', 'worker1', 'worker2')
CORE_ROLES = tuple(r for r in ROLES if r != 'ai')
MAX_WORKERS=16

def worker_roles(c):
    return tuple(sorted((r for r in c['hosts'] if re.fullmatch(r'worker[1-9][0-9]*',r)),key=lambda r:int(r[6:])))

def core_roles(c):return ('app','db')+worker_roles(c)

def roles(c):return ('app','db','ai')+worker_roles(c)

def example():
    return {'schema': 1, 'site_id': 'prodcast-production', 'public_url': 'https://192.0.2.23',
            'admin_ip': '192.0.2.225', 'admin_username': 'admin', 'admin_email': 'admin@example.invalid',
            'tenant_id': 'prodcast-production', 'company_name': 'My company',
            'license_users': 5, 'license_sessions': 1, 'license_expires_at': '',
            'app_subnet': '172.30.23.0/24', 'install_gpu_driver': False, 'install_ai': True,
            '_ai_topology_address': '',
            'hosts': {r: {'address': '192.0.2.'+str(n), 'port': 22,
                          'username': 'Administrator' if r.startswith('worker') else 'ubuntu',
                          'auth': 'key', 'key_path': '', 'fingerprint': ''}
                      for r,n in zip(ROLES, (23,128,76,21,85))}}

def fresh_site():
    """Empty connection values; examples belong to visual placeholders only."""
    value=example();value['install_ai']=False
    for host in value['hosts'].values():host['address']=''
    value['public_url']='';value['admin_ip']=''
    return value


def validate(c, require_trust=False):
    if not isinstance(c.get('install_ai',True),bool): raise ValueError('install_ai must be boolean')
    if c.get('schema') != 1: raise ValueError('Unsupported site schema')
    for k in ('site_id','tenant_id','admin_username'):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', c.get(k,'')):
            raise ValueError(f'{k}: use letters, digits, dash and underscore')
    workers=worker_roles(c)
    if not 1<=len(workers)<=MAX_WORKERS or any(int(r[6:])>MAX_WORKERS for r in workers) or set(c['hosts'])!=set(roles(c)):
        raise ValueError('Configure App, DB, optional AI and 1 to 16 Workers with stable IDs from 1 to 16')
    addresses=[]
    for role,h in c['hosts'].items():
        if role=='ai' and not c.get('install_ai',True):continue
        ip=ipaddress.IPv4Address(h['address'])
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified: raise ValueError(f'{role}: invalid VM IP')
        addresses.append(str(ip))
        if not isinstance(h['port'],int) or not 1 <= h['port'] <= 65535: raise ValueError('Invalid SSH port')
        if not re.fullmatch(r'[A-Za-z0-9_.\\@-]{1,80}',h['username']): raise ValueError('Invalid SSH user')
        if h['auth'] not in ('key','password'): raise ValueError('Select key or password')
        if require_trust and role!='ai' and not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',h.get('fingerprint','')):
            raise ValueError(f'{role}: first verify and save the SSH fingerprint')
        if any(k in h for k in ('password','passphrase','sudo_password')): raise ValueError('Secrets belong in vault')
    if len(set(addresses))!=len(addresses): raise ValueError('Each enabled role requires its own VM address')
    ipaddress.IPv4Address(c['admin_ip'])
    u=urlsplit(c['public_url'])
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443) or u.path not in ('','/') or u.query or u.fragment:
        raise ValueError('Public URL must be an HTTPS origin on port 443')
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',u.hostname): raise ValueError('Invalid public hostname')
    if require_trust:
        documentation=[ipaddress.ip_network(n) for n in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')]
        endpoints={r:h['address'] for r,h in c['hosts'].items() if r!='ai'}
        endpoints.update(admin_ip=c['admin_ip'],public_url=u.hostname)
        for field,value in endpoints.items():
            try:ip=ipaddress.ip_address(value)
            except ValueError:continue
            if any(ip in network for network in documentation):
                raise ValueError(tr('{v0}: {v1} — демонстрационный адрес. Укажите актуальный IP перед подключением к серверам.').format(v0=field,v1=value))
    for k in ('company_name','admin_email'):
        if not c.get(k) or len(c[k])>200 or any(x in c[k] for x in '\r\n\x00'): raise ValueError(f'Invalid {k}')
    for k in ('license_users','license_sessions'):
        if not isinstance(c[k],int) or not 1 <= c[k] <= 100000: raise ValueError(f'Invalid {k}')
    net=ipaddress.IPv4Network(c['app_subnet'])
    if net.prefixlen!=24 or any(ipaddress.ip_address(a) in net for a in addresses): raise ValueError('App Docker subnet must be a separate /24')
    if c.get('license_expires_at'):
        from datetime import datetime
        if datetime.fromisoformat(c['license_expires_at'].replace('Z','+00:00')).tzinfo is None: raise ValueError('License expiry needs timezone')
    return c

def topology_hash(c):
    # Authentication can change without changing the managed installation.
    v={k:v for k,v in c.items() if k not in ('hosts','install_gpu_driver','install_ai','_ai_topology_address','_worker_topology_hosts')}
    v['hosts']={r:h['address'] for r,h in c['hosts'].items()}
    if '_worker_topology_hosts' in c:
        v['hosts']={r:a for r,a in v['hosts'].items() if not r.startswith('worker')}
        v['hosts'].update(c['_worker_topology_hosts'])
    # Freeze only the optional endpoint's contribution to identity. Old profiles
    # retain their original hash; enabling AI never re-identifies the core stack.
    if '_ai_topology_address' in c:v['hosts']['ai']=c['_ai_topology_address']
    return hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()

def worker_addresses(c):
    return {r:c['hosts'][r]['address'] for r in worker_roles(c)}

def validate_membership(c, data):
    """Frozen topology is accepted only with the roster approved in this vault."""
    baseline=c.get('_worker_topology_hosts')
    approved=data.get('worker_hosts')
    if baseline is not None or approved is not None:
        if (not isinstance(baseline,dict) or not baseline
                or baseline!=data.get('worker_topology_hosts')
                or worker_addresses(c)!=approved
                or data.get('topology')!=topology_hash(c)):
            raise ValueError('Worker membership differs from the approved installation profile')

def atomic_json(path, obj):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as f:
        import os
        os.chmod(tmp,0o600); json.dump(obj,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    tmp.replace(path)

@contextlib.contextmanager
def file_lock(path):
    """Advisory OS lock; crashes release it without deleting another process's lock."""
    import os
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            if os.name=='nt':f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
