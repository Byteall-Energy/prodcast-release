"""Local UTF-8 logs live next to the executable, never in the extraction temp dir."""
import re
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from .portable import data_directory

def application_directory():
    return Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]

class SessionLog:
    def __init__(self,directory=None):
        self.directory=Path(directory) if directory else data_directory(application_directory())/'logs'
        self.directory.mkdir(parents=True,exist_ok=True)
        self.stamp=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        self.path=self.directory/('manager-'+self.stamp+'.log')
        self.lock=threading.RLock();self.secrets=set()
        self.remote_listener=None
        self.path.touch(exist_ok=False)

    def protect(self,data):
        with self.lock:
            def walk(x):
                if isinstance(x,dict):
                    for value in x.values():walk(value)
                elif isinstance(x,(tuple,list)):
                    for value in x:walk(value)
                elif isinstance(x,str) and len(x)>=4:self.secrets.add(x)
            # Metadata such as IPs, installation IDs and usernames remains useful.
            if isinstance(data,dict) and 'secrets' in data:walk(data['secrets'])
            if isinstance(data,dict) and 'tls' in data:
                for key,value in data['tls'].items():
                    if key.endswith('.key'):walk(value)
            if isinstance(data,dict) and 'ssh' in data:walk(data['ssh'])
            if isinstance(data,dict) and data.get('app_tls'):walk(data['app_tls'].get('private_key',''))
            if isinstance(data,dict) and data.get('activation'):walk(data['activation'].get('token',''))
            if isinstance(data,dict) and data.get('directory'):walk(data['directory'].get('bind_password',''))
            if isinstance(data,dict) and data.get('directory_test_password'):walk(data['directory_test_password'])

    def redact(self,text):
        with self.lock:
            text=str(text)
            for secret in sorted(self.secrets,key=len,reverse=True):text=text.replace(secret,'[REDACTED]')
            text=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[PRIVATE KEY REDACTED]',text,flags=re.S)
            text=re.sub(r'(\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@',r'\1[REDACTED]@',text,flags=re.I)
            text=re.sub(r'(https?://[^\s?]+)\?[^\s]+',r'\1?[REDACTED]',text,flags=re.I)
            text=re.sub(r'(?i)([\w.-]*(?:password|token|secret|api_key|signing_key|media_key)[\w.-]*[\"\x27]?\s*[:=]\s*)([\"\x27])[^\r\n]*?\2',r'\1"[REDACTED]"',text)
            text=re.sub(r'(?i)([\w.-]*(?:password|token|secret|api_key|signing_key|media_key)[\w.-]*\s*=\s*)[^\s,;]+',r'\1[REDACTED]',text)
            text=re.sub(r'(?i)(Authorization\s*[:=]\s*(?:Bearer|Basic)\s+)[^\s\"\x27,]+',r'\1[REDACTED]',text)
            return text

    def append(self,message):
        with self.lock:
            clean=self.redact(message)
            with self.path.open('a',encoding='utf-8') as f:
                f.write(datetime.now().astimezone().isoformat(timespec='seconds')+' '+clean+'\n');f.flush()
            return clean

    def remote(self,role,action,text):
        if not re.fullmatch(r'[a-z0-9_-]+',role+'-'+action):raise ValueError('Invalid log name')
        with self.lock:
            path=self.directory/(self.stamp+'-'+role+'-'+action+'.log')
            clean=self.redact(text)
            with path.open('a',encoding='utf-8') as f:f.write(clean+'\n')
        if self.remote_listener:
            self.remote_listener(role,action,clean)
        return path
