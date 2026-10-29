"""Owned, resumable staging for application-data restores. Standard library only."""
import ipaddress
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

STRATEGY='validated-forward-v1'
INTERNAL_MEDIA='.prodcast-restore'


def context(m):
    operation=m.P['operation']
    if not re.fullmatch('[a-f0-9]{32}',operation):raise RuntimeError('Invalid restore operation identity')
    target=m.P.get('restore_target',{})
    if target.get('version')!=m.ST.get('version') or target.get('manifest')!=m.ST.get('manifest'):
        raise RuntimeError('Installed component changed during restore; use the original target installation')
    if m.ST.get('maintenance')!=operation:raise RuntimeError('Restore requires this operation to own maintenance')
    return operation


def command(m):
    return ['docker','compose','-p','prodcast-managed-db','-f',str(m.ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres']


def database_info(m,db='prodcast2'):
    major=int(m.psql('SHOW server_version_num;').strip())//10000
    extensions=json.loads(m.psql("SELECT COALESCE(json_object_agg(extname,extversion),'{}'::json) FROM pg_extension;",db))
    migrations=json.loads(m.psql("SELECT COALESCE(json_agg(json_build_array(app,name) ORDER BY app,name),'[]'::json) FROM django_migrations;",db))
    size=int(m.psql('SELECT pg_database_size(current_database());',db).strip())
    return dict(major=major,extensions=extensions,applied_migrations=migrations,bytes=size)


def check_database(m):
    context(m)
    source_major=m.P.get('restore_database_major')
    target_major=int(m.psql('SHOW server_version_num;').strip())//10000
    if source_major!=target_major:
        raise RuntimeError('Unsupported PostgreSQL major version: backup '+str(source_major)+', installed '+str(target_major))
    metadata=m.P.get('restore_database') or {}
    installed=json.loads(m.psql("SELECT COALESCE(json_object_agg(name,default_version),'{}'::json) FROM pg_available_extensions;"))
    for name,version in metadata.get('extensions',{}).items():
        if installed.get(name)!=version:
            raise RuntimeError('Unsupported PostgreSQL extension version: '+name+'; backup '+version+', installed '+str(installed.get(name)))
    return {'database_major':target_major}


def receipt_path(m,role):
    from maintenance_linux import owned_path
    path=m.STATE/'restore-transactions'/(context(m)+'-'+role+'.json')
    owned_path(path,m.STATE)
    return path


def load_receipt(m,role):
    path=receipt_path(m,role)
    record=json.loads(path.read_text()) if path.exists() else {}
    if record and (record.get('source_sha256')!=m.P['restore_file']['sha256'] or record.get('target')!=m.P['restore_target'] or record.get('target_app_manifest')!=m.P['restore_target_app_manifest']):
        raise RuntimeError('Restore staging belongs to another source or target')
    return record


def save_receipt(m,role,record):
    m.write(receipt_path(m,role),json.dumps(record))


def initial_receipt(m):
    return dict(source_sha256=m.P['restore_file']['sha256'],target=m.P['restore_target'],target_app_manifest=m.P['restore_target_app_manifest'],phase='preparing')


def database_names(m):
    suffix=context(m)[:24]
    return 'prodcast_restore_'+suffix,'prodcast_previous_'+suffix


def database_oids(m):
    return json.loads(m.psql("SELECT COALESCE(json_object_agg(datname,oid),'{}'::json) FROM pg_database;"))


def access_rule(m,enabled):
    from maintenance_linux import owned_path
    stage,_=database_names(m)
    ip=str(ipaddress.IPv4Address(m.C['hosts']['app']['address']))
    block='# ProdCast restore '+context(m)+'\nhostssl '+stage+' prodcast_app '+ip+'/32 scram-sha-256\n'
    path=owned_path(m.ROOT/'db/config/pg_hba.conf',m.ROOT)
    original=path.read_text()
    marker='# ProdCast restore '+context(m)+'\n'
    if marker in original and block not in original:raise RuntimeError('Temporary restore access rule was modified; inspect DB policy')
    updated=original.replace(block,'')
    if enabled:
        reject='host all all 0.0.0.0/0 reject'
        if updated.count(reject)!=1:raise RuntimeError('Cannot locate the managed PostgreSQL deny rule')
        updated=updated.replace(reject,block+reject)
    if updated!=original:
        m.write(path,updated,0o644)
        m.psql('SELECT pg_reload_conf();')


def prepare_database(m):
    spec=m.P['restore_file'];path=Path(m.P['stage'])/spec['name']
    if path.stat().st_size!=spec['bytes'] or m.digest(path)!=spec['sha256']:
        raise RuntimeError('Restore file checksum mismatch before database staging')
    check_database(m)
    stage,previous=database_names(m);record=load_receipt(m,'db');oids=database_oids(m)
    if record and oids.get('prodcast2')==record.get('stage_oid') and oids.get(previous)==record.get('original_oid'):
        if record.get('prepared_source_sha256')!=spec['sha256']:raise RuntimeError('Activated restore lacks verified source staging; inspect retained database before continuing')
        record['phase']='switched';save_receipt(m,'db',record);access_rule(m,False)
        return dict(switched=True,previous_database=previous)
    if record.get('phase') in ('prepared','switching'):
        if oids.get('prodcast2')!=record.get('original_oid') or oids.get(stage)!=record.get('stage_oid'):
            raise RuntimeError('Restore database identities changed; refusing to reuse staging')
        if record.get('prepared_source_sha256')==spec['sha256']:
            m.psql('ALTER DATABASE '+stage+' WITH ALLOW_CONNECTIONS true;')
            access_rule(m,True)
            return dict(switched=False,database=stage)
        # Older Managers could overwrite the uploaded dump with their safety copy.
        # Only rebuild our temporary DB while the original DB identity is unchanged.
        record['phase']='preparing';save_receipt(m,'db',record)
    if not record:
        if stage in oids or previous in oids:raise RuntimeError('Unowned restore database name exists')
        if 'prodcast2' not in oids:raise RuntimeError('Original application database is missing')
        record=initial_receipt(m);record['original_oid']=oids['prodcast2'];save_receipt(m,'db',record)
    elif oids.get('prodcast2')!=record.get('original_oid'):
        raise RuntimeError('Original database identity changed during staging')
    size=int(m.psql("SELECT pg_database_size('prodcast2');").strip())
    needed=max(size,int((m.P.get('restore_database') or {}).get('bytes',0)))+256*1024**2
    if shutil.disk_usage(m.ROOT/'db/data/postgres').free<needed:raise RuntimeError('Not enough DB disk space for an isolated restore; original database was not replaced')
    if stage in oids:m.psql('DROP DATABASE '+stage+' WITH (FORCE);')
    m.psql('CREATE DATABASE '+stage+' OWNER prodcast_app TEMPLATE template0;')
    m.psql('REVOKE ALL ON DATABASE '+stage+' FROM PUBLIC; GRANT CONNECT ON DATABASE '+stage+' TO prodcast_app;')
    path=Path(m.P['stage'])/m.P['restore_file']['name']
    with path.open('rb') as src:
        # Worker membership may have changed since backup. Restore object ownership,
        # but discard historical grants; current Worker permissions are reapplied at cutover.
        subprocess.run(command(m)+['pg_restore','--no-acl','--single-transaction','--exit-on-error','-d',stage],stdin=src,stdout=subprocess.DEVNULL,stderr=m.LOG,check=True)
    actual=database_info(m,stage)
    source=m.P.get('restore_database')
    if source and actual['applied_migrations']!=source['applied_migrations']:
        raise RuntimeError('Restored migration history differs from the backup metadata')
    record.update(stage_oid=database_oids(m)[stage],prepared_source_sha256=spec['sha256'],phase='prepared');save_receipt(m,'db',record)
    access_rule(m,True)
    return dict(switched=False,database=stage)


def migration_code(name):
    return """import os,json
from urllib.parse import urlsplit,urlunsplit
parts=urlsplit(os.environ['DATABASE_URL'])
os.environ['DATABASE_URL']=urlunsplit((parts.scheme,parts.netloc,'/'+RESTORE_DB,parts.query,parts.fragment))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','mainapp.settings')
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.core.management import call_command
assert connection.settings_dict['NAME']==RESTORE_DB, 'Migration connection is not the staging database'
executor=MigrationExecutor(connection)
known=set(executor.loader.disk_migrations)
replaced={old for migration in executor.loader.disk_migrations.values() for old in migration.replaces}
unknown=set(MigrationRecorder(connection).applied_migrations())-known-replaced
if unknown:raise RuntimeError('Backup has migrations unsupported by the installed App: '+repr(sorted(unknown)))
executor.loader.check_consistent_history(connection)
plan=executor.migration_plan(executor.loader.graph.leaf_nodes())
if any(backwards for migration,backwards in plan):raise RuntimeError('Restore would require reverse migrations')
tables=('auth_user','apiapp_workflow','apiapp_workflow_scheduler','apiapp_graphics_config')
present=set(connection.introspection.table_names())
before={}
with connection.cursor() as cursor:
 for table in tables:
  if table in present:
   cursor.execute('SELECT count(*) FROM '+connection.ops.quote_name(table));before[table]=cursor.fetchone()[0]
call_command('migrate',interactive=False,verbosity=0)
call_command('sync_integration_plugins','--package','prodcast-petex-plugins',verbosity=0)
executor=MigrationExecutor(connection)
assert not executor.migration_plan(executor.loader.graph.leaf_nodes()), 'Migrations remain unapplied'
with connection.cursor() as cursor:
 for table,count in before.items():
  cursor.execute('SELECT count(*) FROM '+connection.ops.quote_name(table))
  assert cursor.fetchone()[0]>=count, 'Restore migration removed business records from '+table
connection.close()
print('PRODCAST_RESTORE_RESULT:'+json.dumps({'database':RESTORE_DB,'migrated':True}))
""".replace('RESTORE_DB',repr(name))


def migrate_database(m):
    context(m)
    name=m.P.get('restore_staging_database','')
    if name!=database_names(m)[0]:raise RuntimeError('Unexpected staging database name')
    output=m.app_python(migration_code(name),current=True)
    replies=[json.loads(line.split(':',1)[1]) for line in output.splitlines() if line.startswith('PRODCAST_RESTORE_RESULT:')]
    if replies!=[{'database':name,'migrated':True}]:raise RuntimeError('App did not confirm staged database migrations')
    return replies[0]


def promote_database(m):
    context(m)
    stage,previous=database_names(m);record=load_receipt(m,'db');oids=database_oids(m)
    if not record:raise RuntimeError('No prepared restore database')
    if record.get('prepared_source_sha256')!=m.P['restore_file']['sha256']:raise RuntimeError('Restore database source has not been verified; retry database staging')
    if oids.get('prodcast2')==record.get('stage_oid') and oids.get(previous)==record.get('original_oid'):
        access_rule(m,False)
        restore_grants(m)
        return {'restored':True,'previous_database':previous}
    if record.get('phase') not in ('prepared','switching') or oids.get('prodcast2')!=record.get('original_oid') or oids.get(stage)!=record.get('stage_oid') or previous in oids:
        raise RuntimeError('Database identities do not match the prepared restore')
    proof=m.P.get('restore_migration_result',{})
    if proof!={'database':stage,'migrated':True}:raise RuntimeError('Installed App has not validated this staging database')
    record['phase']='switching';save_receipt(m,'db',record)
    m.psql('ALTER DATABASE prodcast2 WITH ALLOW_CONNECTIONS false; ALTER DATABASE '+stage+' WITH ALLOW_CONNECTIONS false;')
    m.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('prodcast2','"+stage+"') AND pid<>pg_backend_pid();")
    m.psql('BEGIN; ALTER DATABASE prodcast2 RENAME TO '+previous+'; ALTER DATABASE '+stage+' RENAME TO prodcast2; ALTER DATABASE prodcast2 WITH ALLOW_CONNECTIONS true; COMMIT;')
    record['phase']='switched';save_receipt(m,'db',record);access_rule(m,False)
    restore_grants(m)
    return {'restored':True,'previous_database':previous}


def restore_grants(m):
    workers=['prodcast_worker_'+n for n in m.worker_numbers()]
    if any(not re.fullmatch('prodcast_worker_[0-9]{2}',name) for name in workers):raise RuntimeError('Invalid Worker grant target')
    m.psql('REVOKE ALL ON DATABASE prodcast2 FROM PUBLIC; GRANT CONNECT ON DATABASE prodcast2 TO '+','.join(['prodcast_app']+workers)+';')
    m.psql(m.worker_grants(),'prodcast2')


def prepare_media(m):
    from maintenance_linux import owned_path,validate_media,MEDIA_SCOPE,MODEL_DIRECTORIES
    context(m);record=load_receipt(m,'app')
    info=json.loads(m.run(['docker','volume','inspect','prodcast-managed-media']))[0]
    volume=Path(info['Mountpoint']);owned_path(volume,volume)
    transaction=owned_path(volume/INTERNAL_MEDIA/context(m),volume)
    stage=transaction/'media';previous=transaction/'previous'
    if record and record.get('phase') in ('prepared','moving-old','moving-new','switched'):
        if record.get('volume')!=str(volume):raise RuntimeError('App media volume changed during restore')
        if record.get('prepared_source_sha256')==m.P['restore_file']['sha256']:return {'prepared':True}
        if record['phase']!='prepared':raise RuntimeError('Media cutover lacks verified source staging; inspect retained media before continuing')
    path=Path(m.P['stage'])/m.P['restore_file']['name']
    spec=m.P['restore_file']
    if path.stat().st_size!=spec['bytes'] or m.digest(path)!=spec['sha256']:raise RuntimeError('Restore file checksum mismatch before media staging')
    exclude=m.P.get('media_scope')==MEDIA_SCOPE
    validate_media(path,exclude_models=exclude)
    with tarfile.open(path,'r:gz') as tar:
        needed=sum(member.size for member in tar if member.isfile())+64*1024**2
    if shutil.disk_usage(volume).free<needed:raise RuntimeError('Not enough App media disk space to stage restore')
    transaction.mkdir(parents=True,exist_ok=True);transaction.chmod(0o700)
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir()
    with tarfile.open(path,'r:gz') as tar:
        for member in tar:
            target=stage/member.name
            if member.isdir():target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(member) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
            os_chmod_chown(target,member)
    previous.mkdir(exist_ok=True)
    record=initial_receipt(m)
    record.update(volume=str(volume),prepared_source_sha256=spec['sha256'],old_entries=sorted(p.name for p in volume.iterdir() if p.name!=INTERNAL_MEDIA and not (exclude and p.name in MODEL_DIRECTORIES)),new_entries=sorted(p.name for p in stage.iterdir()),phase='prepared')
    save_receipt(m,'app',record)
    return {'prepared':True}


def os_chmod_chown(path,member):
    import os
    os.chmod(path,member.mode & 0o777);os.chown(path,member.uid,member.gid)


def promote_media(m):
    from maintenance_linux import owned_path
    context(m);record=load_receipt(m,'app')
    if not record:raise RuntimeError('No prepared media restore')
    if record.get('prepared_source_sha256')!=m.P['restore_file']['sha256']:raise RuntimeError('Restore media source has not been verified; retry media staging')
    volume=Path(record['volume']);owned_path(volume,volume)
    base=owned_path(volume/INTERNAL_MEDIA/context(m),volume);stage=base/'media';previous=base/'previous'
    if record['phase'] in ('prepared','moving-old'):
        record['phase']='moving-old';save_receipt(m,'app',record)
        for name in record['old_entries']:
            src=volume/name;dst=previous/name
            if dst.exists() or dst.is_symlink():
                if src.exists() or src.is_symlink():raise RuntimeError('Unexpected media appeared during restore')
                continue
            if src.exists() or src.is_symlink():src.rename(dst)
            else:raise RuntimeError('Original media disappeared during restore')
        record['phase']='moving-new';save_receipt(m,'app',record)
    if record['phase']=='moving-new':
        for name in record['new_entries']:
            src=stage/name;dst=volume/name
            if src.exists():
                if dst.exists() or dst.is_symlink():raise RuntimeError('Unexpected media target exists during restore')
                src.rename(dst)
            elif not dst.exists():raise RuntimeError('Staged media disappeared during restore')
        record['phase']='switched';save_receipt(m,'app',record)
    if record['phase']!='switched':raise RuntimeError('Unsupported media restore state')
    return {'restored':True,'previous_media':str(previous)}
