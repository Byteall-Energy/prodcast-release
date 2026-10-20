import hashlib
import json
import re
import shutil
import tempfile
import stat
import zipfile
from pathlib import Path, PurePosixPath
from .offline import PROFILE as OFFLINE_PROFILE,assemble_parts

PIN_V02='6485e6bf7d1076bef20af47e711146c94155e2d8584fc19a1a08eaee0ddb6dd4'
PROFILE='prodcast-five-vm-v1'

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def safe_zip(source,dest):
    dest=Path(dest); dest.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(source) as z:
        seen=set(); total=0
        for i in z.infolist():
            if '\\' in i.orig_filename or '\x00' in i.orig_filename: raise ValueError('Unsafe archive path')
            p=PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or ':' in i.filename or not p.parts:
                raise ValueError('Unsafe archive path')
            if stat.S_ISLNK(i.external_attr>>16): raise ValueError('Archive symlinks refused')
            key=str(p).casefold()
            if key in seen: raise ValueError('Duplicate archive path')
            seen.add(key); total+=i.file_size
            if total>20*1024**3 or i.file_size>8*1024**3 or len(seen)>20000: raise ValueError('Archive exceeds limits')
        for i in z.infolist():
            p=dest.joinpath(*PurePosixPath(i.filename).parts)
            if not p.resolve().is_relative_to(dest.resolve()): raise ValueError('Archive path escapes destination through a symlink')
            if any(parent.is_symlink() for parent in [p,*p.parents] if parent!=dest and parent.is_relative_to(dest)):
                raise ValueError('Extraction into symlink refused')
            if p.exists() and p.is_symlink(): raise ValueError('Extraction into symlink refused')
            if i.is_dir(): p.mkdir(parents=True,exist_ok=True)
            else:
                p.parent.mkdir(parents=True,exist_ok=True)
                with z.open(i) as src,p.open('wb') as dst: shutil.copyfileobj(src,dst)

class Release:
    def __init__(self,path,cache, trusted_manifest='',validate_ai=True,model_path='',ollama_path=''):
        self.model_path=Path(model_path) if model_path else None
        self.ollama_path=Path(ollama_path) if ollama_path else None
        self.cache=Path(cache)
        self.ollama_root=None
        self.model_bundle=None
        path=assemble_parts(Path(path),cache)
        if path.is_file():
            dest=Path(cache)/('release-'+sha(path)[:20])
            if not (dest/'.extracted').exists():
                safe_zip(path,dest); (dest/'.extracted').write_text('ok')
            path=dest
        candidates=list(path.glob('release-manifest.json')) or list(path.glob('*/release-manifest.json'))
        if len(candidates)!=1: raise ValueError('Select complete release ZIP or directory containing release-manifest.json')
        self.root=candidates[0].parent; self.digest=sha(candidates[0])
        self.doc=json.loads(candidates[0].read_text('utf-8-sig'))
        self.version=self.doc['version']
        if not re.fullmatch(r'v[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-rc\.[0-9]+)?',self.version): raise ValueError('Unsupported release version')
        self.offline=self.doc.get('management',{}).get('profile')==OFFLINE_PROFILE
        self.offline_spec=self.doc.get('management',{}).get('offline',{}) if self.offline else {}
        self.offline_platforms={}
        self.external_activation=self.doc.get('management',{}).get('profile') in ('prodcast-five-vm-v2',OFFLINE_PROFILE)
        if self.digest!=PIN_V02:
            if not trusted_manifest or self.digest!=trusted_manifest.lower(): raise ValueError('Unknown release: supply publisher-verified manifest SHA256')
            m=self.doc.get('management',{})
            if (m.get('profile'),m.get('schema'),m.get('minimum_manager')) not in ((PROFILE,1,'0.1.0'),('prodcast-five-vm-v2',2,'0.2.0'),(OFFLINE_PROFILE,3,'0.3.0'),(OFFLINE_PROFILE,3,'0.3.1'),(OFFLINE_PROFILE,3,'0.3.2'),(OFFLINE_PROFILE,3,'0.4.0'),(OFFLINE_PROFILE,3,'0.6.4')) or m.get('python')!='3.14.7' or m.get('database_major')!=18:
                raise ValueError('Release needs a newer manager/adapter')
            if m.get('migration_policy')!='forward-only' or not isinstance(m.get('upgrade_from'),list): raise ValueError('Missing upgrade contract')
        elif self.version!='v0.2': raise ValueError('Invalid pinned release')
        self.assets={a['name']:a for a in self.doc['artifacts']}
        for a in self.assets.values():
            if not re.fullmatch(r'[A-Za-z0-9._-]+',a['name']): raise ValueError('Invalid asset name')
        self.files={}
        names=['app-deployment','backend-linux-amd64','frontend-linux-amd64','gateway-linux-amd64',
               'ai-deployment','ai-linux-amd64','license-deployment','license-linux-amd64','worker-windows-amd64']
        if self.external_activation:
            if any(c.get('component')=='license' or c.get('name')=='prodcast-license' for c in self.doc['components']) or any('license' in n.lower() for n in self.assets):
                raise ValueError('External activation release must not bundle an authority')
            names=[n for n in names if not n.startswith('license-')]
        for key in names:
            comp,suffix=key.split('-',1); ext='.zip' if comp=='worker' else '.tar.gz'
            n=f'prodcast-{comp}-{self.version}-{suffix}{ext}'
            a=self.assets.get(n)
            if (comp!='ai' or validate_ai) and (not a or sha(self.root/n)!=a['sha256']): raise ValueError(f'Missing/corrupt release asset: {n}')
            self.files[key]=self.root/n
        self.images={i['name']:i for c in self.doc['components'] for i in c.get('images',[])}
        for n in ('backend','frontend','gateway','license','ai'):
            if n=='license' and self.external_activation:continue
            if n=='ai' and not validate_ai:continue
            i=self.images['prodcast-'+n]
            if not re.fullmatch(r'ghcr\.io/(?:saatchi190499|byteall-energy)/prodcast-[a-z]+:[A-Za-z0-9._-]+',i['transport_tag']): raise ValueError('Unexpected image reference')
        self.allowed_from=self.doc.get('management',{}).get('upgrade_from',['v0.2'])
        if self.offline:
            spec=self.offline_spec
            if spec.get('schema')!=1 or not spec.get('linux_packages'):raise ValueError('Incomplete offline dependency contract')
            required=list(spec['linux_packages'].values())+[spec.get(k) for k in ('db_images','ollama_runtime','model_archive')]
            if not all(isinstance(n,str) and n in self.assets for n in required):raise ValueError('Missing offline payload declaration')
            if spec.get('model_name')!='qwen3:4b-instruct' or not re.fullmatch(r'sha256:[0-9a-f]{64}',spec.get('model_digest','')):raise ValueError('Unsupported offline model contract')
            bundle=spec.get('external_ollama')
            if bundle is not None:
                if (not isinstance(bundle,dict) or not re.fullmatch(r'[A-Za-z0-9._-]+\.zip',bundle.get('name',''))
                        or not re.fullmatch(r'[0-9a-f]{64}',bundle.get('sha256',''))
                        or type(bundle.get('bytes')) is not int or bundle['bytes']<=0):
                    raise ValueError('Invalid external Ollama components contract')
                if spec['ollama_runtime']==spec['model_archive'] or any(n in list(spec['linux_packages'].values())+[spec['db_images']] for n in (spec['ollama_runtime'],spec['model_archive'])):
                    raise ValueError('Ollama components overlap with core payloads')
                if validate_ai:self.prepare_ollama()
            for name in required:
                if not validate_ai and name in (spec['ollama_runtime'],spec['model_archive']):continue
                if sha(self.offline_file(name))!=self.assets[name]['sha256']:raise ValueError('Corrupt offline payload: '+name)
            external=spec.get('external_model')
            if external and (not re.fullmatch(r'[0-9a-f]{64}',external.get('sha256','')) or type(external.get('bytes')) is not int or external['bytes']<=0):
                raise ValueError('Invalid external AI model contract')

    def prepare_ollama(self):
        spec=self.offline_spec.get('external_ollama')
        if not spec:return
        path=self.ollama_path
        if path is None or not path.is_file():raise ValueError('Select the Ollama components ZIP from this release. The AI model is a separate file.')
        expected={self.offline_spec['ollama_runtime'],self.offline_spec['model_archive']}
        # The trusted release manifest authenticates the inner payloads. ZIP
        # timestamps/compression and old release filenames need not match.
        digest=sha(path)
        dest=self.cache/('ollama-'+digest[:20]+'-'+self.digest[:12])
        self.cache.mkdir(parents=True,exist_ok=True)
        if not (dest/'.extracted').exists():
            if not zipfile.is_zipfile(path):
                raise ValueError('Ollama components checksum mismatch: not a valid ZIP')
            with zipfile.ZipFile(path) as z:
                infos=z.infolist()
                if len(infos)!=len(expected) or len({i.filename.casefold() for i in infos})!=len(infos):
                    raise ValueError('Unexpected files in Ollama components ZIP')
                for i in infos:
                    if (PurePosixPath(i.filename).name!=i.filename or ':' in i.filename or '\\' in i.orig_filename
                            or '\x00' in i.orig_filename or i.is_dir() or stat.S_ISLNK(i.external_attr>>16)):
                        raise ValueError('Unexpected files in Ollama components ZIP')
                    if i.file_size not in {self.assets[n]['bytes'] for n in expected}:
                        raise ValueError('Missing/corrupt AI offline payload: unexpected size')
                with tempfile.TemporaryDirectory(prefix='ollama-check-',dir=self.cache) as temporary:
                    remaining=set(expected)
                    for index,i in enumerate(infos):
                        target=Path(temporary)/str(index);h=hashlib.sha256();size=0
                        with z.open(i) as src,target.open('wb') as dst:
                            for block in iter(lambda:src.read(1024*1024),b''):
                                size+=len(block)
                                if size>i.file_size:raise ValueError('Ollama payload exceeds declared size')
                                h.update(block);dst.write(block)
                        matches=[n for n in remaining if self.assets[n]['sha256']==h.hexdigest() and self.assets[n]['bytes']==size]
                        if len(matches)!=1:raise ValueError('Missing/corrupt AI offline payload: '+i.filename)
                        name=matches[0];remaining.remove(name);target.rename(Path(temporary)/name)
                    dest.mkdir(parents=True,exist_ok=True)
                    for name in expected:shutil.copyfile(Path(temporary)/name,dest/name)
                    (dest/'.extracted').write_text('ok')
        for name in expected:
            if not (dest/name).is_file() or sha(dest/name)!=self.assets[name]['sha256']:
                raise ValueError('Missing/corrupt AI offline payload: '+name)
        self.ollama_root=dest

    def offline_file(self,name):
        if self.offline_spec.get('external_ollama') and name in (self.offline_spec['ollama_runtime'],self.offline_spec['model_archive']):
            if self.ollama_root is None:self.prepare_ollama()
            return self.ollama_root/name
        return self.root/name

    def model_file(self):
        spec=self.offline_spec['external_model'];path=self.model_path
        if path is None or not path.is_file():raise ValueError('Select the separately downloaded Ollama model file. Download it before entering the offline network.')
        if not re.fullmatch(r'[A-Za-z0-9._-]+',path.name):raise ValueError('Use a model filename containing only letters, digits, dots, hyphens or underscores')
        if path.stat().st_size!=spec['bytes'] or sha(path)!=spec['sha256']:raise ValueError('AI model checksum mismatch. Use the official model file linked in this release.')
        return path

    def prepare_models(self):
        if self.model_path and self.model_path.suffix.lower()=='.zip':
            if self.model_bundle is not None and self.model_bundle.is_file():return {'offline-model_bundle':self.model_bundle}
            from .ai_models import prepare_bundle
            self.model_bundle=prepare_bundle(self.model_path,self.cache,self.offline_spec.get('external_model'),self.offline_spec['model_digest'])
            return {'offline-model_bundle':self.model_bundle}
        return {'offline-model_blob':self.model_file()} if self.offline_spec.get('external_model') else {}

    def select_platform(self,role,osinfo):
        if not self.offline or role.startswith('worker'):return
        key=osinfo['ID']+':'+osinfo['VERSION_ID']
        if osinfo['ID']=='rhel' and osinfo['VERSION_ID'].split('.')[0]=='9':key='rhel:9'
        if key not in self.offline_spec['linux_packages']:raise ValueError('Offline dependencies are not included for '+key)
        self.offline_platforms[role]=key

    def validate_worker_count(self,count):
        if count<=2:return
        asset=self.assets[self.files['worker-windows-amd64'].name]
        if asset['sha256']=='68a8ca761fe02fe0c8899b8e05823c787f661cba7ccd59ba83c8a7427014cd3e':return
        if self.doc.get('management',{}).get('maximum_workers',2)>=count:return
        raise ValueError('This Worker release supports only two Workers. Use v0.3.0-rc.1 or a release with an explicit maximum_workers contract.')

    def for_role(self,role):
        if role=='ai':
            for key in ('ai-deployment','ai-linux-amd64'):
                path=self.files[key];asset=self.assets.get(path.name)
                if not asset or not path.is_file() or sha(path)!=asset['sha256']:raise ValueError('Missing/corrupt AI asset: '+path.name)
            image=self.images.get('prodcast-ai',{})
            if not re.fullmatch(r'ghcr\.io/(?:saatchi190499|byteall-energy)/prodcast-[a-z]+:[A-Za-z0-9._-]+',image.get('transport_tag','')):raise ValueError('Unexpected AI image reference')
            if self.offline:
                self.prepare_ollama()
                for key in ('ollama_runtime','model_archive'):
                    name=self.offline_spec[key];path=self.offline_file(name)
                    if not path.is_file() or sha(path)!=self.assets[name]['sha256']:raise ValueError('Missing/corrupt AI offline payload: '+name)
        if role.startswith('worker'):return {'worker-windows-amd64':self.files['worker-windows-amd64']}
        keys={'db':[], 'ai':['ai-deployment','ai-linux-amd64'],
              'app':['app-deployment','backend-linux-amd64','frontend-linux-amd64','gateway-linux-amd64','license-deployment','license-linux-amd64'],
              'worker1':['worker-windows-amd64'],'worker2':['worker-windows-amd64']}[role]
        result={k:self.files[k] for k in keys if not (self.external_activation and k.startswith('license-'))}
        if self.offline:
            spec=self.offline_spec;key=self.offline_platforms.get(role)
            packages={key:spec['linux_packages'][key]} if key else spec['linux_packages']
            result.update({'offline-linux-'+k.replace(':','-'):self.root/n for k,n in packages.items()})
            for item in (['db_images'] if role=='db' else ['ollama_runtime','model_archive'] if role=='ai' else []):
                result['offline-'+item]=self.offline_file(spec[item])
            if role=='ai':result.update(self.prepare_models())
        return result
