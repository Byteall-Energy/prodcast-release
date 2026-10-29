"""Verified per-role deployment receipts and non-destructive reconciliation."""
import copy
import hashlib
import json
import time
import uuid
from .config import atomic_json, core_roles, topology_hash
from .release import sha


def fingerprint_details(engine,role,configuration_only=False):
    release=engine.release
    payload=engine.payload(role,'fingerprint','check',include_files=not configuration_only)
    files={} if configuration_only else {key:release.assets.get(path.name,{}).get('sha256') or sha(path) for key,path in release.for_role(role).items() if not key.startswith('offline-linux-')}
    if role.startswith('worker'):
        service=payload.get('worker_service')
        if service and service.get('mode')=='preserve':service=None
        settings=dict(database=engine.c['hosts']['db']['address'],app_url=engine.c['public_url'],
                      policy=payload['worker_execution_policy'],service=service)
        from .worker_queues import execution_queues, QUEUES
        queues=execution_queues(engine.c['hosts'][role])
        # Keep legacy all-queue receipts stable.
        if queues != list(QUEUES):settings['execution_queues']=queues
    elif role=='app':
        settings=dict(site={k:v for k,v in engine.c.items() if k!='hosts'},
                      hosts={r:h['address'] for r,h in engine.c['hosts'].items()},
                      directory=payload['directory'],activation=payload['activation'],app_tls=payload['app_tls'],
                      images={k:v for k,v in getattr(release,'images',{}).items() if k!='prodcast-ai'})
    else:settings=dict(hosts={r:h['address'] for r,h in engine.c['hosts'].items()},
                       install_ai=engine.c.get('install_ai',True),
                       database_images=getattr(release,'offline_spec',{}).get('database_images',{}))
    if not role.startswith('worker') and not configuration_only:
        settings['linux_packages']={key:release.assets[name]['sha256'] for key,name in getattr(release,'offline_spec',{}).get('linux_packages',{}).items()}
    value=dict(files=files,settings=settings,secrets=payload['secrets'],tls=payload['tls'])
    # Stored only inside the encrypted vault, never expose hashes of credentials.
    def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    content_settings={k:v for k,v in settings.items() if k in ('images','database_images','linux_packages')}
    config_settings={k:v for k,v in settings.items() if k not in content_settings}
    result=dict(signature=digest(value),content_signature=digest(dict(files=files,settings=content_settings)),
                configuration_signature=digest(dict(settings=config_settings,secrets=payload['secrets'],tls=payload['tls'])),files=files)
    if role.startswith('worker'):result['worker_execution_policy']=payload.get('worker_execution_policy')
    if role=='app':result['external_activation']=payload.get('external_activation',False)
    return result


def fingerprint(engine,role):return fingerprint_details(engine,role)['signature']


def bind_component(payload,record):
    payload.update({k:record[k] for k in ('version','manifest')})
    for key in ('worker_execution_policy','external_activation'):
        if key in record:payload[key]=record[key]
    if 'worker_execution_policy' in record:payload['installed_policy_known']=True
    return payload


def recorded_components(engine,additional_roles=()):
    """Validate sealed identity/roster once; component versions may differ."""
    baseline=engine.vault.data.get('deployment_components')
    if not baseline:return {}
    roles=set(core_roles(engine.c));records=baseline.get('roles',{})
    retired=set(engine.vault.data.get('retired_workers',{}))|set(additional_roles)
    if (baseline.get('schema')!=1 or baseline.get('installation_id')!=engine.vault.data.get('installation_id')
            or baseline.get('topology')!=topology_hash(engine.c) or roles-set(records) or (set(records)-roles)-retired):
        raise ValueError('Component deployment records do not match this installation; restore the matching profile and vault')
    if any(not records[r].get('version') or not records[r].get('manifest') for r in roles):
        raise ValueError('Component deployment records are incomplete; verify the existing installation')
    return {r:copy.deepcopy(records[r]) for r in core_roles(engine.c)}


def verify_component(role,status,expected,operation):
    if (not status.get('managed') or not status.get('version') or not status.get('manifest')
            or status.get('operation') not in ('',None,operation) or status.get('maintenance') not in ('',None,operation)):
        raise ValueError(role+': complete the pending operation or verify installed ownership first')
    if expected and any(status.get(k)!=expected.get(k) for k in ('version','manifest')):
        raise ValueError(role+': installed release differs from its component deployment record; finish or reconcile that component first')
    return {k:status[k] for k in ('version','manifest')}


def refresh_membership_receipts(engine,new_roles=()):
    """Rebase authorized membership settings without blessing different packages."""
    baseline=engine.vault.data.get('deployment_components')
    if not baseline:return
    records=baseline['roles']
    for role in core_roles(engine.c):
        if role in new_roles:
            records[role]=dict(fingerprint_details(engine,role),version=engine.release.version,manifest=engine.release.digest)
        elif not role.startswith('worker') and records.get(role,{}).get('content_signature'):
            # Content identity stays sealed to the original deployed package.
            records[role]['configuration_signature']=fingerprint_details(engine,role,configuration_only=True)['configuration_signature']
    baseline['roles']={r:records[r] for r in core_roles(engine.c)}
    engine.vault.save()


def enrich_matching_receipts(engine):
    """Upgrade legacy hashes only when this package/settings reproduce the seal."""
    for role,record in engine.vault.data.get('deployment_components',{}).get('roles',{}).items():
        if role not in core_roles(engine.c) or record.get('content_signature'):continue
        details=fingerprint_details(engine,role)
        if record.get('signature')==details['signature']:record.update(details)


def receipt(engine,roles=None,previous=None):
    records=dict(previous or {})
    for role in core_roles(engine.c) if roles is None else roles:
        records[role]=dict(fingerprint_details(engine,role),version=engine.release.version,manifest=engine.release.digest)
    return dict(schema=1,installation_id=engine.vault.data.get('installation_id'),
                topology=topology_hash(engine.c),roles={r:records[r] for r in core_roles(engine.c)})


def save_receipt(engine,roles=None,previous=None):
    if not hasattr(engine.vault,'save') or not getattr(engine.release,'assets',None):return
    engine.vault.data['deployment_components']=receipt(engine,roles,previous)
    engine.vault.save()


def changed_roles(engine,baseline):
    # Legacy profiles can update immediately using the traditional full scope.
    # Never invent unchanged-component receipts from the new target package.
    if not baseline:return core_roles(engine.c)
    if baseline.get('schema')!=1 or baseline.get('installation_id')!=engine.vault.data.get('installation_id') or baseline.get('topology')!=topology_hash(engine.c):
        raise ValueError('Component baseline belongs to another profile or is unsupported; restore the original profile and vault.')
    expected=set(core_roles(engine.c));recorded=set(baseline.get('roles',{}))
    retired=set(engine.vault.data.get('retired_workers',{}))
    if expected-recorded or (recorded-expected)-retired:
        raise ValueError('Component baseline does not match the current Worker roster; restore the matching profile or verify the existing installation.')
    def differs(role):
        old=baseline['roles'][role];new=fingerprint_details(engine,role)
        if old.get('content_signature') and old.get('configuration_signature'):
            return any(old[k]!=new[k] for k in ('content_signature','configuration_signature'))
        return old.get('signature')!=new['signature']
    return tuple(r for r in core_roles(engine.c) if differs(r))


class ExistingInstallation:
    def __init__(self,engine):self.e=engine;self.remotes={}

    def connect(self,role):
        from .engine import RESOURCES
        e=self.e;r=e.factory(role,e.c['hosts'][role],e.vault.data.get('ssh',{}).get(role,{}),e.log)
        self.remotes[role]=r;r.diagnostics=e.diagnostics;info=r.probe()
        if getattr(e.release,'offline',False):e.release.select_platform(role,info['os'])
        r.prepare_stage()
        for name in (('worker.ps1',) if role.startswith('worker') else ('linux.py',)):
            r.put_bytes(name,(RESOURCES/name).read_bytes())
        return r

    def close(self):
        for r in self.remotes.values():
            try:r.cleanup()
            except Exception:self.e.log('Manager staging cleanup needs attention; keep the operation log.')
            finally:
                try:r.close()
                except Exception:self.e.log('Could not close a staging connection; keep the operation log.')

    def action(self,role,operation,action,expected):
        from .engine import RESOURCES
        p=self.e.payload(role,operation,'healthcheck',include_files=False)
        bind_component(p,expected)
        p['installed_runtime']=getattr(self,'check_installed_runtime',False) or (expected['version'],expected['manifest'])!=(getattr(self.e.release,'version',None),getattr(self.e.release,'digest',None))
        if role=='app' and p['installed_runtime'] and 'external_activation' not in expected:
            p['external_activation']=bool(self.e.vault.data.get('activation'))
        return self.remotes[role].action(p,action,RESOURCES)

    def run(self):
        e=self.e;path=e.dir/'journal.json'
        if e.vault.data.get('topology')!=topology_hash(e.c):raise ValueError('Use the original installation profile and vault.')
        original=json.loads(path.read_text('utf-8')) if path.exists() else {}
        baseline_records=recorded_components(e)
        completed=original.get('status')=='complete' and bool(baseline_records)
        self.check_installed_runtime=completed
        if not original.get('operation') or not completed and (not e.release or original.get('manifest')!=e.release.digest):
            raise ValueError('Select the exact release recorded by the interrupted/current core operation, not a newer release.')
        if original.get('status') not in ('failed','running','complete','stopped'):
            raise ValueError('No recoverable core installation journal was found.')
        operation=original['operation'];baseline=e.vault.data.get('deployment_components',{})
        target_roles=set(original.get('changed_roles',[])) if original.get('selective') and not completed else set()
        sealed=e.vault.data.get('component_update_targets',{}).get(operation,{})
        if baseline and not completed:
            differing=set(changed_roles(e,baseline))
            if any(sealed.get(r)!=fingerprint(e,r) if r in target_roles else r in differing for r in core_roles(e.c)):
                raise ValueError('Saved settings/package differ from the verified installation. Restore its settings for healthcheck, or update the changed components.')
        expected={r:baseline.get('roles',{}).get(r,dict(version=getattr(e.release,'version',None),manifest=getattr(e.release,'digest',None))) for r in core_roles(e.c)}
        for role in target_roles:
            if role not in expected:raise ValueError('Component recovery roster mismatch')
            expected[role]=dict(version=e.release.version,manifest=e.release.digest)
        results={}
        try:
            # Finish every read-only check before any reconciliation write.
            for role in core_roles(e.c):
                e.log(role+': existing installation healthcheck (no restart/reinstall)')
                self.connect(role)
                status=self.action(role,operation,'preflight',expected[role])
                if not status.get('managed') or status.get('maintenance') or status.get('operation') not in ('',None,operation):
                    raise ValueError(role+': ownership or maintenance conflict; healthcheck cannot finalize this installation.')
                if completed:verify_component(role,status,expected[role],operation)
                results[role]=self.action(role,operation,'healthcheck',expected[role])
                if results[role].get('healthy') is not True:raise ValueError(role+': healthcheck did not confirm readiness')
            # Agents repeat readiness under their own lock and write only metadata.
            history=e.dir/'history';history.mkdir(exist_ok=True)
            archive=history/('core-before-healthcheck-'+operation+'.json')
            if not archive.exists():atomic_json(archive,original)
            for role in core_roles(e.c):self.action(role,operation,'reconcile',expected[role])
            original.update(status='complete',phase='complete',reconciled_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),healthcheck=results)
            original.pop('inflight',None);atomic_json(path,original)
            # Preserve older per-role versions for components intentionally skipped.
            prior={r:dict(expected[r]) if completed else dict(expected[r],**fingerprint_details(e,r)) for r in core_roles(e.c)}
            e.vault.data['deployment_components']=dict(schema=1,installation_id=e.vault.data.get('installation_id'),topology=topology_hash(e.c),roles=prior)
            e.vault.save()
            return dict(healthy=True,finalized=True,services_restarted=False,roles=results)
        finally:self.close()


def update_actions(changed):
    """Only selected service groups are stopped/configured/reinstalled."""
    changed=tuple(changed);workers=tuple(r for r in changed if r.startswith('worker'))
    steps=[(r,'claim') for r in changed]
    if 'app' in changed:steps += [('app','pause'),('app','drain'),('app','stop')]
    elif workers:steps += [('app','drain-workers')]
    steps += [(r,'stop') for r in workers]
    # App schema migrations still need a DB backup; this never restarts or
    # reconfigures the unchanged DB service group.
    steps += [(r,'backup') for r in ('db','app') if r in changed or r=='db' and 'app' in changed]
    steps += [(r,'bootstrap') for r in ('db','app') if r in changed]
    if 'db' in changed:steps += [('db','install'),('db','grants')]
    if 'app' in changed:steps += [('app','configure'),('app','migrate'),('app','start')]
    steps += [(r,'install') for r in workers]
    if workers and 'app' not in changed:steps += [('app','resume-workers')]
    steps += [(r,'verify') for r in workers]
    if changed:steps += [('app','verify')]
    if 'app' in changed:steps += [('app','beat')]
    steps += [(r,'commit') for r in changed]
    return steps


def selective_update(e):
    from .engine import RESOURCES
    baseline=e.vault.data.get('deployment_components',{})
    if not baseline:raise ValueError('Component update receipt is missing. Restore the original vault to resume this scoped operation; a new legacy update does not require healthcheck.')
    changed=changed_roles(e,baseline);path=e.dir/'journal.json'
    if 'app' in changed and e.vault.data.get('directory') is not None and not e.release.doc.get('management',{}).get('directory_configuration'):
        raise ValueError('LDAP configuration requires a release with the App directory configuration contract (v0.5.0 or later).')
    prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
    pending=prior.get('status') in ('running','failed') and prior.get('selective')
    # phase is persisted as applying, with inflight, before the first action.
    # A failed read-only preflight may be safely replanned with corrected
    # settings; never loosen settings/scope checks after an action was attempted.
    preflight_retry=bool(pending and prior.get('phase')=='preflight'
                         and not prior.get('steps') and not prior.get('inflight'))
    if prior.get('status') in ('running','failed') and not pending:
        raise ValueError('Resume or stop the pending core operation before starting a different component update.')
    if pending and (prior.get('manifest')!=e.release.digest or
                    prior.get('changed_roles')!=list(changed) and not preflight_retry):
        raise ValueError('Resume this component update with its original package, settings and scope.')
    operation=prior['operation'] if pending else uuid.uuid4().hex
    targets={r:fingerprint(e,r) for r in core_roles(e.c)}
    saved=e.vault.data.setdefault('component_update_targets',{})
    settings_changed=bool(pending and saved.get(operation)!=targets)
    if settings_changed and not preflight_retry:raise ValueError('Component update settings changed; restore the original settings to resume.')
    actions=update_actions(changed);owner=ExistingInstallation(e)
    journal=dict(prior) if pending else dict(operation=operation,mode='update',manifest=e.release.digest,
        version=e.release.version,status='running',phase='preflight',selective=True,changed_roles=list(changed),steps=[],
        install_ai=e.c.get('install_ai',True),ai_address=e.c['hosts']['ai']['address'])
    if not pending:
        saved[operation]=targets;e.vault.save()
        history=e.dir/'history';history.mkdir(exist_ok=True)
        if prior.get('operation'):atomic_json(history/('before-component-update-'+operation+'.json'),prior)
        atomic_json(path,journal)
    elif preflight_retry and (settings_changed or prior.get('changed_roles')!=list(changed)):
        history=e.dir/'history';history.mkdir(exist_ok=True)
        atomic_json(history/('before-preflight-retry-'+operation+'-'+uuid.uuid4().hex+'.json'),prior)
        saved[operation]=targets;e.vault.save()
        journal.update(changed_roles=list(changed),install_ai=e.c.get('install_ai',True),
                       ai_address=e.c['hosts']['ai']['address'],status='running')
        atomic_json(path,journal)
        e.log('Preflight-only retry: accepted corrected settings; repeating all checks before applying update steps.')
    e.log('Update scope: '+(', '.join(changed) or 'no core component changes'))
    e.log('Unchanged service groups are not stopped, reinstalled, reconfigured or committed.')
    statuses={};consumers_drained=False;previous_queues={};installed_workers=set()
    try:
        for role in core_roles(e.c):
            owner.connect(role)
            p=e.payload(role,operation,'update' if role in changed else 'check')
            if role not in changed:p.update(version=baseline['roles'][role]['version'],manifest=baseline['roles'][role]['manifest'])
            status=owner.remotes[role].action(p,'preflight',RESOURCES);statuses[role]=status
            if not status.get('managed') or status.get('maintenance') or status.get('operation') not in ('',None,operation):
                raise ValueError(role+': ownership conflict; component update has not been authorized')
            expected=baseline['roles'][role]
            permitted={(expected['version'],expected['manifest'])}
            if pending and role in changed:permitted.add((e.release.version,e.release.digest))
            if (status.get('version'),status.get('manifest')) not in permitted:
                raise ValueError(role+': installed receipt changed; run the existing installation healthcheck')
            if role in changed and status.get('version') not in e.release.allowed_from+[e.release.version]:
                raise ValueError(role+': unsupported upgrade source')
            if role in changed:
                for name in (('worker-service-runner.py',) if role.startswith('worker') else ('app.env.in','worker-grants.sql')):
                    owner.remotes[role].put_bytes(name,(RESOURCES/name).read_bytes())
                for asset in e.release.for_role(role).values():owner.remotes[role].put(asset)
        # Resolve service-account readiness before draining/stopping any service.
        for role in changed:
            if role.startswith('worker'):
                owner.remotes[role].action(e.payload(role,operation,'update'),'service-preflight',RESOURCES)
        for role,action in actions:
            p=e.payload(role,operation,'update' if role in changed else 'component-check')
            if role not in changed:p.update(version=baseline['roles'][role]['version'],manifest=baseline['roles'][role]['manifest'])
            p['changed_workers']=[r for r in changed if r.startswith('worker')]
            journal.update(status='running',phase='applying',inflight={'role':role,'action':action});atomic_json(path,journal)
            e.log(role+': '+action)
            result=owner.remotes[role].action(p,action,RESOURCES)
            if action=='drain-workers':
                consumers_drained=True;previous_queues=result.get('previous_queues',{})
            if action=='install' and role.startswith('worker'):installed_workers.add(role)
            elif action=='resume-workers':consumers_drained=False
            journal['steps'].append(dict(role=role,action=action,result=result));journal.pop('inflight',None);atomic_json(path,journal)
        save_receipt(e,changed,baseline['roles'])
        journal.update(status='complete',phase='complete');atomic_json(path,journal)
        ai=e._optional_ai('update',operation,allow_skip=True)
        return dict(status='complete',changed_roles=list(changed),unchanged_roles=[r for r in core_roles(e.c) if r not in changed],ai=ai)
    except Exception:
        if journal.get('status')!='complete':
            journal['status']='failed';atomic_json(path,journal)
        if consumers_drained and 'app' not in changed and 'app' in owner.remotes:
            try:
                p=e.payload('app',operation,'component-check');p.update(version=baseline['roles']['app']['version'],manifest=baseline['roles']['app']['manifest'])
                p['changed_workers']=[r for r in changed if r.startswith('worker')]
                if previous_queues:
                    from .worker_queues import execution_queues
                    p['resume_queues']={
                        'prodcast-worker-'+f'{int(r[6:]):02}'+'@':
                        execution_queues(e.c['hosts'][r]) if r in installed_workers else previous_queues['prodcast-worker-'+f'{int(r[6:]):02}'+'@']
                        for r in p['changed_workers']}
                owner.remotes['app'].action(p,'resume-workers',RESOURCES)
            except Exception:e.log('Selected Worker consumers could not be restored; keep the journal and resume the same component update. App services were not restarted.')
        raise
    finally:owner.close()


def repair_components(e):
    """Repair installed runtimes without stamping every role with one ZIP version."""
    from .engine import RESOURCES
    records=recorded_components(e);path=e.dir/'journal.json'
    prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
    pending=prior.get('status') in ('running','failed')
    if pending and not prior.get('component_repair'):
        raise ValueError('Resume or stop the interrupted core operation before repairing installed components')
    versions={r:{k:v[k] for k in ('version','manifest')} for r,v in records.items()}
    preflight_retry=pending and prior.get('phase')=='preflight' and not prior.get('steps') and not prior.get('inflight')
    if pending and (prior.get('component_versions')!=versions or prior.get('manifest')!=e.release.digest and not preflight_retry):
        raise ValueError('Resume component repair with its original package and installed component records')
    operation=prior['operation'] if pending else uuid.uuid4().hex
    owner=ExistingInstallation(e);owner.check_installed_runtime=True
    journal=dict(prior) if pending else dict(operation=operation,mode='repair',manifest=e.release.digest,
        component_repair=True,component_versions=versions,status='running',phase='preflight',steps=[])
    if preflight_retry and prior.get('manifest')!=e.release.digest:
        atomic_json(e.dir/'history'/('repair-before-preflight-retry-'+uuid.uuid4().hex+'.json'),prior)
        journal['manifest']=e.release.digest
    repair_workers=[];preserved=[]
    def payload(role):
        p=e.payload(role,operation,'repair',include_files=role in repair_workers)
        bind_component(p,records[role]);p['installed_runtime']=True
        return p
    try:
        # Validate every owner and any required Worker package before claiming a VM.
        for role in core_roles(e.c):
            owner.connect(role)
            verify_component(role,owner.action(role,operation,'preflight',records[role]),versions[role],operation)
            if role.startswith('worker'):
                try:
                    if owner.action(role,operation,'healthcheck',records[role]).get('healthy') is not True:
                        raise RuntimeError('Worker is not healthy')
                    preserved.append(role);continue
                except RuntimeError:
                    exact=(e.release.version,e.release.digest)==(versions[role]['version'],versions[role]['manifest'])
                    files=fingerprint_details(e,role)['files']
                    equivalent=(bool(records[role].get('files')) and files==records[role]['files']
                                and 'worker_execution_policy' in records[role]
                                and records[role]['worker_execution_policy']==e.payload(role,operation,'check',include_files=False)['worker_execution_policy'])
                    if not (exact or equivalent):
                        raise ValueError(role+': repair requires its installed Worker payload; select that release or use the individual Worker repair action')
                repair_workers.append(role)
                owner.remotes[role].put_bytes('worker-service-runner.py',(RESOURCES/'worker-service-runner.py').read_bytes())
                for asset in e.release.for_role(role).values():owner.remotes[role].put(asset)
                owner.remotes[role].action(payload(role),'service-preflight',RESOURCES)
        if not pending:
            atomic_json(e.dir/'history'/('before-component-repair-'+operation+'.json'),prior)
        journal.update(status='running',phase='applying');atomic_json(path,journal)
        roles=('db','app',*repair_workers)
        for role,action in ([(r,'claim') for r in roles]+[(r,'repair') for r in roles]
                            +[(r,'verify') for r in repair_workers]+[('app','verify'),('app','beat')]
                            +[(r,'commit') for r in roles]):
            journal['inflight']={'role':role,'action':action};atomic_json(path,journal)
            e.log(role+': '+action+' (installed '+versions[role]['version']+')')
            result=owner.remotes[role].action(payload(role),action,RESOURCES)
            journal['steps'].append(dict(role=role,action=action,result=result));journal.pop('inflight',None);atomic_json(path,journal)
        journal.update(status='complete',phase='complete');atomic_json(path,journal)
        return dict(status='complete',repaired_roles=list(roles),healthy_workers_preserved=preserved,component_versions=versions)
    except Exception:
        journal['status']='failed';atomic_json(path,journal);raise
    finally:owner.close()
