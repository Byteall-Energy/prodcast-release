"""Renderer-independent API for the local desktop application."""
import copy
import hashlib
import hmac
import ipaddress
import json
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import validate, atomic_json, worker_roles, topology_hash, fresh_site
from .engine import Engine
from .i18n import set_language, rendered
from .operation_history import OperationHistory, pending_operations, filtered_events
from .portable import (data_directory, last_directory, runtime_config, save_config,
                       open_credentials, profile_directory, remember, import_site)
from .release import Release
from .session_logs import SessionLog, application_directory
from .vault import Vault


class DesktopController:
    def __init__(self, home=None):
        self.home = Path(home) if home else application_directory()
        self.data = data_directory(self.home)
        self.directory = last_directory(self.home)
        self.log = SessionLog(self.data / 'logs')
        self.history = OperationHistory(self.log.directory)
        self.lock = threading.RLock()
        self.local = threading.local()
        self.busy = False
        self.active = None
        self.dirty = False
        self.last_result = None
        self.fingerprint = None
        self.certificate = None
        self.vault_cache = None
        self.packages = dict(release='', trusted='', ai_model='', ai_ollama='', ai_action='keep')
        try: self.language = json.loads((self.data / 'ui.json').read_text('utf-8')).get('language', 'ru')
        except (OSError, ValueError): self.language = 'ru'
        if self.language not in ('ru', 'en'): self.language = 'ru'
        set_language(self.language)
        self.config = fresh_site()
        if (self.directory / 'site.json').exists(): self.config = runtime_config(self.directory / 'site.json')
        self._restore_draft()
        self.log.remote_listener = lambda role, action, text: self.emit(role + ': ' + action + '\n' + text)

    def _idle(self):
        if self.busy: raise ValueError('An operation is running. Configuration changes are locked.')

    def _vault(self, password=''):
        vault = open_credentials(self.directory, password)
        self.log.protect(vault.data)
        return vault

    def _read_vault(self):
        if not (self.directory / 'secrets.json').exists(): return {}
        signature = (str(self.directory), (self.directory / 'secrets.json').stat().st_mtime_ns,
                     (self.directory / 'secrets.key').stat().st_mtime_ns)
        if self.vault_cache and self.vault_cache[0] == signature: return copy.deepcopy(self.vault_cache[1])
        vault = Vault(self.directory / 'secrets.json', (self.directory / 'secrets.key').read_text('ascii').strip())
        self.log.protect(vault.data)
        self.vault_cache = (signature,copy.deepcopy(vault.data))
        return vault.data

    def sites(self):
        root = self.data / 'sites'
        paths = {p.parent for pattern in ('*/site.json', '*/ui-draft.json') for p in root.glob(pattern)}
        paths.add(self.directory)
        return [dict(id=p.name, installed=(p / 'site.json').exists()) for p in sorted(paths)]

    def snapshot(self):
        with self.lock:
            vault = self._read_vault()
            public_directory = {k:v for k,v in vault.get('directory', {}).items() if k != 'bind_password'}
            public_directory['password_stored'] = bool(vault.get('directory', {}).get('bind_password'))
            activation = {k:v for k,v in vault.get('activation', {}).items() if k not in ('token','document','public_key')}
            activation['token_stored'] = bool(vault.get('activation', {}).get('token'))
            activation['offline_stored'] = bool(vault.get('activation', {}).get('document'))
            tls = vault.get('app_tls')
            receipt = self._json(self.directory / 'ui-deployed.json')
            journal = self._json(self.directory / 'journal.json')
            installed = bool(vault.get('topology') and (receipt.get('version') or journal.get('status') == 'complete' or journal.get('mode') in ('update','repair')))
            applied = 'unknown'
            if receipt and (self.directory / 'site.json').exists():
                key = (self.directory / 'secrets.key').read_text('ascii').strip()
                applied = 'match' if receipt.get('signature',receipt.get('settings_signature')) == self._signature(runtime_config(self.directory / 'site.json'), vault, key) else 'pending'
            return dict(version=__version__, language=self.language, site=self.directory.name,
                        profile_path=str(self.directory), sites=self.sites(), config=copy.deepcopy(self.config),
                        packages=dict(self.packages), busy=self.busy, dirty=self.dirty,
                        installed=installed, identity_bound=bool(vault.get('topology')), active=self.active['id'] if self.active else None,
                        installed_workers=list(worker_roles(runtime_config(self.directory / 'site.json'))) if (self.directory / 'site.json').exists() else [],
                        credentials={r:{k:bool(v) for k,v in a.items()} for r,a in vault.get('ssh',{}).items()},
                        worker_services={r:{**{k:v for k,v in account.items() if k!='password'},'password_stored':bool(account.get('password'))} for r,account in vault.get('worker_services',{}).items()},
                        directory=public_directory, activation=activation,
                        certificate=dict(info=tls['info'],url=tls['url']) if tls else None,
                        installation_id=vault.get('installation_id'), pending=pending_operations(self.directory),
                        history=self.history.list(self.directory), result=copy.deepcopy(self.last_result),
                        applied=applied, receipt=receipt, log_path=str(self.log.directory))

    @staticmethod
    def _json(path):
        try:
            value = json.loads(path.read_text('utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}

    def _restore_draft(self):
        draft = self._json(self.directory / 'ui-draft.json')
        if not draft: return
        if isinstance(draft.get('config'), dict):
            self.config = draft['config']
            self.packages.update({k:v for k,v in draft.get('packages', {}).items() if k in self.packages})
        elif isinstance(draft.get('values'), dict):
            self.config.update(draft['values'])
            count = int(draft.get('worker_count', 2))
            if not 1 <= count <= 16: raise ValueError('Invalid draft worker count')
            hosts = draft.get('hosts', {})
            active = ('app','db','ai') + tuple(sorted((r for r in hosts if re.fullmatch(r'worker[1-9][0-9]*',r)),key=lambda r:int(r[6:]))[:count])
            self.config['hosts'] = {r:hosts.get(r,self.config['hosts'].get(r,{})) for r in active}
            for host in self.config['hosts'].values(): host['port'] = int(host.get('port',22))
            self.config['install_ai'] = draft.get('install_ai',False)
            for key in ('release','trusted','ai_model','ai_ollama'): self.packages[key] = draft.get(key,'')

    def update(self, payload):
        self._idle()
        value = copy.deepcopy(payload.get('config', self.config))
        # No password/key material may enter the public profile or draft.
        if set(value) - set(self.config): raise ValueError('Unknown profile fields')
        if not isinstance(value.get('hosts'),dict): raise ValueError('Invalid hosts')
        allowed = {'address','port','username','auth','key_path','fingerprint'}
        for role, host in value['hosts'].items():
            if role not in ('app','db','ai') and not re.fullmatch(r'worker(?:[1-9]|1[0-6])',role): raise ValueError('Invalid role')
            if not isinstance(host,dict) or set(host) - allowed: raise ValueError('Secrets belong in encrypted access storage')
            host['port'] = int(host.get('port',22) or 22)
        self.config = value
        self.packages.update({k:str(v) for k,v in payload.get('packages',{}).items() if k in self.packages})
        self.dirty = True
        return {'saved':False}

    def save_draft(self, payload=None):
        self._idle()
        atomic_json(self.directory / 'ui-draft.json', dict(config=self.config,packages=self.packages))
        remember(self.home,self.directory)
        self.dirty = False
        return {'saved':True,'remote_changed':False}

    def new_site(self, payload=None):
        self._idle()
        self.directory = self.data / 'sites' / ('draft-' + uuid.uuid4().hex[:8])
        self.config = fresh_site(); self.packages = dict.fromkeys(('release','trusted','ai_model','ai_ollama'),'')
        self.packages['ai_action'] = 'keep'
        self.fingerprint = self.certificate = self.last_result = None
        self.dirty = False
        return {'new':True}

    def select_site(self, payload):
        self._idle()
        identity = payload['id']
        if identity not in {item['id'] for item in self.sites()}: raise ValueError('Unknown site')
        target = (self.data / 'sites' / identity).resolve()
        if not target.is_relative_to((self.data / 'sites').resolve()): raise ValueError('Invalid site')
        config = runtime_config(target / 'site.json') if (target / 'site.json').exists() else fresh_site()
        self.directory = target; self.config = config
        self.packages = dict(release='',trusted='',ai_model='',ai_ollama='',ai_action='keep')
        self._restore_draft()
        self.fingerprint = self.certificate = self.last_result = None
        self.dirty = False; remember(self.home,target)
        return {'selected':True}

    def import_profile(self, payload):
        self._idle()
        directory, config = import_site(payload['path'], self.home)
        self.directory = directory; self.config = config; self.dirty = False
        self.packages = dict(release='',trusted='',ai_model='',ai_ollama='',ai_action='keep')
        remember(self.home,directory)
        return {'imported':True}

    def _prepare(self):
        config = validate(copy.deepcopy(self.config))
        path = self.directory / 'site.json'
        vault = self._read_vault()
        if path.exists() and vault.get('topology'):
            old = runtime_config(path)
            if old['site_id'] != config['site_id'] or any(old['hosts'][r]['address'] != config['hosts'][r]['address'] for r in ('app','db')):
                raise ValueError('Installed core identity cannot change. Use New site.')
            if worker_roles(old) != worker_roles(config) or any(old['hosts'][r]['address'] != config['hosts'][r]['address'] for r in worker_roles(old)):
                raise ValueError('Use Add / Remove worker to change installed membership.')
        if not path.exists():
            destination = profile_directory(self.home,config)
            if destination != self.directory:
                if destination.exists(): raise ValueError('This site already exists. Open its profile instead.')
                destination.mkdir(parents=True)
                for name in ('secrets.json','secrets.key'):
                    source = self.directory / name
                    if source.exists(): shutil.copy2(source,destination / name)
                self.directory = destination
        self.config = save_config(self.directory,config)
        (self.directory / 'ui-draft.json').unlink(missing_ok=True)
        remember(self.home,self.directory); self.dirty = False
        return copy.deepcopy(self.config)

    def save_profile(self, payload=None):
        self._idle(); self._prepare()
        return {'saved':True,'remote_changed':False}

    def save_access(self, payload):
        self._idle()
        role = payload['role']
        if role not in self.config['hosts']: raise ValueError('Unknown server')
        vault = self._vault(payload.get('legacy_password',''))
        access = vault.data.setdefault('ssh',{}).setdefault(role,{})
        for key,value in payload.get('secrets',{}).items():
            if key not in ('password','passphrase','sudo_password') or not isinstance(value,str): raise ValueError('Invalid credential field')
            access[key] = value
        self.log.protect(vault.data); vault.save()
        return {'saved':True,'remote_changed':False}

    def language_set(self, payload):
        if payload['language'] not in ('ru','en'): raise ValueError('Invalid language')
        self._idle(); self.language = payload['language']; set_language(self.language)
        atomic_json(self.data / 'ui.json',dict(language=self.language))
        return {'saved':True}

    def worker_service_save(self,payload):
        self._idle();role=payload['role']
        if role not in worker_roles(self.config):raise ValueError('Select a configured Worker')
        mode=payload.get('mode','preserve');account=payload.get('account','').strip()
        if mode not in ('preserve','managed','custom'):raise ValueError('Unsupported Worker service account mode')
        if mode=='custom' and (not re.fullmatch(r'[A-Za-z0-9_.@\\$-]{1,160}',account) or not ('\\' in account or '@' in account)):
            raise ValueError('Use DOMAIN\\user, user@domain or COMPUTER\\user for the service account')
        vault=self._vault();old=vault.data.get('worker_services',{}).get(role,{})
        password=payload.get('password',old.get('password','') if old.get('account')==account else '')
        if not isinstance(password,str) or any(c in password for c in '\r\n\x00'):raise ValueError('Invalid service-account password')
        vault.data.setdefault('worker_services',{})[role]=dict(mode=mode,account=account if mode=='custom' else '',password=password if mode=='custom' else '')
        self.log.protect(vault.data);vault.save();return {'saved':True,'applied':False}

    def emit(self, text):
        clean = self.log.append(rendered(text))
        item = getattr(self.local,'operation',None)
        if item: self.history.append(item,clean)

    def _job(self, mode, function):
        with self.lock:
            self._idle()
            item = self.history.begin(mode,self.directory,tuple(self.config['hosts']),self.packages['release'])
            self.busy = True; self.active = item; self.last_result = None
        def task():
            self.local.operation = item
            try:
                result = function()
                with self.lock: self.last_result = dict(operation=item['id'],mode=mode,ok=True,value=result)
            except Exception as error:
                self.emit('ERROR: ' + str(error))
                with self.lock: self.last_result = dict(operation=item['id'],mode=mode,ok=False,error=self.log.redact(str(error)))
            finally:
                try: self.history.finish(item)
                finally:
                    self.local.operation = None
                    with self.lock: self.busy = False; self.active = None
        threading.Thread(target=task,daemon=False).start()
        return {'operation':item['id']}

    def logs(self, payload):
        records = {item['id'] for item in self.history.list(self.directory)}
        identity = payload.get('id') or (self.active['id'] if self.active else None)
        if identity not in records: return {'text':'','targets':[]}
        events = self.history.events(identity)
        return dict(text='\n'.join(filtered_events(events,payload.get('target',''),payload.get('summary',False))),
                    targets=sorted({e['target'] for e in events if e.get('target')}))

    def _host(self, role):
        if role not in self.config['hosts']: raise ValueError('Unknown server')
        host = copy.deepcopy(self.config['hosts'][role])
        ip = ipaddress.IPv4Address(host['address'])
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast: raise ValueError('Invalid server IPv4')
        host['port'] = int(host['port'])
        if not 1 <= host['port'] <= 65535: raise ValueError('Invalid SSH port')
        return host

    def scan_server(self, payload):
        from .ssh import scan
        role = payload['role']; host = self._host(role); directory = self.directory
        def run():
            value = scan(host,progress=self.emit)
            self.fingerprint = dict(role=role,address=host['address'],port=host['port'],value=value,site=str(directory))
            return self.fingerprint
        return self._job('fingerprint',run)

    def trust_server(self, payload):
        self._idle()
        proof = self.fingerprint
        if not proof or proof['role'] != payload['role'] or proof['value'] != payload['fingerprint']: raise ValueError('Retrieve and compare the fingerprint first')
        host = self._host(proof['role'])
        if (host['address'],host['port'],str(self.directory)) != (proof['address'],proof['port'],proof['site']): raise ValueError('Endpoint changed; retrieve fingerprint again')
        self.config['hosts'][proof['role']]['fingerprint'] = proof['value']; self.dirty = True
        self.fingerprint = None
        return {'trusted':True}

    def check_server(self, payload):
        from .ssh import Remote
        role = payload['role']; host = self._host(role)
        if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',host.get('fingerprint','')): raise ValueError('Verify the server fingerprint first')
        auth = copy.deepcopy(self._read_vault().get('ssh',{}).get(role,{}))
        def run():
            remote = Remote(role,host,auth,self.emit)
            try:
                info = remote.probe()
                self.emit(role + ': sign-in and privileges verified')
                return {'role':role,'hostname':info.get('hostname'),'os':info.get('os')}
            finally: remote.close()
        return self._job('check-server',run)

    def verify_release(self, payload=None):
        packages = dict(self.packages); directory = self.directory
        def run():
            release = Release(packages['release'],directory / 'cache',packages['trusted'],validate_ai=False)
            management = release.doc.get('management',{})
            return dict(version=release.version,manifest=release.digest,minimum_manager=management.get('minimum_manager'),upgrade_from=management.get('upgrade_from',[]))
        return self._job('verify-package',run)

    def update_preview(self,payload=None):
        self._idle();config=validate(copy.deepcopy(self.config),True);vault=self._vault();packages=dict(self.packages);directory=self.directory
        def preview():
            from .component_updates import changed_roles,update_actions
            release=Release(packages['release'],directory/'cache',packages['trusted'],validate_ai=False)
            engine=Engine(config,vault,release,directory,self.emit,diagnostics=self.log)
            journal=self._json(directory/'journal.json')
            baseline=vault.data.get('deployment_components',{})
            legacy=not baseline or (journal.get('status') in ('running','failed') and not journal.get('selective'))
            changed=changed_roles(engine,{} if legacy else baseline)
            return dict(changed=list(changed),unchanged=[r for r in config['hosts'] if r!='ai' and r not in changed],actions=update_actions(changed),ai=packages['ai_action'],update_strategy='traditional_full_stack' if legacy else 'changed_components',healthcheck_required=False)
        return self._job('update-preview',preview)

    @staticmethod
    def _signature(config, secrets, key):
        value = dict(config={k:v for k,v in config.items() if k not in ('hosts','_worker_topology_hosts')},
                     directory=secrets.get('directory'),activation=secrets.get('activation'),app_tls=secrets.get('app_tls'))
        return hmac.new(key.encode(),json.dumps(value,sort_keys=True,ensure_ascii=False).encode(),hashlib.sha256).hexdigest()

    def run(self, payload):
        self._idle()
        mode = payload['mode']
        if mode not in ('install','update','repair','ai','status','check','backup','app-tls','healthcheck'): raise ValueError('Unsupported operation')
        config = self._prepare(); validate(config,True)
        vault = self._vault(); packages = dict(self.packages); directory = self.directory
        needs_release = mode in ('install','update','repair','ai','healthcheck')
        if needs_release and not packages['release']: raise ValueError('Select the complete release first')
        def perform():
            keep_ai = mode == 'update' and packages.get('ai_action') == 'keep'
            release = Release(packages['release'],directory / 'cache',packages['trusted'],validate_ai=False,
                              model_path='' if keep_ai else packages['ai_model'],ollama_path='' if keep_ai else packages['ai_ollama']) if needs_release else None
            engine=Engine(config,vault,release,directory,self.emit,diagnostics=self.log)
            engine.keep_ai=keep_ai
            result=engine.run(mode)
            if mode in ('install','update','healthcheck') and self._json(directory / 'journal.json').get('status') == 'complete':
                key = (directory / 'secrets.key').read_text('ascii').strip()
                atomic_json(directory / 'ui-deployed.json',dict(version=release.version,manifest=release.digest,signature=self._signature(config,vault.data,key)))
            return {'mode':mode,'finished':True,'result':result,'pending':pending_operations(directory)}
        return self._job(mode,perform)

    def add_worker_draft(self, payload=None):
        self._idle()
        used = worker_roles(self.config)
        number = next((n for n in range(1,17) if 'worker'+str(n) not in used),None)
        if number is None: raise ValueError('Maximum 16 workers')
        role = 'worker' + str(number)
        self.config['hosts'][role] = dict(address='',port=22,username='Administrator',auth='password',key_path='',fingerprint='')
        self.dirty = True
        return {'role':role}

    def set_initial_workers(self, payload):
        self._idle()
        if self._read_vault().get('topology'): raise ValueError('Use worker actions on an installed site')
        count = int(payload['count'])
        if not 1 <= count <= 16: raise ValueError('Worker count must be 1–16')
        while len(worker_roles(self.config)) < count: self.add_worker_draft()
        for role in worker_roles(self.config)[count:]: del self.config['hosts'][role]
        self.dirty = True
        return {'count':count}

    def worker_action(self, payload):
        from .workers import add_workers
        from .worker_lifecycle import run_worker_action
        self._idle()
        mode = payload['mode']; role = payload.get('role','')
        if mode not in ('add-workers','worker-repair','worker-remove'): raise ValueError('Invalid worker action')
        directory = self.directory; packages = dict(self.packages); vault = self._vault()
        target = copy.deepcopy(self.config)
        if mode != 'worker-remove' and not packages['release']: raise ValueError('Select the exact installed complete release')
        def run():
            release = Release(packages['release'],directory / 'cache',packages['trusted'],validate_ai=False) if mode != 'worker-remove' else None
            saved = add_workers(directory,target,vault,release,self.emit,diagnostics=self.log) if mode == 'add-workers' else run_worker_action(directory,role,mode,vault,release,self.emit,diagnostics=self.log)
            (directory / 'ui-draft.json').unlink(missing_ok=True)
            with self.lock: self.config = saved; self.dirty = False
            return {'finished':True,'role':role}
        return self._job(mode,run)

    def stop(self, payload=None):
        self._idle()
        config = validate(runtime_config(self.directory / 'site.json'),True)
        vault = self._vault(); directory = self.directory
        def run():
            result = Engine(config,vault,None,directory,self.emit,diagnostics=self.log).stop_previous_operation()
            return dict(result=result,remaining=pending_operations(directory))
        return self._job('stop',run)

    def directory_save(self, payload):
        from .directory import save_directory
        self._idle(); vault = self._vault()
        value = copy.deepcopy(payload)
        if not value.get('bind_dn', '').strip(): value['bind_password'] = ''
        elif 'bind_password' not in value: value['bind_password'] = vault.data.get('directory',{}).get('bind_password','')
        save_directory(vault,value); self.log.protect(vault.data)
        return {'saved':True,'applied':False}

    def directory_ca(self, payload):
        from .directory import read_ca
        path = Path(payload['path'])
        if path.stat().st_size > 262144: raise ValueError('CA file too large')
        return read_ca(path.read_bytes())

    def unlock_legacy(self, payload):
        self._idle(); self._vault(payload.get('password',''))
        return {'unlocked':True}

    def directory_probe(self, payload):
        from .directory import probe_directory
        value = copy.deepcopy(payload['directory']); vault = self._read_vault()
        if not value.get('bind_dn', '').strip(): value['bind_password'] = ''
        elif 'bind_password' not in value: value['bind_password'] = vault.get('directory',{}).get('bind_password','')
        role = self._host('app'); auth = copy.deepcopy(vault.get('ssh',{}).get('app',{}))
        password = payload.get('password',''); user = payload.get('user',''); mode = payload['mode']
        self.log.protect({'directory':value,'directory_test_password':password})
        return self._job('directory-'+mode,lambda:probe_directory(role,auth,value,mode,user,password,self.emit))

    def activation_save(self, payload):
        from .activation import installation_id, validate_activation
        self._idle(); config = self._prepare(); vault = self._vault(); vault.initialize(config)
        value = copy.deepcopy(payload)
        if value.get('mode') == 'offline':
            stored = vault.data.get('activation',{})
            value['document'] = self._public_file(value.pop('document_path'),65536) if value.get('document_path') else stored.get('document','')
            value['public_key'] = self._public_file(value.pop('public_key_path'),16384) if value.get('public_key_path') else stored.get('public_key','')
            value['tenant_id'] = json.loads(value['document'])['payload']['tenant_id']
        if 'token' not in value: value['token'] = vault.data.get('activation',{}).get('token','')
        if value.get('ca_path'): value['ca_pem'] = self._public_file(value.pop('ca_path'),262144)
        vault.data['activation'] = validate_activation(value,installation_id(vault))
        self.log.protect(vault.data); vault.save()
        return {'saved':True,'applied':False}

    def initialize_identity(self, payload=None):
        from .activation import installation_id
        self._idle(); config = self._prepare(); vault = self._vault(); vault.initialize(config)
        return {'installation_id':installation_id(vault)}

    @staticmethod
    def _public_file(path, limit):
        file = Path(path)
        if file.stat().st_size > limit: raise ValueError('File too large')
        text = file.read_text('utf-8-sig')
        if 'PRIVATE KEY-----' in text: raise ValueError('A public certificate/key is required, not a private signing key')
        return text

    def certificate_check(self, payload):
        from .app_certificate import read_pfx, validate_for_site
        self._idle()
        password = payload.get('password',''); self.log.protect({'secrets':{'pfx_password':password}})
        chain = Path(payload['ca_path']).read_bytes() if payload.get('ca_path') else b''
        material = read_pfx(Path(payload['path']).read_bytes(),password,payload['url'],chain)
        validate_for_site(material,self.config)
        identity = uuid.uuid4().hex
        self.certificate = dict(id=identity,site=str(self.directory),material=material)
        return dict(id=identity,info=material['info'],url=material['url'])

    def certificate_save(self, payload):
        from .app_certificate import store_app_certificate
        self._idle(); pending = self.certificate
        if not pending or pending['id'] != payload['id'] or pending['site'] != str(self.directory): raise ValueError('Validate this certificate first')
        vault = self._vault(); store_app_certificate(vault,self.config,pending['material']); self.log.protect(vault.data)
        self.certificate = None
        return {'saved':True,'applied':False}

    def administrator(self, payload=None):
        # Viewing initial credentials must never prepare or save an in-flight site.
        vault = self._read_vault()
        config = runtime_config(self.directory / 'site.json')
        password = vault.get('secrets',{}).get('ADMIN_PASSWORD')
        if not password: raise ValueError('Initial administrator password has not been generated yet')
        return dict(url=vault.get('app_tls',{}).get('url') or config['public_url'],username=config['admin_username'],password=password)

    def maintenance(self, payload):
        from .maintenance import Maintenance, import_backup_profile
        self._idle(); mode = payload['mode']
        if mode not in ('export','restore','import','reset-keep','reset-full'): raise ValueError('Invalid maintenance mode')
        password = payload.get('password',''); path = payload.get('path','')
        self.log.protect({'secrets':{'archive_password':password}})
        if mode in ('export','restore','import') and len(password) < 12: raise ValueError('Archive password needs at least 12 characters')
        if mode == 'reset-full' and payload.get('confirmation') != runtime_config(self.directory / 'site.json')['site_id']: raise ValueError('Type the saved site ID to confirm deletion')
        if mode == 'import':
            destination = self.data / 'sites' / ('recovered-' + uuid.uuid4().hex[:12])
            def recover():
                config = import_backup_profile(path,password,self.data / 'temporary',destination)
                with self.lock: self.directory = destination; self.config = config; self.dirty = False
                remember(self.home,destination)
                return {'profile_recovered':True,'remote_changed':False}
            return self._job('import',recover)
        config = validate(runtime_config(self.directory / 'site.json'),True)
        vault = self._vault(); directory = self.directory
        return self._job(mode,lambda:Maintenance(Engine(config,vault,None,directory,self.emit,diagnostics=self.log)).run(mode,path,password))

    def dispatch(self, action, payload=None):
        payload = payload or {}
        allowed = {'update','save_draft','new_site','select_site','import_profile','save_profile','save_access',
                   'language_set','logs','scan_server','trust_server','check_server','verify_release','run',
                   'add_worker_draft','set_initial_workers','worker_action','stop','directory_save','directory_probe',
                   'activation_save','initialize_identity','certificate_check','certificate_save','administrator','maintenance'}
        allowed.update(('directory_ca','unlock_legacy','worker_service_save','update_preview'))
        if action == 'state': return self.snapshot()
        if action not in allowed: raise ValueError('Unsupported desktop action')
        return getattr(self,action)(payload)
