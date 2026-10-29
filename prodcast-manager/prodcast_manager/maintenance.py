"""Explicit, resumable backup/restore/reset operations for the selected site."""
import contextlib
import copy
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from . import backup_archive as archive
from .config import atomic_json,file_lock,core_roles,worker_roles,topology_hash
from .engine import RESOURCES
from .portable import open_credentials,save_config
from .operation_history import journal_blocks_changes


@contextlib.contextmanager
def private_workspace(parent):
    parent=Path(parent);parent.mkdir(parents=True,exist_ok=True)
    path=Path(tempfile.mkdtemp(prefix='backup-',dir=parent))
    try:
        os.chmod(path,0o700)
        if os.name=='nt':
            who=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True).stdout
            import csv
            sid=next(csv.reader([who.strip()]))[1]
            subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)(F)','*S-1-5-18:(OI)(CI)(F)'],capture_output=True,check=True)
        yield path
    finally:shutil.rmtree(path)


def read_backup(path,password,folder):
    plain=folder/'archive.zip';archive.decrypt(path,plain,password)
    return archive.unpack(plain,folder/'data')


def installed_releases(engine,statuses):
    """Accept completed selective deployments, but never infer an unverified mix."""
    roles=set(core_roles(engine.c))
    if set(statuses)!=roles or not all(s.get('managed') and s.get('version') and s.get('manifest') for s in statuses.values()):
        raise ValueError('Install App, DB and Workers before backup/restore')
    versions={r:{k:statuses[r][k] for k in ('version','manifest')} for r in roles}
    baseline=engine.vault.data.get('deployment_components')
    if baseline:
        if baseline.get('schema')!=1 or baseline.get('installation_id')!=engine.vault.data.get('installation_id') or baseline.get('topology')!=topology_hash(engine.c) or set(baseline.get('roles',{}))!=roles:
            raise ValueError('Component deployment records do not match this installation; restore the matching profile before backup/restore')
        for role in sorted(roles):
            if any(baseline['roles'][role].get(k)!=v for k,v in versions[role].items()):
                raise ValueError(role+': installed release differs from its component deployment record; finish or reconcile that component before backup/restore')
    elif len({(v['version'],v['manifest']) for v in versions.values()})!=1:
        raise ValueError('Different component releases need verified deployment records; restore the original Manager profile before backup/restore')
    core=engine.dir/'journal.json'
    if core.exists() and json.loads(core.read_text('utf-8')).get('status') in ('running','failed'):
        raise ValueError('Complete or stop the pending core operation before backup/restore')
    return versions


def import_backup_profile(path,password,parent,destination):
    """Recover identity and encryption keys; SSH credentials must be supplied again."""
    destination=Path(destination)
    if destination.exists():raise ValueError('Choose a new profile directory')
    with private_workspace(parent) as folder:
        data=read_backup(path,password,folder)
        site=copy.deepcopy(data['site'])
        for host in site['hosts'].values():host.update(auth='password',key_path='')
        config=save_config(destination,site)
        vault=open_credentials(destination);vault.data=data['vault'];vault.data['ssh']={};vault.save()
        atomic_json(destination/'recovery-source.json',{'version':data['version'],'manifest':data['manifest'],'installation_id':vault.data['installation_id'],**({'component_versions':data['component_versions']} if 'component_versions' in data else {})})
        return config


class Maintenance:
    def __init__(self,engine):self.e=engine;self.remotes={};self.statuses={};self.claimed=[];self.database_metadata=None

    def action(self,role,action,**extra):
        payload=self.e.payload(role,self.operation,self.mode);payload.update(extra)
        if role in self.statuses:
            payload['version']=self.statuses[role].get('version','')
            payload['manifest']=self.statuses[role].get('manifest','')
        payload['external_activation']=bool(self.e.vault.data.get('activation'))
        self.e.log(role+': '+action)
        return self.remotes[role].action(payload,action,RESOURCES)

    def connect(self,roles):
        for role in roles:
            r=self.e.factory(role,self.e.c['hosts'][role],self.e.vault.data.get('ssh',{}).get(role,{}),self.e.log)
            self.remotes[role]=r;r.diagnostics=self.e.diagnostics;r.probe();r.prepare_stage()
            for name in (('worker.ps1','worker-service-runner.py') if role.startswith('worker') else ('linux.py','maintenance_linux.py','restore_compatibility.py','worker-grants.sql')):
                r.put_bytes(name,(RESOURCES/name).read_bytes())
            self.statuses[role]=self.action(role,'preflight')
        for role in roles:
            # A lost reply can still mean the VM acquired our lease.
            self.claimed.append(role);self.action(role,'maintenance-claim')

    def snapshot(self):
        return {r:self.action(r,'maintenance-snapshot') for r in ('app',*worker_roles(self.e.c))}

    def quiesce(self):
        self.action('app','pause');self.action('app','drain')
        for role in worker_roles(self.e.c):self.action(role,'maintenance-stop')
        self.action('app','maintenance-stop')

    def resume(self,previous):
        failures=[]
        for role in (*worker_roles(self.e.c),'app'):
            try:self.action(role,'maintenance-resume',previous_runtime=previous[role])
            except Exception:failures.append(role)
        if not failures:
            for role in (*worker_roles(self.e.c),'app'):
                try:self.action(role,'maintenance-health',previous_runtime=previous[role])
                except Exception:failures.append(role)
        if failures:
            if self.mode=='restore':
                for role in (*worker_roles(self.e.c),'app'):
                    try:self.action(role,'maintenance-stop')
                    except Exception:self.e.log(role+': could not stop after incomplete restart; inspect this VM')
            raise RuntimeError('Could not restart: '+', '.join(failures)+'. Retry this operation.')

    def download(self,role,folder):
        result=self.action(role,'data-export');expected={'prodcast2.dump'} if role=='db' else {'media.tar.gz','configuration.tar.gz'}
        if set(result['files'])!=expected:raise ValueError('Unexpected backup files returned by server')
        if role=='db':self.database_metadata=archive.validate_database_metadata(result.get('database'))
        r=self.remotes[role]
        for name,spec in result['files'].items():
            target=folder/role/name;target.parent.mkdir(parents=True,exist_ok=True)
            with r.client.open_sftp() as s:s.get(r.stage+'/'+name,str(target))
            os.chmod(target,0o600)
            if target.stat().st_size!=spec['bytes'] or archive.sha(target)!=spec['sha256']:raise ValueError('Downloaded backup checksum mismatch')

    def run(self,mode,path='',password=''):
        if mode not in ('export','restore','reset-keep','reset-full'):raise ValueError('Unknown maintenance mode')
        if self.e.vault.data.get('topology')!=topology_hash(self.e.c):raise ValueError('Use the original installation profile or import it from a backup')
        self.mode=mode;local=self.e.dir/'maintenance-journal.json'
        with file_lock(self.e.dir/'operation.lock'):
            from .workers import require_no_expansion
            require_no_expansion(self.e.dir)
            prior=json.loads(local.read_text('utf-8')) if local.exists() else {}
            original_prior=copy.deepcopy(prior)
            recovered_export=prior.get('mode')=='export' and prior.get('status')=='failed' and not journal_blocks_changes('maintenance-journal.json',prior)
            pending=prior.get('status') in ('running','failed')
            if pending and prior['mode']!=mode:
                if journal_blocks_changes('maintenance-journal.json',prior):raise ValueError('Resume the previous maintenance operation first: '+prior['mode'])
                atomic_json(self.e.dir/'history'/('maintenance-'+prior['operation']+'.json'),prior)
                pending=False
            self.operation=prior['operation'] if pending else uuid.uuid4().hex
            journal=prior if pending else {'mode':mode,'operation':self.operation,'steps':[]}
            journal['status']='running';atomic_json(local,journal)
            roles=list(core_roles(self.e.c))
            if mode.startswith('reset-') and self.e.c.get('install_ai',True):roles.append('ai')
            try:
                if mode=='export' and not journal.get('backup_destination'):
                    if not str(path).strip() or not Path(path).name or Path(path).is_dir():raise ValueError('Choose a backup file destination before exporting')
                    destination=Path(path)
                    if not destination.parent.is_dir():raise ValueError('The backup destination folder does not exist')
                    if destination.exists() or destination.with_name(destination.name+'.partial').exists():raise ValueError('Choose a new backup filename')
                self.connect(roles)
                if journal.get('work_complete'):
                    pass  # Only lease release was interrupted; never repeat data changes.
                elif mode=='export' and journal.get('backup_destination'):
                    saved=Path(journal['backup_destination'])
                    if Path(path).resolve()!=saved or not saved.is_file() or archive.sha(saved)!=journal['backup_sha256']:
                        raise ValueError('Resume with the already exported backup file, unchanged')
                    self.resume(journal['previous_runtime'])
                    journal['services_recovered']=True
                elif mode.startswith('reset-'):
                    for role in [*worker_roles(self.e.c),'app',*(['ai'] if 'ai' in roles else []),'db']:
                        self.action(role,'reset');journal['steps'].append(role);atomic_json(local,journal)
                    for name in (('journal.json','ai-journal.json','app-tls-journal.json') if mode=='reset-full' else ()):
                        source=self.e.dir/name
                        if source.exists():
                            destination=self.e.dir/'history'/(self.operation+'-'+name);destination.parent.mkdir(exist_ok=True);source.replace(destination)
                    # Identity/license/keys are retained locally, including after full reset.
                    self.e.log('Reset complete. '+('Use Repair with the exact installed release. For an incomplete first install, resume Install with its original release.' if mode=='reset-keep' else 'Use Install to deploy again.'))
                else:
                    components=installed_releases(self.e,self.statuses)
                    version=components['app']['version'];manifest=components['app']['manifest']
                    if journal.get('component_versions',components)!=components:
                        if mode=='export' and recovered_export and not journal.get('backup_destination'):
                            atomic_json(self.e.dir/'history'/('export-before-component-change-'+self.operation+'.json'),original_prior)
                            journal.pop('previous_runtime',None);journal.pop('quiesced',None)
                        else:raise ValueError('Installed component releases changed during maintenance; resume with the original installation')
                    journal['component_versions']=components;atomic_json(local,journal)
                    with private_workspace(self.e.dir/'temporary') as folder:
                        recovery=None
                        if mode=='restore':
                            recovery=read_backup(path,password,folder)
                            if recovery['vault']['installation_id']!=self.e.vault.data['installation_id']:raise ValueError('Backup belongs to another installation; import its profile first')
                            if archive.release_order(recovery['version'])>archive.release_order(version):raise ValueError('A newer App backup cannot be restored into an older App; install a compatible newer App first')
                            crypto_keys=('FERNET_KEY','MEDIA_KEY','SIGNING_KEY','MODULE_KEY','RESOLVE_KEY','DJANGO_KEY')
                            if any(recovery['vault']['secrets'].get(k)!=self.e.vault.data['secrets'].get(k) for k in crypto_keys):raise ValueError('Encryption keys differ; import the original backup profile before restoring')
                            media_scope=recovery.get('media_scope','legacy-full-media')
                            if media_scope not in ('legacy-full-media','application-data-without-models-v1'):raise ValueError('Unsupported backup media scope')
                            source_sha=archive.sha(path)
                            if journal.get('source_sha256',source_sha)!=source_sha:raise ValueError('Resume restore with the original backup')
                            journal['source_sha256']=source_sha
                            restore_options={}
                            for role,name in [('db','prodcast2.dump'),('app','media.tar.gz')]:
                                source=folder/'data'/role/name;self.remotes[role].put(source)
                                extra={'restore_file':{'name':name,'bytes':source.stat().st_size,'sha256':archive.sha(source)},'restore_manifest':recovery['manifest'],'media_scope':media_scope,
                                       'restore_strategy':'validated-forward-v1','restore_target':components[role],
                                       'restore_target_app_manifest':components['app']['manifest'],
                                       'restore_database_major':recovery['database_major'],'restore_database':recovery.get('database')}
                                restore_options[role]=extra
                                self.action(role,'data-validate',**extra)
                        previous=journal.get('previous_runtime') or self.snapshot()
                        journal['previous_runtime']=previous;atomic_json(local,journal)
                        export_ok=False;operation_error=None
                        try:
                            if not (mode=='restore' and journal.get('quiesced')):
                                self.quiesce();journal['quiesced']=True;atomic_json(local,journal)
                            else:
                                for role in (*worker_roles(self.e.c),'app'):self.action(role,'maintenance-stop')
                            if mode=='export':
                                self.e.log('Application-data backup: database, workflows and uploaded data sources; model files and Worker installations are excluded. Keep a separate model backup or reimport models from SMB.')
                                data=folder/'data'
                                for role in ('db','app'):self.download(role,data)
                                vault=copy.deepcopy(self.e.vault.data);vault.pop('ssh',None)
                                metadata=archive.validate_database_metadata(self.database_metadata)
                                record={'schema':4,'database_major':metadata['major'],'database':metadata,'version':version,'manifest':manifest,'component_versions':components,'site':self.e.c,'vault':vault,'media_scope':'application-data-without-models-v1'}
                                (data/'recovery.json').write_text(json.dumps(record),'utf-8')
                                archive.pack(data,folder/'backup.zip');archive.encrypt(folder/'backup.zip',path,password);export_ok=True
                                journal.update(backup_destination=str(Path(path).resolve()),backup_sha256=archive.sha(path));atomic_json(local,journal)
                            else:
                                if media_scope=='application-data-without-models-v1':self.e.log('Restoring application data; existing model directories are preserved. Restore model files separately or reimport them from SMB.')
                                # Keep the original safety copy when retrying a partial restore.
                                for role in ('db','app'):
                                    marker='safety-'+role
                                    if marker not in journal['steps']:
                                        self.action(role,'data-export',server_backup_only=True);journal['steps'].append(marker);atomic_json(local,journal)
                                # Reassert the source after safety copies, including retries from older Managers.
                                for role,name in [('db','prodcast2.dump'),('app','media.tar.gz')]:
                                    self.remotes[role].put(folder/'data'/role/name)
                                    self.action(role,'data-validate',**restore_options[role])
                                self.e.log('Staging restored App files and an isolated database. The current database is retained until migrations pass.')
                                self.action('app','data-stage-media',**restore_options['app'])
                                prepared=self.action('db','data-prepare',**restore_options['db'])
                                migration=None
                                if not prepared.get('switched'):
                                    if prepared.get('database')!='prodcast_restore_'+self.operation[:24]:raise ValueError('DB did not confirm the owned restore staging database')
                                    self.e.log('Applying installed App migrations to the restored database; checking workflows, schedules, users and graphics.')
                                    migration=self.action('app','data-migrate',restore_staging_database=prepared['database'],**restore_options['app'])
                                    if migration!={'database':prepared['database'],'migrated':True}:raise ValueError('App did not confirm compatible forward migrations')
                                self.e.log('Activating validated restored data. Previous database and App files are retained for recovery.')
                                self.action('db','data-restore',restore_migration_result=migration,**restore_options['db'])
                                self.action('app','data-restore',**restore_options['app'])
                                export_ok=True
                        except Exception as error:
                            operation_error=error
                            self.e.log(('Backup export failed: ' if mode=='export' else 'Data restore failed: ')+str(error))
                            raise
                        finally:
                            # A partially restored stack must remain stopped until a successful retry.
                            if mode=='export' or export_ok:
                                try:
                                    self.resume(previous)
                                    journal['services_recovered']=True
                                except Exception as recovery_error:
                                    journal['services_recovered']=False
                                    self.e.log('Service recovery also needs attention: '+str(recovery_error))
                                    if operation_error is None:raise
                                finally:
                                    journal['archive_saved']=bool(journal.get('backup_destination'))
                                    atomic_json(local,journal)
                        self.e.log('Backup saved: '+str(path) if mode=='export' else 'Database and App files restored.')
                journal['work_complete']=True;atomic_json(local,journal)
                for role in list(self.claimed):
                    self.action(role,'maintenance-release');self.claimed.remove(role)
                journal['status']='complete';atomic_json(local,journal)
                result={'status':'complete','mode':mode,'operation':self.operation}
                if mode=='export':
                    result.update(archive=journal['backup_destination'],bytes=Path(journal['backup_destination']).stat().st_size,services_recovered=True,models_included=False)
                return result
            except Exception:
                journal['status']='failed';atomic_json(local,journal)
                self.e.log('Maintenance stopped. Keep the profile and backup; retry the same operation after correcting the cause.')
                raise
            finally:
                for role in list(self.claimed):
                    if journal['status']!='complete' and mode in ('restore','reset-keep','reset-full'):continue
                    try:
                        self.action(role,'maintenance-release');self.claimed.remove(role)
                    except Exception:self.e.log(role+': maintenance lease retained; retry from the same profile')
                if mode=='export' and journal.get('status')=='failed':
                    journal['leases_released']=not self.claimed;atomic_json(local,journal)
                for r in self.remotes.values():
                    try:r.cleanup()
                    except Exception:pass
                    r.close()
