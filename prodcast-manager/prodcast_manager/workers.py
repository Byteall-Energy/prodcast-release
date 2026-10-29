"""Append-only, resumable expansion of an installed Worker roster."""
import copy
import hashlib
import json
import secrets
import uuid
from pathlib import Path

from .config import (atomic_json, file_lock, topology_hash, validate, validate_membership,
                     worker_addresses, worker_roles, core_roles)
from .portable import runtime_config, save_config
from .operation_history import journal_blocks_changes

JOURNAL='workers-journal.json'


def read_json(path):
    return json.loads(path.read_text('utf-8')) if path.exists() else {}


def require_no_expansion(directory):
    if read_json(Path(directory)/'worker-action-journal.json').get('status') in ('running','failed'):
        raise ValueError('Resume the selected Worker action before running another operation')
    if read_json(Path(directory)/JOURNAL).get('status') in ('running','failed'):
        raise ValueError('Resume Add Workers before running another operation')


def candidate(original, proposed):
    """Add unused worker IDs; never re-address an existing host."""
    validate(original,True);validate(proposed,True)
    old=worker_addresses(original);new=worker_addresses(proposed)
    if len(new)<=len(old) or any(new.get(r)!=a for r,a in old.items()):
        raise ValueError('Add Workers can only append new Workers; existing Workers must keep their addresses')
    for role,host in original['hosts'].items():
        if host['address']!=proposed['hosts'][role]['address']:
            raise ValueError('Existing server addresses must not change when adding Workers')
    from .worker_queues import execution_queues
    for role in old:
        if execution_queues(original['hosts'][role])!=execution_queues(proposed['hosts'][role]):
            raise ValueError('Update existing Worker queues separately before adding Workers')
    ignored={'hosts','_worker_topology_hosts'}
    if {k:v for k,v in original.items() if k not in ignored}!={k:v for k,v in proposed.items() if k not in ignored}:
        raise ValueError('Keep the original site settings when adding Workers')
    target=copy.deepcopy(proposed)
    baseline=original.get('_worker_topology_hosts',old)
    if proposed.get('_worker_topology_hosts',baseline)!=baseline:
        raise ValueError('The original Worker topology must not change')
    target['_worker_topology_hosts']=copy.deepcopy(baseline)
    if topology_hash(target)!=topology_hash(original):raise ValueError('Installation identity would change')
    return target,tuple(r for r in worker_roles(target) if r not in old)


def binding(config):
    # SSH credentials can be corrected on retry; the planned installation cannot.
    value={k:v for k,v in config.items() if k!='hosts'}
    value['hosts']={r:h['address'] for r,h in config['hosts'].items()}
    from .worker_queues import subscriptions, QUEUES
    selected={r:q for r,q in subscriptions(config).items() if q!=list(QUEUES)}
    if selected:value['worker_queues']=selected
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def add_workers(directory, proposed, vault, release, log=print, remote_factory=None, diagnostics=None):
    from .engine import Engine,RESOURCES
    directory=Path(directory);path=directory/JOURNAL;remotes={}
    with file_lock(directory/'operation.lock'):
        for name in ('journal.json','ai-journal.json','maintenance-journal.json','app-tls-journal.json','worker-action-journal.json'):
            if journal_blocks_changes(name,read_json(directory/name)):
                raise ValueError('Complete the pending operation first: '+name)
        journal=read_json(path);pending=journal.get('status') in ('running','failed')
        if journal.get('status')=='failed' and journal.get('phase')=='preflight':
            # No server changes were attempted. Correcting a mistyped new IP or
            # selecting the installed release must not strand the profile.
            atomic_json(directory/'history'/('workers-preflight-'+journal['operation']+'.json'),journal)
            pending=False
        original=runtime_config(directory/'site.json')
        if pending:
            signed=vault.data.get('worker_expansion',{})
            if (signed.get('operation')!=journal.get('operation') or signed.get('target')!=binding(journal['target'])
                    or signed.get('manifest')!=release.digest or journal.get('manifest')!=release.digest
                    or signed.get('original')!=binding(journal['original'])):
                raise ValueError('Resume Add Workers with the original vault, journal and release')
            proposed=copy.deepcopy(proposed)
            proposed['_worker_topology_hosts']=journal['target']['_worker_topology_hosts']
            if binding(proposed)!=signed['target']:
                raise ValueError('Resume Add Workers with the same new Worker addresses and site settings')
            if binding(original) not in (signed['original'],signed['target']):
                raise ValueError('The saved profile changed during Add Workers')
            # Complete a local two-file commit interrupted between site and vault.
            if journal.get('phase')=='committing':
                vault.data.update(worker_topology_hosts=journal['target']['_worker_topology_hosts'],
                                  worker_hosts=worker_addresses(journal['target']))
                vault.save();save_config(directory,proposed)
                original=runtime_config(directory/'site.json')
            else:
                validate_membership(original,vault.data)
            target=proposed;new=tuple(journal['new_workers']);operation=journal['operation']
        else:
            if not vault.data.get('installation_id') or vault.data.get('topology')!=topology_hash(original):
                raise ValueError('Open the original installed site and its vault')
            validate_membership(original,vault.data)
            target,new=candidate(original,proposed);operation=uuid.uuid4().hex
            release.validate_worker_count(len(worker_roles(target)))
            journal=dict(operation=operation,status='running',phase='preflight',manifest=release.digest,
                         version=release.version,original=original,target=target,new_workers=list(new),steps=[])
            # Keep encrypted credentials and original local metadata before expanding.
            history=directory/'history'/('workers-'+operation);history.mkdir(parents=True)
            import shutil
            for item in (directory/'site.json',vault.path):
                if item.exists():shutil.copyfile(item,history/item.name)
            vault.data['worker_expansion']={'operation':operation,'target':binding(target),
                                           'original':binding(original),'manifest':release.digest}
            for role in new:
                number=f'{int(role[6:]):02}'
                for prefix in ('PW_W','REDIS_W','SVC_W'):
                    vault.data['secrets'].setdefault(prefix+number,('Pc!9' if prefix=='SVC_W' else '')+secrets.token_hex(32))
            vault.save();atomic_json(path,journal)
        release.validate_worker_count(len(worker_roles(target)))
        if diagnostics:diagnostics.protect(vault.data)
        journal['status']='running';atomic_json(path,journal)
        options={'remote_factory':remote_factory} if remote_factory else {}
        engine=Engine(original,vault,release,directory,log,diagnostics=diagnostics,**options)
        engine.c=target  # Only this operation may use the not-yet-approved roster.
        retired=copy.deepcopy(vault.data.get('retired_workers',{}))
        # Upgrade profiles created by the first per-Worker removal preview.
        prior_removal=vault.data.get('worker_action',{})
        removed_role=prior_removal.get('role')
        if (prior_removal.get('mode')=='worker-remove' and removed_role not in worker_addresses(original)
                and prior_removal.get('target')==binding(original)):
            retired.setdefault(removed_role,{'operation':prior_removal['operation']})
        reactivated={r:retired[r] for r in new if r in retired}
        def payload(role):
            result=engine.payload(role,operation,'add-workers')
            result['worker_expansion']={'id':operation,'target':binding(target),
                                        'new_workers':list(new),'previous_workers':worker_addresses(journal['original']),
                                        'reactivated_workers':reactivated}
            return result
        def action(role,name):
            log(role+': '+name)
            result=remotes[role].action(payload(role),name,RESOURCES)
            journal['steps'].append({'role':role,'action':name})
            atomic_json(path,journal)
            return result
        try:
            statuses={}
            for role in core_roles(target):
                remote=engine.factory(role,target['hosts'][role],vault.data.get('ssh',{}).get(role,{}),log)
                remotes[role]=remote;remote.diagnostics=diagnostics
                info=remote.probe()
                if role in new and info['free']<8*1024**3:raise ValueError(role+': need at least 8 GiB free')
                remote.prepare_stage()
                names=('worker.ps1','worker-service-runner.py') if role.startswith('worker') else ('linux.py','workers_linux.py','worker-grants.sql')
                for name in names:remote.put_bytes(name,(RESOURCES/name).read_bytes())
                status=remote.action(payload(role),'preflight',RESOURCES);statuses[role]=status
                if status.get('maintenance') not in (None,'',operation):raise ValueError(role+': pending maintenance operation')
                if role not in new:
                    if (not status.get('managed') or status.get('version')!=release.version
                            or status.get('manifest')!=release.digest or status.get('operation') not in ('',None,operation)):
                        raise ValueError(role+': select the exact installed release and finish pending operations')
                elif status.get('managed'):
                    reclaim=role in reactivated and (status.get('retired') is True or status.get('worker_expansion')==operation)
                    if status.get('worker_expansion')!=operation and not reclaim:
                        raise ValueError(role+': the new VM already belongs to an installation')
                    if status.get('operation') not in ('',None,operation):raise ValueError(role+': another operation owns this VM')
                    if not reclaim and status.get('version') and (status['version']!=release.version or status.get('manifest')!=release.digest):
                        raise ValueError(role+': resumed Worker release does not match the expansion plan')
            # Transfer only the Worker package. App/DB/AI payloads are not deployed.
            for role in new:
                for asset in release.for_role(role).values():remotes[role].put(asset)
            journal['phase']='applying' if journal.get('phase')!='committing' else 'committing';atomic_json(path,journal)
            action('db','workers-prepare')
            for role in new:
                action(role,'claim')
                action(role,'install')
                result=action(role,'verify')
                if not result.get('healthy'):raise RuntimeError(role+': Worker readiness failed')
                action(role,'commit')
            # Persist all membership files before releasing the DB operation lock.
            journal['phase']='committing';journal['target']=target;atomic_json(path,journal)
            vault.data.update(worker_topology_hosts=target['_worker_topology_hosts'],worker_hosts=worker_addresses(target))
            vault.save();saved=save_config(directory,target)
            action('db','workers-commit')
            for role in new:retired.pop(role,None)
            vault.data['retired_workers']=retired;vault.save()
            journal['status']='complete';atomic_json(path,journal)
            # Keep the last transaction receipt in the vault for crash recovery.
            log('Workers added: '+', '.join(new))
            return saved
        except Exception:
            journal['status']='failed';atomic_json(path,journal)
            log('Add Workers stopped. Keep the profile and journal; retry with the same release and Worker addresses.')
            raise
        finally:
            for remote in remotes.values():
                try:remote.cleanup()
                except Exception as error:
                    if hasattr(remote,'cleanup_warning'):remote.cleanup_warning(error)
                    else:log(remote.role+': Could not clean a temporary staging directory')
                try:remote.close()
                except Exception:pass
