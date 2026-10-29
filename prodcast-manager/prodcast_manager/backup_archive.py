"""Password-protected, streaming portable backup format (no model/runtime files)."""
import hashlib
import json
import os
import struct
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC=b'PRODCAST-BACKUP-1\n'
CHUNK=1024*1024
FILES={'recovery.json','manifest.json','db/prodcast2.dump','app/media.tar.gz','app/configuration.tar.gz'}


def key(password,salt):
    if not isinstance(password,str) or len(password)<12:raise ValueError('Backup password must contain at least 12 characters')
    return Scrypt(salt=salt,length=32,n=2**17,r=8,p=1).derive(password.encode())


def encrypt(source,destination,password):
    destination=Path(destination);partial=destination.with_name(destination.name+'.partial')
    if destination.exists() or partial.exists():raise ValueError('Choose a new backup filename')
    salt=os.urandom(16);prefix=os.urandom(8);header=MAGIC+salt+prefix
    cipher=AESGCM(key(password,salt))
    try:
        with open(source,'rb') as src,partial.open('xb') as dst:
            os.chmod(partial,0o600);dst.write(header);counter=0
            while True:
                data=src.read(CHUNK);index=struct.pack('>I',counter)
                encrypted=cipher.encrypt(prefix+index,data,header+index)
                dst.write(struct.pack('>I',len(encrypted)));dst.write(encrypted)
                counter+=1
                if not data:break
            dst.flush();os.fsync(dst.fileno())
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def decrypt(source,destination,password):
    destination=Path(destination)
    if destination.exists():raise ValueError('Temporary destination already exists')
    try:
        with open(source,'rb') as src,destination.open('xb') as dst:
            os.chmod(destination,0o600)
            header=src.read(len(MAGIC)+24)
            if len(header)!=len(MAGIC)+24 or not header.startswith(MAGIC):raise ValueError('Not a ProdCast backup')
            salt=header[len(MAGIC):len(MAGIC)+16];prefix=header[-8:];cipher=AESGCM(key(password,salt));counter=0
            while True:
                size=src.read(4)
                if len(size)!=4:raise ValueError('Truncated backup')
                size=struct.unpack('>I',size)[0]
                if not 16<=size<=CHUNK+16:raise ValueError('Invalid backup frame')
                data=src.read(size)
                if len(data)!=size:raise ValueError('Truncated backup')
                index=struct.pack('>I',counter)
                plain=cipher.decrypt(prefix+index,data,header+index);counter+=1
                if not plain:
                    if src.read(1):raise ValueError('Unexpected trailing backup data')
                    break
                dst.write(plain)
    except Exception:
        destination.unlink(missing_ok=True)
        raise ValueError('Cannot open backup: wrong password, incomplete or damaged file') from None


def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(CHUNK),b''):h.update(block)
    return h.hexdigest()


def pack(folder,destination):
    folder=Path(folder)
    manifest={'schema':1,'files':{n:{'sha256':sha(folder/n),'bytes':(folder/n).stat().st_size} for n in sorted(FILES-{'manifest.json'})}}
    (folder/'manifest.json').write_text(json.dumps(manifest),'utf-8')
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_STORED,allowZip64=True) as z:
        for name in sorted(FILES):z.write(folder/name,name)


def unpack(source,destination):
    destination=Path(destination)
    with zipfile.ZipFile(source) as z:
        if len(z.infolist())!=len(FILES) or set(z.namelist())!=FILES:raise ValueError('Unexpected backup contents')
        for i in z.infolist():
            if i.orig_filename!=i.filename or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe backup entry')
            if i.file_size>1024**4:raise ValueError('Backup file is too large')
        if z.getinfo('manifest.json').file_size>16384 or z.getinfo('recovery.json').file_size>4*1024**2:raise ValueError('Backup metadata is too large')
        manifest=json.loads(z.read('manifest.json'))
        if manifest.get('schema')!=1 or set(manifest.get('files',{}))!=FILES-{'manifest.json'}:raise ValueError('Invalid backup manifest')
        import shutil
        for name in sorted(FILES-{'manifest.json'}):
            expected=manifest['files'][name]
            if expected['bytes']!=z.getinfo(name).file_size:raise ValueError('Backup size mismatch')
            path=destination/name;path.parent.mkdir(parents=True,exist_ok=True)
            with z.open(name) as src,path.open('xb') as dst:
                os.chmod(path,0o600);shutil.copyfileobj(src,dst,CHUNK)
            if sha(path)!=expected['sha256']:raise ValueError('Backup checksum mismatch')
    recovery=json.loads((destination/'recovery.json').read_text('utf-8'))
    if recovery.get('schema') not in (1,2,3) or recovery.get('database_major')!=18:raise ValueError('Unsupported recovery format')
    if recovery['schema'] in (2,3) and recovery.get('media_scope')!='application-data-without-models-v1':raise ValueError('Unsupported backup media scope')
    if recovery['schema']==3:
        components=recovery.get('component_versions')
        if not isinstance(components,dict) or not {'app','db'}<=set(components) or any(not isinstance(v,dict) or set(v)!={'version','manifest'} or not all(isinstance(x,str) and x for x in v.values()) for v in components.values()):
            raise ValueError('Invalid backup component release records')
        if components['app']!={'version':recovery.get('version'),'manifest':recovery.get('manifest')}:raise ValueError('Backup App release record mismatch')
    return recovery
