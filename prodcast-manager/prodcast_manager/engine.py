from .i18n import tr, log_tr, english_log
import json
import time
import uuid
from pathlib import Path
from .config import core_roles, worker_roles, validate, topology_hash, atomic_json, file_lock, validate_membership
from .ssh import Remote
from .release import sha
from .certificates import upgrade_legacy_certificates
from .app_certificate import validate_for_site
from .worker_policy import worker_execution_policy
from .directory import app_directory

RESOURCES=Path(__file__).parent/'resources'

def plan(mode,install_ai=True,workers=None):
    workers=tuple(workers) if workers is not None else ('worker1','worker2')
    core=('app','db')+workers
    common=[(r,'preflight') for r in core]
    optional=[('ai','preflight')]+([] if mode=='check' else [('ai',mode)]) if install_ai else []
    if mode=='status': return common+[(r,'status') for r in core]+optional
    if mode=='check': return common+optional
    if mode=='backup': return common+[(r,'backup') for r in ('db','app')]+optional
    if mode=='repair':
        return common+[(r,'claim') for r in core]+[(r,'bootstrap') for r in ('db','app')]+[(r,'repair') for r in ('db','app')+workers]+[('app','verify'),('app','beat')]+[(r,'commit') for r in core]+([('ai','repair')] if install_ai else [])
    start=[(r,'claim') for r in core]
    if mode=='update':
        start += [('app','pause'),('app','drain')]+[(r,'stop') for r in workers]+[('app','stop')]
        start += [(r,'backup') for r in ('db','app')]
    start += [(r,'bootstrap') for r in ('db','app')]
    start += [('db','install'),('app','configure'),('app','migrate'),('db','grants'),('app','start')]+[(r,'install') for r in workers]+[('app','verify'),('app','beat')]
    start += [(r,'commit') for r in core]
    tail=[('ai','preflight'),('ai','claim'),('ai','bootstrap')]
    if mode=='update':tail += [('ai','backup')]
    tail += [('ai','install'),('app','verify-ai'),('ai','commit')]
    return common+start+(tail if install_ai else [])

class Engine:
    def __init__(self,c,vault,release,state_dir,log=print,remote_factory=Remote,diagnostics=None):
        self.c=validate(c,True); self.vault=vault; self.release=release
        validate_membership(c,vault.data)
        self.dir=Path(state_dir); self.dir.mkdir(parents=True,exist_ok=True)
        self.log=lambda message:log(english_log(message)); self.factory=remote_factory; self.remotes={}
        self.report=[]
        self.diagnostics=diagnostics

    def payload(self,role,operation,mode,include_files=True):
        secrets=self.vault.data.get('secrets',{}); tls=self.vault.data.get('tls',{})
        if role.startswith('worker'):
            n=f'{int(role[6:]):02}'
            allowed=['PW_W'+n,'REDIS_W'+n,'MEDIA_KEY','SIGNING_KEY','SVC_W'+n];certs=[]
        else:
            allowed={
                'db':'PG_ADMIN PW_APP PW_LICENSE PW_AI REDIS_ADMIN REDIS_APP',
                'ai':'PW_AI AI_HASH AI_KEY',
                'app':'PW_APP PW_LICENSE REDIS_APP DJANGO_KEY FERNET_KEY MODULE_KEY RESOLVE_KEY MEDIA_KEY SIGNING_KEY METRICS_KEY AI_KEY AI_BASIC LICENSE_SALT LICENSE_BOOTSTRAP ADMIN_PASSWORD'}[role].split()
            if role=='db':
                allowed += [prefix+f'{int(r[6:]):02}' for r in worker_roles(self.c) for prefix in ('PW_W','REDIS_W')]
            certs={'db':['postgres','redis'],'ai':['ai'],'app':['site','license']}[role]
        files=self.release.for_role(role) if self.release and include_files else {}
        result=dict(site=self.c,topology=topology_hash(self.c),installation_id=self.vault.data.get('installation_id',''),role=role,operation=operation,mode=mode,
                    previous_operation=getattr(self,'previous_operation',''),
                    version=self.release.version if self.release else '',
                    manifest=self.release.digest if self.release else '',
                    files={k:{'name':v.name,'sha256':sha(v) if k=='offline-model_bundle' else self.release.offline_spec['external_model']['sha256'] if k=='offline-model_blob' else self.release.assets[v.name]['sha256']} for k,v in files.items()},
                    images=self.release.images if self.release else {},
                    offline=getattr(self.release,'offline_spec',{}),
                    worker_execution_policy=worker_execution_policy(self.release),
                    secrets={k:secrets[k] for k in allowed if k in secrets},
                    tls={k:v for k,v in tls.items() if k=='ca.crt' or k.rsplit('.',1)[0] in certs},
                    app_tls=self.vault.data.get('app_tls') if role=='app' else None,
                    external_activation=getattr(self.release,'external_activation',False),
                    activation=self.vault.data.get('activation') if role=='app' else None,
                    directory=app_directory(self.vault.data.get('directory')) if role=='app' else None)
        if role.startswith('worker') and self.vault.data.get('worker_services',{}).get(role):
            result['worker_service']=dict(self.vault.data['worker_services'][role])
        return result

    def run(self,mode):
        self.reconnect_ai=False
        if mode not in ('check','status','install','update','repair','backup','app-tls','ai','healthcheck'): raise ValueError('Unknown operation')
        if mode in ('install','update','repair','ai','healthcheck') and not self.release: raise ValueError('Select a verified release')
        if mode in ('install','update','repair') and hasattr(self.release,'validate_worker_count'):
            self.release.validate_worker_count(len(worker_roles(self.c)))
        if mode in ('install','update') and self.vault.data.get('directory') is not None:
            if not self.release.doc.get('management',{}).get('directory_configuration'):
                raise ValueError('LDAP configuration requires a release with the App directory configuration contract (v0.5.0 or later).')
        with file_lock(self.dir/'operation.lock'):
            from .workers import require_no_expansion
            require_no_expansion(self.dir)
            maintenance_path=self.dir/'maintenance-journal.json'
            if mode not in ('check','status') and maintenance_path.exists() and json.loads(maintenance_path.read_text('utf-8')).get('status') in ('running','failed'):
                raise ValueError('Resume the interrupted backup/restore/reset operation before deploying')
            tls_journal=self.dir/'app-tls-journal.json'
            if mode in ('install','update','repair','healthcheck','ai') and tls_journal.exists() and json.loads(tls_journal.read_text('utf-8')).get('status') in ('running','failed'):
                raise ValueError(tr('Сначала повторите «Применить сертификат на App» для завершения предыдущей операции.'))
            if mode in ('install','update','repair','app-tls') and self.vault.data.get('app_tls'):
                validate_for_site(self.vault.data['app_tls'],self.c)
            if mode=='app-tls':
                return self._apply_app_tls()
            if mode=='healthcheck':
                from .component_updates import ExistingInstallation
                return ExistingInstallation(self).run()
            if mode=='ai':
                core_path=self.dir/'journal.json'
                core=json.loads(core_path.read_text('utf-8')) if core_path.exists() else {}
                if core.get('status')!='complete':raise ValueError(tr('Сначала завершите установку основного стека.'))
                if not self.c.get('install_ai',True):raise ValueError(tr('Включите AI на вкладке «AI». Отсутствие пакетов не означает отключение установленного AI.'))
                if self.vault.data.get('topology')!=topology_hash(self.c):raise ValueError('Use the original installation profile')
                if self.diagnostics:self.diagnostics.protect(self.vault.data)
                self.reconnect_ai=not core.get('install_ai',True)
                if self.reconnect_ai:self.log('AI is enabled in the profile but App was previously configured without AI. Restoring only AI DB access and App integration; Workers and AI models are not reset.')
                from .ai_policy import can_reconnect_existing_ai
                if self.reconnect_ai and can_reconnect_existing_ai(self.vault,self.dir):
                    from .ai_connection import reconnect_existing_ai
                    result=reconnect_existing_ai(self,core['operation'])
                else:result=self._optional_ai('update',core['operation'],allow_skip=False)
                if result.get('status')=='complete' and self.reconnect_ai:
                    core['install_ai']=True;atomic_json(core_path,core)
                report_path=self.dir/('report-'+core['operation']+'.json')
                try:
                    report=json.loads(report_path.read_text('utf-8')) if report_path.exists() else {'mode':core['mode'],'operation':core['operation'],'status':'complete','core_status':'complete'}
                    report['ai']=result;atomic_json(report_path,report)
                except (OSError,ValueError):self.log(tr('Результат AI сохранён отдельно; общий отчёт обновить не удалось.'))
                return result
            if mode in ('install','update','repair'):
                if mode=='update':
                    from .ai_policy import preserve_existing_ai
                    if preserve_existing_ai(self.c,self.vault,self.dir,self.log):self.keep_ai=True
                if mode=='repair':
                    if self.vault.data.get('topology')!=topology_hash(self.c):raise ValueError('Recovery requires the original installation profile')
                else:self.vault.initialize(self.c)
                if getattr(self.release,'external_activation',False):
                    from .activation import validate_activation, installation_id
                    if not self.vault.data.get('activation'):
                        raise ValueError(tr('Сначала сохраните онлайн или офлайн активацию App на вкладке площадки.'))
                    validate_activation(self.vault.data['activation'],installation_id(self.vault))
                    if getattr(self.release,'offline',False) and self.vault.data['activation']['mode']!='offline':
                        raise ValueError(tr('Офлайн-релиз требует выданный файл активации и публичный PEM.'))
                if mode!='repair' and upgrade_legacy_certificates(self.vault):
                    self.log(tr('Старые сертификаты дополнены AKI/SKI; ключи и CA сохранены. Зашифрованная копия прежнего vault находится в history.'))
            elif mode=='backup' or (mode=='status' and self.vault.data.get('topology')):
                if self.vault.data.get('topology')!=topology_hash(self.c): raise ValueError('Use the original site file and vault for this installation')
            if self.diagnostics:self.diagnostics.protect(self.vault.data)
            if mode=='update' and getattr(self.release,'assets',None):
                from .component_updates import selective_update
                path=self.dir/'journal.json'
                prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
                legacy_pending=prior.get('status') in ('running','failed') and not prior.get('selective')
                if (prior.get('selective') and prior.get('status') in ('running','failed')) or (self.vault.data.get('deployment_components') and not legacy_pending):
                    return selective_update(self)
                self.log('No component baseline or a legacy operation is pending: using the traditional full-stack update. Healthcheck is optional. App, DB and all Workers are in scope; normal ownership, compatibility and resume checks still apply.')
            return self._run(mode)

    def _apply_app_tls(self):
        if not self.vault.data.get('app_tls'):
            raise ValueError(tr('Сначала импортируйте PFX на вкладке «Сертификат App».'))
        if self.vault.data.get('topology')!=topology_hash(self.c):
            raise ValueError(tr('Нужны исходные site.json и vault уже установленной площадки.'))
        path=self.dir/'journal.json'
        if path.exists() and json.loads(path.read_text('utf-8')).get('status') in ('running','failed'):
            raise ValueError(tr('Сначала завершите прерванную установку или обновление.'))
        path=self.dir/'app-tls-journal.json'
        journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
        operation=journal.get('operation') if journal.get('status') in ('running','failed') else uuid.uuid4().hex
        if self.diagnostics:self.diagnostics.protect(self.vault.data)
        r=self.factory('app',self.c['hosts']['app'],self.vault.data.get('ssh',{}).get('app',{}),self.log)
        r.diagnostics=self.diagnostics
        try:
            self.log(tr('app: проверка SSH перед применением сертификата'))
            r.probe();r.prepare_stage()
            r.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
            payload=self.payload('app',operation,'app-tls')
            status=r.action(payload,'preflight',RESOURCES)
            if not status.get('managed') or not status.get('version'):
                raise ValueError(tr('Сначала завершите установку. При первой установке импортированный PFX применяется автоматически.'))
            journal={'operation':operation,'status':'running','url':self.vault.data['app_tls']['url']}
            atomic_json(path,journal)
            self.log(tr('app: установка сертификата и перезапуск веб-сервисов; возможна краткая недоступность сайта'))
            result=r.action(payload,'app-tls',RESOURCES)
            journal.update(status='complete',result=result);atomic_json(path,journal)
            self.log(tr('app: сертификат применён; HTTPS проверен для ')+journal['url'])
            self.log(tr('Для браузеров нужен DNS клиента и доверие к CA организации. Проверка сервера не меняет настройки ПК.'))
            return result
        except Exception:
            if journal.get('status')=='running':
                journal['status']='failed';atomic_json(path,journal)
            self.log(tr('Сертификат сохранён в vault. Устраните причину и повторите «Применить сертификат на App».'))
            raise
        finally:
            try:r.cleanup()
            finally:r.close()

    def stop_previous_operation(self):
        """Stop core and optional AI journals independently, preserving recovery data."""
        core=self._stop_previous_core_operation()
        path=self.dir/'ai-journal.json'
        with file_lock(self.dir/'operation.lock'):
            journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
            if journal.get('status') not in ('running','failed'):
                return core
            operation=journal.get('operation')
            if not operation:
                raise ValueError('AI journal has no operation ID; preserve the journal for recovery')
            if journal.get('ai_address',self.c['hosts']['ai']['address'])!=self.c['hosts']['ai']['address']:
                raise ValueError('AI: stop the pending operation using its original VM address')
            remote=None
            try:
                remote=self.factory('ai',self.c['hosts']['ai'],self.vault.data.get('ssh',{}).get('ai',{}),self.log)
                remote.diagnostics=self.diagnostics
                remote.probe();remote.prepare_stage()
                remote.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
                payload=self.payload('ai',operation,'stop-operation')
                state=remote.action(payload,'preflight',RESOURCES)
                owner=state.get('operation')
                if owner not in (None,'',operation):
                    raise RuntimeError('AI VM is owned by a different operation; refusing to release it')
                result=remote.action(payload,'release-operation',RESOURCES) if owner else {'released':False,'reason':'no operation owner'}
                history=self.dir/'history';history.mkdir(exist_ok=True)
                archived=history/('ai-journal-'+operation+'.json')
                if not archived.exists():atomic_json(archived,journal)
                journal.update(status='stopped',stopped_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),stop_reason='manual')
                journal.pop('inflight',None);atomic_json(path,journal)
            finally:
                if remote:
                    try:remote.cleanup()
                    except Exception:self.log('AI: temporary staging cleanup failed; recovery journal retained')
                    finally:remote.close()
        self.log('Pending AI operation stopped. Installed AI, core stack, vault and recovery history preserved.')
        return {'status':'stopped','operation':operation,'core':core,'released':core.get('released',[])+[{'role':'ai','result':result}]}

    def _stop_previous_core_operation(self):
        """Release a stale deployment owner without deleting recovery data."""
        path=self.dir/'journal.json'
        journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
        status=journal.get('status')
        if status not in ('running','failed') or not journal.get('operation'):
            return {'status':'idle','message':tr('Незавершённой операции для остановки нет.')}
        operation=journal['operation'];errors=[];released=[]
        with file_lock(self.dir/'operation.lock'):
            for role in core_roles(self.c):
                remote=None
                try:
                    remote=self.factory(role,self.c['hosts'][role],self.vault.data.get('ssh',{}).get(role,{}),self.log)
                    remote.diagnostics=self.diagnostics
                    remote.probe();remote.prepare_stage()
                    resource='worker.ps1' if role.startswith('worker') else 'linux.py'
                    remote.put_bytes(resource,(RESOURCES/resource).read_bytes())
                    # The agent validates the owner against the operation ID
                    # carried in the payload. Send the ID observed on each VM,
                    # after verifying it still matches the failed journal.
                    probe=remote.action(self.payload(role,operation,'stop-operation'),'preflight',RESOURCES)
                    remote_operation=probe.get('operation')
                    if remote_operation in (None,''):
                        released.append({'role':role,'result':{'released':False,'reason':'no operation owner'}})
                        continue
                    release_payload=self.payload(role,operation,'stop-operation')
                    # The local journal can be older than the owner recorded on
                    # a VM. Preflight has verified site, topology, and vault;
                    # release the exact owner observed there.
                    release_payload['operation']=remote_operation
                    result=remote.action(release_payload,'release-operation',RESOURCES)
                    released.append({'role':role,'result':result})
                except Exception as error:
                    errors.append(role+': '+str(error))
                finally:
                    if remote:
                        try:remote.cleanup()
                        except Exception:pass
                        try:remote.close()
                        except Exception:pass
            if errors:
                raise RuntimeError(tr('Не удалось остановить предыдущую операцию на: ')+', '.join(errors))
            history=self.dir/'history';history.mkdir(exist_ok=True)
            archived=history/('journal-'+operation+'.json')
            if not archived.exists():atomic_json(archived,journal)
            journal.update(status='stopped',stopped_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),stop_reason='manual')
            journal.pop('inflight',None);atomic_json(path,journal)
        self.log(tr('Предыдущая операция остановлена. Vault и журналы сохранены; новый релиз разрешён.'))
        return {'status':'stopped','operation':operation,'released':released}

    def _run(self,mode):
        path=self.dir/'journal.json'
        journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
        mutating=mode in ('install','update','repair')
        digest=self.release.digest if self.release else ''
        same_failed_release=(journal.get('status')=='failed' and journal.get('manifest')==digest)
        pending=journal.get('status')=='running' or same_failed_release
        if mutating and pending and journal.get('phase')=='applying' and 'ai_address' in journal:
            if (journal.get('install_ai'),journal['ai_address'])!=(self.c.get('install_ai',True),self.c['hosts']['ai']['address']):
                raise ValueError('Finish the interrupted operation before changing AI settings.')
        self.previous_operation = journal.get('operation','') if journal.get('status') in ('failed','stopped','cancelled') and not same_failed_release else ''
        switching=False
        if mutating and pending:
            if journal['mode']!=mode or journal['manifest']!=digest:
                if journal.get('steps') or journal.get('phase')=='applying' or journal.get('inflight'):
                    raise ValueError(tr('Есть незавершённая операция «{v0}» с начатыми серверными шагами. Продолжите её с исходным релизом; смена режима сейчас заблокирована.').format(v0=journal['mode']))
                switching=True
                self.log(tr('Проверю основной стек перед сменой режима незавершённой предварительной проверки. Старый журнал пока сохранён.'))
            operation=journal['operation']
        else: operation=uuid.uuid4().hex
        if mutating and not switching:
            if journal.get('status') in ('failed','stopped','cancelled') and not same_failed_release and journal.get('operation'):
                history=self.dir/'history';history.mkdir(exist_ok=True)
                archived=history/('journal-'+journal['operation']+'.json')
                if not archived.exists():atomic_json(archived,journal)
                self.log(tr('Предыдущая операция завершилась ошибкой; её журнал сохранён в history. Новый запуск разрешён.'))
            # Carry an interrupted pre-0.1.12 AI transaction into its own journal,
            # including when the user disables AI while finishing the core.
            ai_path=self.dir/'ai-journal.json'
            old_ai=[s for s in journal.get('steps',[]) if s.get('role')=='ai']
            old_inflight=journal.get('inflight') or {}
            if pending and not ai_path.exists() and (old_ai or old_inflight.get('role')=='ai'):
                legacy_ai=dict(operation=operation,manifest=digest,version=self.release.version,status='failed',steps=old_ai)
                if old_inflight.get('role')=='ai':legacy_ai['inflight']=old_inflight['action']
                atomic_json(ai_path,legacy_ai)
            journal=dict(journal if pending else {},operation=operation,mode=mode,manifest=digest,status='running',steps=journal.get('steps',[]) if pending else [],phase=journal.get('phase','unknown') if pending else 'preflight',install_ai=self.c.get('install_ai',True),ai_address=self.c['hosts']['ai']['address'])
            atomic_json(path,journal)
        preflight_complete=False
        applying=False
        try:
            # Check every machine before staging credentials or doing installation work.
            statuses={}
            for role in core_roles(self.c):
                self.log(tr('{v0}: проверка SSH и прав').format(v0=role))
                r=self.factory(role,self.c['hosts'][role],self.vault.data.get('ssh',{}).get(role,{}),self.log)
                r.diagnostics=self.diagnostics
                self.remotes[role]=r; info=r.probe()
                if getattr(self.release,'offline',False):self.release.select_platform(role,info['os'])
                if info['free']<8*1024**3: raise RuntimeError(f'{role}: need at least 8 GiB free before staging')
                r.prepare_stage()
                for name in (('worker.ps1','worker-service-runner.py') if role.startswith('worker') else ('linux.py','app.env.in','worker-grants.sql')):
                    r.put_bytes(name,(RESOURCES/name).read_bytes())
                result=r.action(self.payload(role,operation,mode),'preflight',RESOURCES)
                if mutating and result.get('maintenance') not in (None,'',operation):
                    raise RuntimeError(role+': complete the pending maintenance / Worker expansion operation first')
                statuses[role]=result; self.log(tr('{v0}: проверка пройдена').format(v0=role))
            preflight_complete=True
            if switching:
                # A rejected Install on an existing stack is only a preflight.
                # Permit Update after every host explicitly confirms a committed,
                # consistent release and no outstanding remote operation.
                # A failed preflight has not claimed or changed a server. Once
                # every VM independently confirms a managed installation with
                # an allowed source version and no remote operation, archive
                # the stale local journal even if its old manifest differs.
                # This is important after correcting a release manifest (for
                # example, adding v0.5.0 to upgrade_from): the old failed
                # preflight must not force the operator to resume a bad hash.
                preflight_only=(journal.get('phase')=='preflight' and not journal.get('steps') and not journal.get('inflight'))
                if preflight_only:
                    # The regular update/install checks below remain the source
                    # of truth for VM state and upgrade_from. The old journal is
                    # only a local record of a read-only failed preflight.
                    committed_update=True
                else:
                    committed_update=False
                if any(s.get('managed') or s.get('version') for s in statuses.values()) and not committed_update:
                    raise RuntimeError(tr('На одной из VM есть состояние установки. Смена режима небезопасна: продолжите исходную операцию с исходным релизом. Журнал сохранён.'))
                atomic_json(self.dir/'history'/('journal-'+journal['operation']+'.json'),journal)
                operation=uuid.uuid4().hex
                journal=dict(operation=operation,mode=mode,manifest=digest,status='running',steps=[],phase='preflight',install_ai=self.c.get('install_ai',True),ai_address=self.c['hosts']['ai']['address'])
                atomic_json(path,journal);switching=False;pending=False
                self.log(tr('Предыдущая попытка остановилась до изменений. Все серверы подтвердили завершённую установку; обновление разрешено. Журнал проверки сохранён в history.') if committed_update else tr('Основной стек подтвердил отсутствие установки Manager. Предыдущий журнал сохранён в history; выбранный режим разрешён.'))
            if mode=='repair':
                if not all(s.get('managed') and s.get('version')==self.release.version and s.get('manifest')==digest for s in statuses.values()):
                    raise RuntimeError(tr('Для восстановления выберите точный установленный релиз и исходную площадку.'))
            if mode=='update':
                for role,s in statuses.items():
                    if not s.get('managed'): raise RuntimeError(tr('{v0}: установка Manager не найдена. Для первичного развёртывания выберите «Установить» с релизом v0.2. Обновление доступно после завершения установки.').format(v0=role))
                    if s.get('version') not in self.release.allowed_from+[self.release.version]: raise RuntimeError(f'{role}: unsupported upgrade source')
            if mode=='install' and any(s.get('version') for s in statuses.values()) and (not pending or journal.get('phase')=='preflight'):
                raise RuntimeError(tr('Установка уже существует. Выберите «Обновить»: текущая проверка не изменяла серверы.'))
            if mode=='backup' and not all(s.get('managed') for s in statuses.values()):
                raise RuntimeError(tr('Резервное копирование доступно после установки Manager на App, DB и выбранных Workers. Сначала завершите «Установить».'))
            if mutating:
                for role,r in self.remotes.items():
                    for f in self.release.for_role(role).values(): r.put(f)
            if mode=='update' and getattr(self.release,'assets',None):
                for role in worker_roles(self.c):
                    self.remotes[role].action(self.payload(role,operation,mode),'service-preflight',RESOURCES)
            for role,action in plan(mode,False,worker_roles(self.c))[len(core_roles(self.c)):]:
                self.log(f'{role}: {action}')
                if mutating:
                    applying=True;journal['phase']='applying';journal['inflight']={'role':role,'action':action}
                    atomic_json(path,journal)
                if mode=='status' and not statuses[role].get('managed'):
                    result={'managed':False,'version':'','status':'not_installed_by_manager'}
                    self.log(tr('{v0}: установка Manager не найдена — доступен режим «Установить».').format(v0=role))
                else:result=self.remotes[role].action(self.payload(role,operation,mode),action,RESOURCES)
                event={'time':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'role':role,'action':action,'result':result}
                self.report.append(event)
                if mutating:
                    journal['steps'].append(event);journal.pop('inflight',None); atomic_json(path,journal)
                self.log(f'{role}: {action} — OK')
            if mutating:
                journal['status']='complete'; atomic_json(path,journal)
                from .component_updates import save_receipt
                save_receipt(self)
            core_report={'mode':mode,'operation':operation,'hosts':statuses,'steps':list(self.report),'status':'complete','core_status':'complete'}
            atomic_json(self.dir/('report-'+operation+'.json'),core_report)
            if 'tls' in self.vault.data:
                (self.dir/'prodcast-ca.crt').write_text(self.vault.data['tls']['ca.crt'],encoding='ascii')
            self.log(tr('Основной стек: успешно. App, DB и Workers проверены; результат сохранён.') if mutating else tr('Операция для App, DB и Workers завершена; результат сохранён.'))
            # Core is durably complete before any AI connection, upload or check.
            try:optional=self._optional_ai(mode,operation,True)
            except Exception as error:
                optional={'status':'warning','error':str(error)}
                self.log(tr('ПРЕДУПРЕЖДЕНИЕ AI: ')+str(error))
            core_report['ai']=optional
            try:atomic_json(self.dir/('report-'+operation+'.json'),core_report)
            except OSError:self.log(tr('Не удалось дополнить отчёт результатом AI. Успешный журнал основного стека сохранён.'))
            label={'complete':log_tr('успешно'),'disabled':log_tr('отключён'),'skipped':log_tr('пропущен — существующая установка сохранена'),'warning':log_tr('требует внимания'),'checked':log_tr('проверен'),'not_installed':log_tr('не установлен')}.get(optional['status'],optional['status'])
            self.log(tr('Готово. Основной стек: успешно. AI: ')+label+'.')
            return self.report
        except Exception:
            if mutating and not switching:
                journal['status']='failed'; atomic_json(path,journal)
            atomic_json(self.dir/('report-'+operation+'.json'),{'mode':mode,'operation':operation,'steps':self.report,'status':'failed'})
            if not applying:
                self.log(tr('Предварительная проверка остановлена. Основные шаги установки/обновления этого запуска ещё не выполнялись. Сохраните vault и журнал, устраните причину и повторите ту же операцию.'))
            else:
                self.log(tr('Операция завершилась ошибкой. Vault и журнал сохранены; исправленный релиз можно запустить заново. Если VM удерживает старого владельца, нажмите «Остановить предыдущую операцию». Откат БД автоматически не выполняется.'))
            raise
        finally:
            self.previous_operation = ''
            for r in self.remotes.values():
                try: r.cleanup()
                except Exception: self.log(tr('Не удалось очистить временный inbox на одном из серверов; удалите его после проверки операции.'))
                finally:
                    try:r.close()
                    except Exception:pass

    def _optional_ai(self,mode,core_operation,allow_skip=False):
        if not self.c.get('install_ai',True):
            self.log(tr('AI отключён: подключение, установка и проверки пропущены. Существующие службы AI не удаляются.'))
            return {'status':'disabled'}
        path=self.dir/'ai-journal.json';journal={};remote=None;app_remote=None;db_remote=None
        spec=getattr(self.release,'offline_spec',{})
        external=bool(spec.get('external_ollama') or spec.get('external_model'))
        missing_selection=not getattr(self.release,'model_path',None) and not getattr(self.release,'ollama_path',None)
        if allow_skip and mode=='update' and (getattr(self,'keep_ai',False) or external and missing_selection):
            prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
            pending=prior.get('status') in ('running','failed')
            started=bool(prior.get('steps') or prior.get('inflight'))
            if pending and not started:
                history=self.dir/'history';history.mkdir(exist_ok=True)
                archived=history/('ai-journal-skipped-'+str(prior.get('operation',core_operation))+'.json')
                if not archived.exists():atomic_json(archived,prior)
                prior.update(status='skipped',skip_reason='packages_not_selected',skipped_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
                atomic_json(path,prior)
            self.log(tr('AI: пакеты не выбраны; обновление пропущено, существующая установка сохранена.'))
            if pending and started:
                self.log(tr('Предыдущая операция AI уже начиналась: журнал восстановления сохранён. При необходимости повторите или остановите её отдельно.'))
            return {'status':'skipped','reason':'packages_not_selected','pending_recovery_preserved':pending and started}
        mutating=mode in ('install','update','repair')
        try:
            journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
            pending=journal.get('status') in ('running','failed')
            if mutating and pending and journal.get('ai_address',self.c['hosts']['ai']['address'])!=self.c['hosts']['ai']['address']:
                raise ValueError('AI: resume the interrupted AI operation with its original VM address first.')
            if mutating and pending and journal.get('manifest')!=self.release.digest:
                if journal.get('steps') or journal.get('inflight'):
                    raise ValueError(tr('AI: сначала повторите незавершённый этап AI с исходным релизом через «Повторить AI». Основной стек уже готов.'))
                pending=False
            operation=journal['operation'] if pending else core_operation
            if mutating:
                journal=dict(journal if pending else {},operation=operation,manifest=self.release.digest,version=self.release.version,status='running',ai_address=self.c['hosts']['ai']['address'],steps=journal.get('steps',[]) if pending else [])
                atomic_json(path,journal)
                files=self.release.for_role('ai')  # Validate optional assets only here.
            host=self.c['hosts']['ai']
            import ipaddress,re
            if any(ipaddress.ip_address(host['address']) in ipaddress.ip_network(n) for n in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',host.get('fingerprint','')):
                raise ValueError(tr('Укажите адрес AI и подтвердите его SSH-отпечаток на вкладке серверов.'))
            self.log(tr('AI: отдельный заключительный этап; ошибки не изменяют успех основного стека.'))
            remote=self.factory('ai',host,self.vault.data.get('ssh',{}).get('ai',{}),self.log)
            remote.diagnostics=self.diagnostics
            info=remote.probe()
            if getattr(self.release,'offline',False):
                self.release.select_platform('ai',info['os']);files=self.release.for_role('ai')
            needed=20 if getattr(self.release,'offline',False) else 8
            if info['free']<needed*1024**3:raise RuntimeError(f'AI: need at least {needed} GiB free before staging')
            remote.prepare_stage();remote.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
            # Probe without update assumptions; an optional AI may not exist yet.
            status=remote.action(self.payload('ai',operation,'check'),'preflight',RESOURCES)
            if not mutating:
                if mode=='check':return {'status':'checked'}
                result=remote.action(self.payload('ai',operation,mode),mode,RESOURCES) if status.get('managed') else {'managed':False,'status':'not_installed_by_manager'}
                self.report.append({'role':'ai','action':mode,'result':result})
                return {'status':'complete' if status.get('managed') else 'not_installed','result':result}
            if status.get('version') and status['version'] not in self.release.allowed_from+[self.release.version]:raise ValueError('AI: unsupported upgrade source')
            if mode=='repair' and (status.get('version')!=self.release.version or status.get('manifest')!=self.release.digest):
                raise ValueError('AI: recovery requires its exact installed release. Use Retry AI to finish an incomplete installation.')
            ai_mode='repair' if mode=='repair' else ('update' if status.get('version') else 'install')
            payload=self.payload('ai',operation,ai_mode)
            remote.action(payload,'preflight',RESOURCES)  # Ownership and pending-operation guards.
            for file in files.values():remote.put(file)
            actions=['claim','bootstrap','repair'] if mode=='repair' else ['claim','bootstrap']+(['backup'] if status.get('version') else [])+['install']
            for action in actions:
                if action in ('install','repair'):
                    # Optional AI owns its additive schema migration, including
                    # Retry AI. Do not restart App/Workers or alter DB ownership.
                    db_remote=self.factory('db',self.c['hosts']['db'],self.vault.data.get('ssh',{}).get('db',{}),self.log)
                    db_remote.diagnostics=self.diagnostics;db_remote.probe();db_remote.prepare_stage()
                    db_remote.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
                    if getattr(self,'reconnect_ai',False):
                        journal['inflight']='ai-access';atomic_json(path,journal)
                        db_remote.action(self.payload('db',operation,'check',include_files=False),'ai-access',RESOURCES)
                    journal['inflight']='documents-schema';atomic_json(path,journal);self.log('db: ai-documents')
                    result=db_remote.action(self.payload('db',operation,'check'),'ai-documents',RESOURCES)
                    journal['steps'].append({'action':'documents-schema','result':result});journal.pop('inflight',None);atomic_json(path,journal)
                journal['inflight']=action;atomic_json(path,journal);self.log('ai: '+action)
                result=remote.action(payload,action,RESOURCES)
                journal['steps'].append({'action':action,'result':result});journal.pop('inflight',None);atomic_json(path,journal)
                self.log('ai: '+action+' — OK')
            journal['inflight']='verify-from-app';atomic_json(path,journal)
            app_remote=self.factory('app',self.c['hosts']['app'],self.vault.data.get('ssh',{}).get('app',{}),self.log)
            app_remote.diagnostics=self.diagnostics;app_remote.prepare_stage()
            app_remote.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
            if getattr(self,'reconnect_ai',False):
                journal['inflight']='ai-reconnect';atomic_json(path,journal)
                app_remote.action(self.payload('app',operation,'check',include_files=False),'ai-reconnect',RESOURCES)
            result=app_remote.action(self.payload('app',operation,'check'),'verify-ai',RESOURCES)
            remote.action(payload,'commit',RESOURCES)
            journal.update(status='complete',result=result);journal.pop('inflight',None);atomic_json(path,journal)
            self.vault.data['ai_installed']=True
            if hasattr(self.vault,'save'):self.vault.save()
            self.log(tr('AI: установка и проверка ответа из App завершены успешно.'))
            return {'status':'complete','result':result}
        except Exception as error:
            # Do not expose arbitrary SSH/package output; Remote already sanitizes it.
            if mutating and journal.get('status')=='running':
                journal.update(status='failed',error=str(error))
                try:atomic_json(path,journal)
                except OSError:self.log(tr('AI: не удалось записать отдельный журнал; журнал основного стека сохранён.'))
            self.log(tr('ПРЕДУПРЕЖДЕНИЕ AI: ')+str(error))
            self.log(tr('Основной стек остаётся успешно развёрнутым. AI можно повторить отдельно с тем же релизом.'))
            return {'status':'warning','error':str(error)}
        finally:
            for r in (app_remote,db_remote,remote):
                if r:
                    try:r.cleanup()
                    except Exception:self.log(tr('AI: временный каталог не удалось очистить; основной стек не затронут.'))
                    try:r.close()
                    except Exception:pass
