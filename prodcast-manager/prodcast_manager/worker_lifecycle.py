"""Resumable repair/removal of one member of an installed Worker roster."""
import copy
import uuid
from pathlib import Path
from .config import atomic_json, file_lock, topology_hash, validate_membership, worker_addresses, worker_roles
from .portable import runtime_config, save_config
from .workers import binding, read_json
from .operation_history import journal_blocks_changes


def prune_removed_receipt(vault,role):
    baseline=vault.data.get('deployment_components',{})
    if baseline.get('installation_id')==vault.data.get('installation_id'):
        baseline.get('roles',{}).pop(role,None)

JOURNAL = 'worker-action-journal.json'


def removal_target(config, role):
    if role not in worker_roles(config):
        raise ValueError('Select an installed Worker')
    if len(worker_roles(config)) == 1:
        raise ValueError('Keep at least one Worker installed')
    target = copy.deepcopy(config)
    target.setdefault('_worker_topology_hosts', worker_addresses(config))
    del target['hosts'][role]
    if topology_hash(target) != topology_hash(config):
        raise ValueError('Worker removal would change installation identity')
    return target


def run_worker_action(directory, role, mode, vault, release=None, log=print,
                      remote_factory=None, diagnostics=None):
    from .engine import Engine, RESOURCES
    if mode not in ('worker-repair', 'worker-remove'):
        raise ValueError('Unknown Worker action')
    directory = Path(directory)
    path = directory / JOURNAL
    remotes = {}
    with file_lock(directory / 'operation.lock'):
        for name in ('journal.json', 'ai-journal.json', 'maintenance-journal.json',
                     'app-tls-journal.json', 'workers-journal.json'):
            if journal_blocks_changes(name,read_json(directory / name)):
                raise ValueError('Complete or stop the pending operation first: ' + name)
        current = runtime_config(directory / 'site.json')
        journal = read_json(path)
        pending = journal.get('status') in ('running', 'failed')
        if pending:
            signed=vault.data.get('worker_action',{})
            if (signed.get('operation')!=journal.get('operation') or signed.get('original')!=binding(journal['original'])
                    or signed.get('target')!=binding(journal['target']) or signed.get('mode')!=mode or signed.get('role')!=role):
                raise ValueError('Use the original vault and Worker action journal')
            if (journal['role'], journal['mode']) != (role, mode):
                raise ValueError('Resume the previous selected Worker action')
            if journal['installation_id'] != vault.data.get('installation_id'):
                raise ValueError('Use the original installation vault')
            if binding(current) not in (binding(journal['original']), binding(journal['target'])):
                raise ValueError('Worker action profile changed')
            if journal.get('phase')=='committing':
                vault.data.update(worker_topology_hosts=journal['target']['_worker_topology_hosts'],
                                  worker_hosts=worker_addresses(journal['target']))
                vault.data.setdefault('retired_workers',{})[role]={'operation':journal['operation'],'address':journal['original']['hosts'][role]['address']}
                prune_removed_receipt(vault,role)
                vault.save();save_config(directory,journal['target'])
                current=runtime_config(directory/'site.json')
        else:
            validate_membership(current,vault.data)
            if role not in worker_roles(current) or not vault.data.get('installation_id'):
                raise ValueError('Open the installed site and select its Worker')
            target = removal_target(current, role) if mode == 'worker-remove' else copy.deepcopy(current)
            journal = dict(operation=uuid.uuid4().hex, role=role, mode=mode,
                           installation_id=vault.data['installation_id'], original=current,
                           target=target, steps=[], status='running')
        if mode == 'worker-repair' and release is None:
            raise ValueError('Select the exact installed release for Worker repair')
        if pending and mode == 'worker-repair' and journal.get('manifest') != release.digest:
            raise ValueError('Resume Worker repair with the original release')
        if release:
            journal['manifest'] = release.digest
        # Back up encrypted local metadata before the first remote mutation.
        if not pending:
            import shutil
            history = directory / 'history' / ('worker-action-' + journal['operation'])
            history.mkdir(parents=True)
            for item in (directory / 'site.json', vault.path):
                if item.exists():
                    shutil.copyfile(item, history / item.name)
            vault.data['worker_action']={'operation':journal['operation'],'role':role,'mode':mode,
                                         'original':binding(journal['original']),'target':binding(journal['target'])}
            vault.save()
        journal['status'] = 'running'
        atomic_json(path, journal)
        options = {'remote_factory': remote_factory} if remote_factory else {}
        engine = Engine(current, vault, release, directory, log, diagnostics=diagnostics, **options)
        engine.c = journal['original']
        operation = journal['operation']
        selected = ('app', role) if mode == 'worker-repair' else ('app', 'db', role)

        def action(host, name, **extra):
            payload = engine.payload(host, operation, mode)
            payload['worker_action'] = {'role': role, 'target': journal['target'],
                                        'previous_workers': worker_addresses(journal['original'])}
            payload.update(extra)
            log(host + ': ' + name)
            return remotes[host].action(payload, name, RESOURCES)

        def step(host, name, **extra):
            key = host + ':' + name
            if key in journal['steps']:
                return
            result = action(host, name, **extra)
            journal['steps'].append(key)
            atomic_json(path, journal)
            return result

        try:
            for host in selected:
                remote = engine.factory(host, engine.c['hosts'][host], vault.data.get('ssh', {}).get(host, {}), log)
                remotes[host] = remote
                remote.diagnostics = diagnostics
                remote.probe()
                remote.prepare_stage()
                names = ('worker.ps1', 'worker-service-runner.py') if host == role else ('linux.py', 'maintenance_linux.py', 'worker_lifecycle_linux.py', 'workers_linux.py')
                for name in names:
                    remote.put_bytes(name, (RESOURCES / name).read_bytes())
                status = action(host, 'preflight')
                if (not status.get('managed') or status.get('operation') not in ('', None, operation)
                        or status.get('maintenance') not in ('', None, operation)):
                    raise ValueError(host + ': complete the previous operation before this Worker action')
                if mode == 'worker-repair' and host == role and (status.get('version') != release.version or status.get('manifest') != release.digest):
                    raise ValueError('Select the exact release installed on ' + role)
            for host in selected:
                step(host, 'maintenance-claim')
            if 'app_runtime' not in journal:
                journal['app_runtime'] = action('app', 'maintenance-snapshot')
                atomic_json(path, journal)
            step('app', 'pause')
            # Strict drain fails closed if consumers or queued tasks cannot be accounted for.
            step('app', 'drain')
            step(role, 'maintenance-stop')
            if mode == 'worker-repair':
                for asset in release.for_role(role).values():
                    remotes[role].put(asset)
                step(role, 'reset')  # Removes only the service; runtime/logs are retained.
                step(role, 'claim')
                step(role, 'repair')
                # Readiness must be rechecked even when a previous verify succeeded.
                if not action(role, 'verify').get('healthy'):
                    raise RuntimeError(role + ': Worker readiness failed')
                step(role, 'commit')
            else:
                step('db', 'worker-retire-access')
                step(role, 'worker-retire')
                # DB receipt accepts both rosters while the local commit is retried.
                step('db', 'worker-retire-membership')
                journal['phase']='committing';atomic_json(path,journal)
                vault.data.update(worker_topology_hosts=journal['target']['_worker_topology_hosts'],
                                  worker_hosts=worker_addresses(journal['target']))
                vault.data.setdefault('retired_workers',{})[role]={'operation':operation,'address':journal['original']['hosts'][role]['address']}
                prune_removed_receipt(vault,role)
                vault.save()
                save_config(directory, journal['target'])
            step('app', 'maintenance-resume', previous_runtime=journal['app_runtime'])
            action('app', 'maintenance-health', previous_runtime=journal['app_runtime'])
            for host in selected:
                step(host, 'maintenance-release')
            journal['status'] = 'complete'
            atomic_json(path, journal)
            log(role + ': ' + ('repaired' if mode == 'worker-repair' else 'removed; runtime and logs retained'))
            return runtime_config(directory / 'site.json')
        except Exception:
            journal['status'] = 'failed'
            atomic_json(path, journal)
            log('Worker action stopped. Keep the vault and journal and resume the same Worker action. App scheduling may remain paused.')
            raise
        finally:
            for remote in remotes.values():
                try:
                    remote.cleanup()
                except Exception as error:
                    if hasattr(remote,'cleanup_warning'):remote.cleanup_warning(error)
                    else:log(remote.role+': Could not clean a temporary staging directory')
                try:
                    remote.close()
                except Exception:
                    pass
