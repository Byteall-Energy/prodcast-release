"""Data-only backup/restore and scoped removal of Manager-owned components."""
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path,PurePosixPath

MODEL_DIRECTORIES={'models_files','official_models'}
MEDIA_SCOPE='application-data-without-models-v1'


def owned_path(path,root):
    path=Path(path);root=Path(root)
    if not path.is_relative_to(root) or path==root.parent:raise RuntimeError('Unsafe managed path')
    for part in [path,*path.parents]:
        if part.is_symlink():raise RuntimeError('Managed path contains a symlink')
    if path.resolve()!=path.absolute():raise RuntimeError('Unexpected managed path resolution')
    return path


def projects(m):return ['prodcast-managed-'+m.P['role']]+(['prodcast-managed-license'] if m.P['role']=='app' else [])


def containers(m):
    if not shutil.which('docker'):return []
    found=[]
    for project in projects(m):
        found+=m.run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project]).split()
    if any(not re.fullmatch('[a-f0-9]{12,64}',i) for i in found):raise RuntimeError('Invalid container ID')
    return found


def export_data(m):
    if not m.ST.get('version'):raise RuntimeError('Complete the installation before exporting data')
    folder=m.STATE/'backups'/m.P['operation'];owned_path(folder,m.STATE);folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o700)
    names=[]
    if m.P['role']=='db':
        from restore_compatibility import database_info
        file=folder/'prodcast2.dump'
        command=['docker','compose','-p','prodcast-managed-db','-f',str(m.ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres']
        with file.open('wb') as dst:subprocess.run(command+['pg_dump','-Fc','-d','prodcast2'],stdout=dst,stderr=m.LOG,check=True)
        with file.open('rb') as src:subprocess.run(command+['pg_restore','--list'],stdin=src,stdout=subprocess.DEVNULL,stderr=m.LOG,check=True)
        names=['prodcast2.dump']
    elif m.P['role']=='app':
        volume=json.loads(m.run(['docker','volume','inspect','prodcast-managed-media']))[0]['Mountpoint']
        m.run(['tar','-czf',folder/'media.tar.gz','--exclude=./models_files','--exclude=./official_models','--exclude=./.prodcast-restore','-C',volume,'.'])
        m.run(['tar','-czf',folder/'configuration.tar.gz','-C',m.old_target(),'app'])
        names=['media.tar.gz','configuration.tar.gz']
    else:raise RuntimeError('Data export is supported only on App and DB')
    if m.P.get('server_backup_only'):
        return {'server_backup':str(folder),'staged':False}
    # Only these files become readable by the authenticated SSH account.
    stage=Path(m.P['stage']);uid=stage.stat().st_uid;gid=stage.stat().st_gid
    output={}
    for name in names:
        dst=stage/name;shutil.copyfile(folder/name,dst);dst.chmod(0o600);os.chown(dst,uid,gid)
        output[name]={'bytes':dst.stat().st_size,'sha256':m.digest(dst)}
    result={'files':output,'server_backup':str(folder)}
    if m.P['role']=='db':result['database']=database_info(m)
    return result


def validate_media(path,exclude_models=False):
    seen=set()
    with tarfile.open(path,'r:gz') as tar:
        for member in tar:
            name=PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):raise RuntimeError('Unsafe media backup entry')
            if name.parts and name.parts[0]=='.prodcast-restore':raise RuntimeError('Reserved restore directory in media backup')
            if exclude_models and name.parts and name.parts[0] in MODEL_DIRECTORIES:raise RuntimeError('Model files are not part of this application-data backup')
            key=str(name)
            if key in seen:raise RuntimeError('Duplicate media backup entry')
            seen.add(key)


def validate_restore(m):
    scope=m.P.get('media_scope','legacy-full-media')
    if scope not in ('legacy-full-media',MEDIA_SCOPE):raise RuntimeError('Unsupported backup media scope')
    spec=m.P['restore_file'];name=spec['name']
    expected='prodcast2.dump' if m.P['role']=='db' else 'media.tar.gz'
    if name!=expected:raise RuntimeError('Unexpected restore file')
    path=Path(m.P['stage'])/name
    if path.stat().st_size!=spec['bytes'] or m.digest(path)!=spec['sha256']:raise RuntimeError('Restore file checksum mismatch')
    staged=m.P.get('restore_strategy')=='validated-forward-v1'
    if staged:
        from restore_compatibility import context,check_database
        context(m)
    elif m.ST.get('manifest')!=m.P['restore_manifest']:raise RuntimeError('Install the exact backup release before restoring')
    if m.P['role']=='db':
        cmd=['docker','compose','-p','prodcast-managed-db','-f',str(m.ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres','pg_restore','--list']
        with path.open('rb') as src:listing=subprocess.run(cmd,stdin=src,stdout=subprocess.PIPE,stderr=m.LOG,text=True,check=True).stdout
        if staged:
            check_database(m)
            source=re.search(r'^;\s*Dumped from database version:\s*(\d+)',listing,re.M)
            if not source or int(source[1])!=m.P['restore_database_major']:raise RuntimeError('Backup database version metadata does not match its dump')
    else:validate_media(path,exclude_models=scope==MEDIA_SCOPE)
    return {'validated':True}


def restore_data(m):
    validate_restore(m)
    if m.P.get('restore_strategy')=='validated-forward-v1':
        from restore_compatibility import promote_database,promote_media
        return promote_database(m) if m.P['role']=='db' else promote_media(m)
    path=Path(m.P['stage'])/m.P['restore_file']['name']
    if m.P['role']=='db':
        # App and Workers are stopped by the coordinator. No role passwords are overwritten.
        cmd=['docker','compose','-p','prodcast-managed-db','-f',str(m.ROOT/'db/compose.json'),'exec','-T','--user','postgres','postgres']
        m.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='prodcast2' AND pid<>pg_backend_pid();")
        with path.open('rb') as src:
            subprocess.run(cmd+['pg_restore','--clean','--if-exists','--single-transaction','--exit-on-error','-d','prodcast2'],stdin=src,stdout=subprocess.DEVNULL,stderr=m.LOG,check=True)
    else:
        info=json.loads(m.run(['docker','volume','inspect','prodcast-managed-media']))[0]
        volume=Path(info['Mountpoint']);owned_path(volume,volume)
        # A server-side safety copy has already been made before this point.
        for child in volume.iterdir():
            if child.name=='.prodcast-restore' or m.P.get('media_scope')==MEDIA_SCOPE and child.name in MODEL_DIRECTORIES:continue
            if child.is_symlink() or child.is_file():child.unlink()
            elif child.is_dir():shutil.rmtree(child)
            else:raise RuntimeError('Unexpected media entry')
        with tarfile.open(path,'r:gz') as tar:
            for member in tar:
                target=volume/member.name
                if member.isdir():target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with tar.extractfile(member) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
                os.chmod(target,member.mode & 0o777);os.chown(target,member.uid,member.gid)
    return {'restored':True}


def reset(m):
    if not m.ST:return {'reset':True,'already_clean':True}
    full=m.P['mode']=='reset-full'
    ids=containers(m)
    if ids:
        m.run(['docker','stop','--time','90',*ids])
        m.run(['docker','rm',*ids])
    if m.P['role']=='ai' and Path('/etc/systemd/system/prodcast-managed-ollama.service').exists():
        m.run(['systemctl','disable','--now','prodcast-managed-ollama'])
    if full:
        if m.P['role']=='app' and shutil.which('docker'):
            available=m.run(['docker','volume','ls','--format','{{.Name}}']).split()
            for name in ('prodcast-managed-media','prodcast-managed-static','prodcast-app-installation-data'):
                if name in available:
                    info=json.loads(m.run(['docker','volume','inspect',name]))[0]
                    if info.get('Labels',{}).get('com.docker.compose.project')!='prodcast-managed-app':raise RuntimeError('Unowned App volume; reset refused')
                    m.run(['docker','volume','rm',name])
            networks=m.run(['docker','network','ls','--format','{{.Name}}']).split()
            if 'prodcast-managed-app' in networks:m.run(['docker','network','rm','prodcast-managed-app'])
        owned_path(m.ROOT,m.ROOT)
        if m.ROOT.exists():shutil.rmtree(m.ROOT)
        if m.P['role']=='ai':
            for p in (m.OLLAMA_HOME,Path('/opt/prodcast-ollama')):
                owned_path(p,p)
                if p.exists():shutil.rmtree(p)
            Path('/etc/systemd/system/prodcast-managed-ollama.service').unlink(missing_ok=True)
            m.run(['systemctl','daemon-reload'])
        for name in ('app-tls-transaction.json','license-client.json'):(m.STATE/name).unlink(missing_ok=True)
        activation=m.STATE/'app-activation';owned_path(activation,m.STATE)
        if activation.exists():shutil.rmtree(activation)
        (m.STATE/'state.json').unlink(missing_ok=True)
    else:
        m.ST.update(operation='',steps={},step_tls={});m.save()
        transaction=m.STATE/'app-tls-transaction.json'
        if transaction.exists():transaction.rename(m.STATE/('app-tls-abandoned-'+m.P['operation']+'.json'))
    return {'reset':True,'data_preserved':not full,'dependencies_preserved':True,'server_backups_preserved':True}


def dispatch(m):
    action=m.P['action']
    if action=='maintenance-claim':
        if m.ST.get('operation') and not m.P['mode'].startswith('reset-'):raise RuntimeError('Complete or reset the interrupted deployment before backing up or restoring')
        if m.ST:m.ST['maintenance']=m.P['operation'];m.save()
        return {'claimed':True}
    if action=='maintenance-release':
        if m.ST:m.ST.pop('maintenance',None);m.save()
        return {'released':True}
    if action=='maintenance-snapshot':
        running=[]
        for project in projects(m):
            if shutil.which('docker'):running+=m.run(['docker','ps','-q','--filter','label=com.docker.compose.project='+project]).split()
        return {'running':running}
    if action=='maintenance-health':
        ids=m.P['previous_runtime']['running'];owned=set(containers(m))
        if any(i not in owned for i in ids):raise RuntimeError('Original containers changed during maintenance')
        for attempt in range(60):
            states=[json.loads(line) for line in m.run(['docker','inspect','--format','{{json .State}}',*ids]).splitlines()] if ids else []
            if all(s['Running'] and s.get('Health',{}).get('Status','healthy')=='healthy' for s in states):return {'healthy':True}
            time.sleep(3)
        raise RuntimeError('App containers are not healthy after restart')
    if action=='data-export':return export_data(m)
    if action in ('data-prepare','data-stage-media','data-migrate'):
        from restore_compatibility import prepare_database,prepare_media,migrate_database
        if (action=='data-prepare' and m.P['role']!='db') or (action!='data-prepare' and m.P['role']!='app'):raise RuntimeError('Invalid restore action target')
        return {'data-prepare':prepare_database,'data-stage-media':prepare_media,'data-migrate':migrate_database}[action](m)
    if action=='data-validate':return validate_restore(m)
    if action=='data-restore':return restore_data(m)
    if action=='reset':return reset(m)
    if action=='maintenance-stop':
        ids=containers(m)
        if ids:m.run(['docker','stop','--time','90',*ids])
        return {'stopped':True}
    if action=='maintenance-resume':
        owned=set(containers(m));ids=m.P['previous_runtime']['running']
        if any(i not in owned for i in ids):raise RuntimeError('Original containers changed during maintenance')
        if ids:m.run(['docker','start',*ids])
        return {'started':True}
    raise RuntimeError('Unknown maintenance operation')
