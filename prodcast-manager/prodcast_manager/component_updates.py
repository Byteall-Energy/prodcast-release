"""Verified per-role deployment receipts and non-destructive reconciliation."""
import hashlib
import json
import time
import uuid
from .config import atomic_json, core_roles, topology_hash
from .release import sha


def fingerprint(engine,role):
    release=engine.release
    payload=engine.payload(role,'fingerprint','check')
    files={key:release.assets.get(path.name,{}).get('sha256') or sha(path) for key,path in release.for_role(role).items() if not key.startswith('offline-linux-')}
    if role.startswith('worker'):
        service=payload.get('worker_service')
        if service and service.get('mode')=='preserve':service=None
        settings=dict(database=engine.c['hosts']['db']['address'],app_url=engine.c['public_url'],
                      policy=payload['worker_execution_policy'],service=service)
    elif role=='app':
        settings=dict(site={k:v for k,v in engine.c.items() if k!='hosts'},
                      hosts={r:h['address'] for r,h in engine.c['hosts'].items()},
                      directory=payload['directory'],activation=payload['activation'],app_tls=payload['app_tls'],
                      images={k:v for k,v in release.images.items() if k!='prodcast-ai'})
    else:settings=dict(hosts={r:h['address'] for r,h in engine.c['hosts'].items()},
                       install_ai=engine.c.get('install_ai',True),
                       database_images=getattr(release,'offline_spec',{}).get('database_images',{}))
    if not role.startswith('worker'):
        settings['linux_packages']={key:release.assets[name]['sha256'] for key,name in getattr(release,'offline_spec',{}).get('linux_packages',{}).items()}
    value=dict(files=files,settings=settings,secrets=payload['secrets'],tls=payload['tls'])
    # Stored only inside the encrypted vault, never expose hashes of credentials.
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def receipt(engine,roles=None,previous=None):
    records=dict(previous or {})
    for role in core_roles(engine.c) if roles is None else roles:
        records[role]=dict(signature=fingerprint(engine,role),version=engine.release.version,manifest=engine.release.digest)
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
    return tuple(r for r in core_roles(engine.c) if baseline['roles'][r]['signature']!=fingerprint(engine,r))


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
            except Exception:self.e.log('Healthcheck staging cleanup needs attention; installed services were not modified.')
            finally:
                try:r.close()
                except Exception:self.e.log('Could not close a staging connection; installed services were not modified.')

    def action(self,role,operation,action,expected):
        from .engine import RESOURCES
        p=self.e.payload(role,operation,'healthcheck')
        p.update(version=expected['version'],manifest=expected['manifest'])
        return self.remotes[role].action(p,action,RESOURCES)

    def run(self):
        e=self.e;path=e.dir/'journal.json'
        if e.vault.data.get('topology')!=topology_hash(e.c):raise ValueError('Use the original installation profile and vault.')
        original=json.loads(path.read_text('utf-8')) if path.exists() else {}
        if original.get('manifest')!=e.release.digest or not original.get('operation'):
            raise ValueError('Select the exact release recorded by the interrupted/current core operation, not a newer release.')
        if original.get('status') not in ('failed','running','complete','stopped'):
            raise ValueError('No recoverable core installation journal was found.')
        operation=original['operation'];baseline=e.vault.data.get('deployment_components',{})
        target_roles=set(original.get('changed_roles',[])) if original.get('selective') else set()
        sealed=e.vault.data.get('component_update_targets',{}).get(operation,{})
        if baseline:
            if any((sealed.get(r) if r in target_roles else baseline.get('roles',{}).get(r,{}).get('signature'))!=fingerprint(e,r) for r in core_roles(e.c)):
                raise ValueError('Saved settings/package differ from the verified installation. Restore its settings for healthcheck, or update the changed components.')
        expected={r:baseline.get('roles',{}).get(r,dict(version=e.release.version,manifest=e.release.digest)) for r in core_roles(e.c)}
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
            prior={r:dict(expected[r],signature=fingerprint(e,r)) for r in core_roles(e.c)}
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
    steps += [(r,'backup') for r in ('db','app')+workers if r in changed or r=='db' and 'app' in changed]
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
    prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
    pending=prior.get('status') in ('running','failed') and prior.get('selective')
    if prior.get('status') in ('running','failed') and not pending:
        raise ValueError('Resume or stop the pending core operation before starting a different component update.')
    if pending and (prior.get('manifest')!=e.release.digest or prior.get('changed_roles')!=list(changed)):
        raise ValueError('Resume this component update with its original package, settings and scope.')
    operation=prior['operation'] if pending else uuid.uuid4().hex
    targets={r:fingerprint(e,r) for r in core_roles(e.c)}
    saved=e.vault.data.setdefault('component_update_targets',{})
    if pending and saved.get(operation)!=targets:raise ValueError('Component update settings changed; restore the original settings to resume.')
    actions=update_actions(changed);owner=ExistingInstallation(e)
    journal=dict(prior) if pending else dict(operation=operation,mode='update',manifest=e.release.digest,
        version=e.release.version,status='running',phase='preflight',selective=True,changed_roles=list(changed),steps=[],
        install_ai=e.c.get('install_ai',True),ai_address=e.c['hosts']['ai']['address'])
    if not pending:
        saved[operation]=targets;e.vault.save()
        history=e.dir/'history';history.mkdir(exist_ok=True)
        if prior.get('operation'):atomic_json(history/('before-component-update-'+operation+'.json'),prior)
        atomic_json(path,journal)
    e.log('Update scope: '+(', '.join(changed) or 'no core component changes'))
    e.log('Unchanged service groups are not stopped, reinstalled, reconfigured or committed.')
    statuses={};consumers_drained=False
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
            if action=='drain-workers':consumers_drained=True
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
                owner.remotes['app'].action(p,'resume-workers',RESOURCES)
            except Exception:e.log('Selected Worker consumers could not be restored; keep the journal and resume the same component update. App services were not restarted.')
        raise
    finally:owner.close()
