from .i18n import log_tr as tr
import base64
import hashlib
import json
import shlex
import socket
import time
import threading
import ipaddress
import uuid
import re
from pathlib import Path
import paramiko

def fingerprint(key): return 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')

class ScanError(RuntimeError):
    def __init__(self,stage,message):
        self.stage=stage
        super().__init__(message)

def scan(host,progress=None,cancel=None,connect_timeout=15,banner_timeout=30,handshake_timeout=30):
    """Get a host key without authenticating; report which network phase failed."""
    progress=progress or (lambda message:None)
    cancel=cancel or threading.Event()
    address=host['address'];port=int(host['port']);endpoint=f'{address}:{port}'
    ip=ipaddress.ip_address(address)
    if any(ip in ipaddress.ip_network(n) for n in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')):
        raise ScanError('configuration',tr('{v0} — демонстрационный адрес из шаблона. На вкладке «Серверы» замените его реальным IP виртуальной машины, затем повторите получение отпечатка.').format(v0=address))
    def cancelled():
        if cancel.is_set(): raise ScanError('cancelled',tr('Получение отпечатка отменено.'))
    cancelled();progress(tr('Подключение к {v0}…').format(v0=endpoint))
    try: sock=socket.create_connection((address,port),timeout=connect_timeout)
    except TimeoutError as e:
        raise ScanError('tcp',tr('{v0}: TCP-подключение не установлено за {v1:g} с. Проверьте IP, SSH-порт, VPN/маршрут и правила firewall. Пароль и SSH-ключ ещё не использовались.').format(v0=endpoint,v1=connect_timeout)) from e
    except ConnectionRefusedError as e:
        raise ScanError('tcp',tr('{v0}: соединение отклонено. Проверьте, запущена ли служба SSH и слушает ли она этот порт.').format(v0=endpoint)) from e
    except OSError as e:
        raise ScanError('tcp',tr('{v0}: ошибка сети ({v1}). Проверьте адрес, VPN/маршрут и доступность VM.').format(v0=endpoint,v1=e)) from e
    with sock:
        cancelled();progress(tr('{v0}: TCP доступен. Ожидание ответа SSH…').format(v0=endpoint))
        t=paramiko.Transport(sock);t.banner_timeout=banner_timeout;t.handshake_timeout=handshake_timeout
        done=threading.Event();banner_seen=False;started=time.monotonic()
        try:
            t.start_client(event=done)
            while not done.wait(.1):
                cancelled()
                if t.remote_version and not banner_seen:
                    banner_seen=True;progress(tr('{v0}: сервер ответил по SSH. Обмен ключами…').format(v0=endpoint))
                if time.monotonic()-started>banner_timeout+handshake_timeout:
                    raise TimeoutError('SSH negotiation deadline exceeded')
            cancelled()
            error=t.get_exception()
            if error: raise error
            if not t.is_active(): raise EOFError('Connection closed during SSH negotiation')
            result=fingerprint(t.get_remote_server_key())
            progress(tr('{v0}: отпечаток получен; требуется сверка с консолью VM.').format(v0=endpoint))
            return result
        except ScanError: raise
        except (paramiko.SSHException,EOFError,OSError) as e:
            if t.remote_version:
                raise ScanError('handshake',tr('{v0}: SSH ответил, но обмен ключами не завершился. Проверьте журнал sshd, ограничения соединений и совместимость алгоритмов. Причина: {v1}').format(v0=endpoint,v1=e)) from e
            raise ScanError('banner',tr('{v0}: TCP-порт доступен, но корректное SSH-приветствие не получено. Возможно, указан порт другой службы, SSH не отвечает или соединение закрывает firewall/прокси. Причина: {v1}').format(v0=endpoint,v1=e)) from e
        finally: t.close()

class PinnedPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self,expected): self.expected=expected
    def missing_host_key(self,client,hostname,key):
        if not self.expected or fingerprint(key)!=self.expected:
            raise paramiko.SSHException('SSH host fingerprint differs; credentials were not sent')

def ps(code):
    setup="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); $OutputEncoding=[Console]::OutputEncoding; "
    return 'powershell.exe -NoLogo -NoProfile -NonInteractive -OutputFormat Text -ExecutionPolicy Bypass -EncodedCommand '+base64.b64encode((setup+code).encode('utf-16-le')).decode()

PROBE_MARKER='MANAGER_PROBE_V1:'

PROGRESS_MESSAGES={
    'ai-driver':tr('установка NVIDIA-драйвера и сборка модулей ядра'),
    'ai-driver-ready':tr('драйвер установлен; для GPU могут потребоваться перезагрузка и регистрация ключа Secure Boot; продолжаем с доступными ресурсами'),
    'ai-images':tr('загрузка контейнеров AI из релиза'),
    'ai-runtime-download':tr('скачивание Ollama; длительность зависит от скорости сети'),
    'ai-model-download':tr('скачивание модели Ollama'),
    'ai-generation':tr('проверка генерации ответа; на CPU может занять несколько минут'),
}

class ProgressLines:
    """Only known status codes reach the GUI; arbitrary remote output stays private."""
    def __init__(self,emit):self.emit=emit;self.pending=b'';self.discard=False
    def feed(self,data):
        for part in data.splitlines(keepends=True):
            done=part.endswith(b'\n')
            if not self.discard:
                self.pending+=part
                if len(self.pending)>512:self.pending=b'';self.discard=True
            if done:
                if not self.discard:
                    line=self.pending.rstrip(b'\r\n')
                    if line.startswith(b'MANAGER_PROGRESS:'):
                        message=PROGRESS_MESSAGES.get(line[17:].decode('ascii','replace'))
                        if message:self.emit(tr(message))
                self.pending=b'';self.discard=False

def windows_probe_script():
    return """$ErrorActionPreference='Stop'; try {
if(!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
    [Console]::WriteLine('MANAGER_ERROR:SSH account must be administrator'); exit 1
}
if(![Environment]::Is64BitProcess){[Console]::WriteLine('MANAGER_ERROR:64-bit PowerShell required'); exit 1}
$probe=@{hostname=$env:COMPUTERNAME;os=(Get-CimInstance Win32_OperatingSystem).Caption;free=[long]((Get-PSDrive -Name C).Free)}
$json=$probe | ConvertTo-Json -Compress
[Console]::WriteLine('MANAGER_PROBE_V1:'+([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))))
} catch {[Console]::WriteLine('MANAGER_ERROR:Windows probe failed: verify access to Get-CimInstance and drive C'); exit 1}
"""

def linux_probe_script():
    return "import base64,json,os,platform,shutil,shlex; assert os.geteuid()==0; assert platform.machine()=='x86_64'; info=platform.freedesktop_os_release() if hasattr(platform,'freedesktop_os_release') else {k:(shlex.split(v) or [''])[0] for k,v in (line.strip().split('=',1) for line in open('/etc/os-release') if '=' in line and not line.lstrip().startswith('#'))}; data=dict(hostname=platform.node(),os=info,free=shutil.disk_usage('/').free); print('MANAGER_PROBE_V1:'+base64.b64encode(json.dumps(data).encode()).decode())"

def parse_probe_output(output,role):
    lines=[line.strip().lstrip('\ufeff') for line in output.splitlines()]
    replies=[line[len(PROBE_MARKER):] for line in lines if line.startswith(PROBE_MARKER)]
    if len(replies)!=1:
        detail=tr('пустой ответ') if not output.strip() else tr('ответ не содержит единственного служебного блока')
        if '#< CLIXML' in output: detail=tr('PowerShell вернул CLIXML вместо текстового ответа')
        raise RuntimeError(tr('{v0}: проверка ОС и прав — {v1}. Проверьте запуск PowerShell/Python через SSH и используйте актуальную версию Manager.').format(v0=role,v1=detail))
    try:
        obj=json.loads(base64.b64decode(replies[0],validate=True).decode('utf-8'))
        if not isinstance(obj,dict) or not isinstance(obj.get('hostname'),str) or not obj['hostname']: raise ValueError()
        if not isinstance(obj.get('os'),(str,dict)) or not obj['os']: raise ValueError()
        if type(obj.get('free')) is not int or obj['free']<0: raise ValueError()
        return obj
    except (ValueError,TypeError,UnicodeError) as e:
        raise RuntimeError(tr('{v0}: проверка ОС и прав — повреждён или неполон служебный ответ SSH. Установка не продолжена.').format(v0=role)) from e

class Remote:
    def __init__(self,role,host,auth,log):
        self.role=role; self.host=host; self.auth=auth; self.log=log
        self.windows=role.startswith('worker'); self.stage=None
        self.client=paramiko.SSHClient(); self.client.set_missing_host_key_policy(PinnedPolicy(host['fingerprint']))
        self.client.connect(host['address'],port=host['port'],username=host['username'],
                            password=auth.get('password') if host['auth']=='password' else None,
                            key_filename=host['key_path'] if host['auth']=='key' else None,
                            passphrase=auth.get('passphrase') or None,allow_agent=False,look_for_keys=False,
                            timeout=20,auth_timeout=30,banner_timeout=30)
        self.client.get_transport().set_keepalive(20)

    def close(self): self.client.close()

    def command(self,cmd,stdin='',timeout=7200):
        ch=self.client.get_transport().open_session(timeout=20)
        ch.exec_command(cmd)
        if stdin: ch.sendall(stdin.encode())
        ch.shutdown_write(); buffers={'stdout':bytearray(),'stderr':bytearray()}; started=time.monotonic(); deadline=started+timeout; last_notice=started
        progress=ProgressLines(lambda message:self.log(f'{self.role}: {message}'))
        try:
            while True:
                for stream,ready,recv in (('stdout',ch.recv_ready,ch.recv),('stderr',ch.recv_stderr_ready,ch.recv_stderr)):
                    while ready():
                        b=recv(65536)
                        if not b:break
                        if stream=='stdout':progress.feed(b)
                        buffers[stream].extend(b)
                        if len(buffers[stream])>2*1024*1024: del buffers[stream][:-1024*1024]
                if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready(): break
                if time.monotonic()>deadline: raise TimeoutError('Remote action timed out; inspect remote state before retry')
                if time.monotonic()-last_notice>20:
                    self.log(tr('{v0}: шаг выполняется, прошло {v1} с').format(v0=self.role,v1=int(time.monotonic() - started)))
                    last_notice=time.monotonic()
                time.sleep(.05)
            out=buffers['stdout'].decode('utf-8-sig','replace')
            err=buffers['stderr'].decode('utf-8-sig','replace')
            if ch.recv_exit_status()!=0:
                # Arbitrary child process output can contain secrets. Never return it to UI/log.
                marker=next((l for l in (out+'\n'+err).splitlines() if l.startswith('MANAGER_ERROR:')),None)
                if marker:marker=marker.replace('inspect /var/lib/prodcast-manager/operation.log',tr('журнал на Linux VM: /var/lib/prodcast-manager/operation.log'))
                raise RuntimeError(f'{self.role}: '+(marker or 'remote action failed; inspect protected server log'))
            return out
        finally: ch.close()

    def root(self,cmd,**kwargs):
        if self.host['username']=='root': return self.command(cmd,**kwargs)
        password=self.auth.get('sudo_password','')
        if '\n' in password or '\r' in password: raise ValueError('Newlines in sudo password are unsupported')
        return self.command('sudo -S -p '+shlex.quote('')+' -- '+cmd,stdin=password+'\n',**kwargs)

    def probe(self):
        if self.windows:
            out=self.command(ps(windows_probe_script()),timeout=60)
        else:out=self.root('python3 -c '+shlex.quote(linux_probe_script()),timeout=40)
        return parse_probe_output(out,self.role)

    def prepare_stage(self):
        suffix=uuid.uuid4().hex
        if self.windows:
            self.stage='C:/ProgramData/ProdCastManager/inbox/'+suffix
            code=f"$ErrorActionPreference='Stop'; New-Item -ItemType Directory -Force '{self.stage}' | Out-Null; & icacls.exe '{self.stage}' /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null; if($LASTEXITCODE){{throw 'ACL failure'}}"
            self.command(ps(code))
        else:
            # /tmp is a RAM-backed tmpfs on current Ubuntu. Offline model bundles
            # belong on persistent temporary storage, not in the VM's RAM.
            self.stage='/var/tmp/prodcast-manager-'+suffix
            self.command('umask 077; mkdir '+shlex.quote(self.stage))

    def put_bytes(self,name,data):
        with self.client.open_sftp() as s:
            path=self.stage+'/'+name
            with s.file(path,'wb') as f: f.write(data)
            if not self.windows: s.chmod(path,0o600)

    def put(self,path):
        if not self.windows:
            required=Path(path).stat().st_size+256*1024**2
            output=self.command('python3 -c '+shlex.quote('import shutil; print("MANAGER_SPACE:"+str(shutil.disk_usage('+repr(self.stage)+').free))'))
            replies=[line.removeprefix('MANAGER_SPACE:') for line in output.splitlines() if line.startswith('MANAGER_SPACE:')]
            if len(replies)!=1 or not replies[0].isdigit():raise RuntimeError(f'{self.role}: could not verify staging disk space')
            available=int(replies[0])
            if available<required:
                raise RuntimeError(f'{self.role}: insufficient staging disk space in /var/tmp; need {required//1048576} MiB, available {available//1048576} MiB')
        with self.client.open_sftp() as s:
            self.log(tr('{v0}: передача {v1}').format(v0=self.role,v1=Path(path).name))
            notice=[time.monotonic()]
            def progress(sent,total):
                if time.monotonic()-notice[0]>20:
                    self.log(tr('{v0}: передано {v1} / {v2} МБ').format(v0=self.role,v1=sent // 1048576,v2=total // 1048576));notice[0]=time.monotonic()
            s.put(str(path),self.stage+'/'+Path(path).name,callback=progress,confirm=True)
            if not self.windows: s.chmod(self.stage+'/'+Path(path).name,0o600)

    def action(self,payload,action,resources):
        diagnostics=getattr(self,'diagnostics',None)
        if diagnostics:diagnostics.protect(payload)
        try:return self._action(payload,action,resources)
        except Exception as original:
            if diagnostics:
                try:
                    path=diagnostics.remote(self.role,action,str(original)+'\n'+self.collect_diagnostics())
                    self.log(tr('{v0}: диагностика сервера сохранена на этом ПК: {v1}').format(v0=self.role,v1=path))
                except Exception:
                    self.log(tr('{v0}: не удалось сохранить диагностику сервера; основной журнал запуска находится в папке prodcast-data/logs рядом с приложением').format(v0=self.role))
            raise

    def collect_diagnostics(self):
        if self.windows:
            return self.command(ps("$ErrorActionPreference='Stop'; $p='C:/ProgramData/ProdCastManager/operation.log'; if(Test-Path -LiteralPath $p){ Get-Content -LiteralPath $p -Tail 200 -Encoding UTF8 } else { 'Server operation log does not exist yet' }"),timeout=30)
        code="from pathlib import Path; p=Path('/var/lib/prodcast-manager/operation.log'); f=p.open('rb') if p.exists() else None; f and f.seek(max(0,p.stat().st_size-65536)); print(f.read().decode('utf-8','replace') if f else 'Server operation log does not exist yet'); f and f.close()"
        out='Linux VM: /var/lib/prodcast-manager/operation.log\n'+self.root('python3 -c '+shlex.quote(code),timeout=30)
        if self.role=='ai':
            try:out+='\nOllama systemd journal:\n'+self.root('journalctl -u prodcast-managed-ollama -n 60 --no-pager -o cat',timeout=30)
            except Exception:out+='\nOllama systemd journal unavailable'
        return out

    def _action(self,payload,action,resources):
        payload=dict(payload,action=action,stage=self.stage)
        if action=='ai-documents':
            self.put_bytes('ai-documents.sql',(Path(resources)/'ai-documents.sql').read_bytes())
        if action=='bootstrap' and not self.windows and payload.get('offline'):
            # Small, signed compatibility RPMs are shipped with the portable
            # Manager so existing release archives and resume hashes stay valid.
            for package in sorted((Path(resources)/'rhel9').glob('*.rpm')):
                self.put(package)
        self.put_bytes('request.json',json.dumps(payload).encode())
        if self.windows:
            cmd=ps(f"& '{self.stage}/worker.ps1' -Request '{self.stage}/request.json'; if(!$?){{exit 1}}")
            out=self.command(cmd)
        else:
            # Imported stage modules otherwise leave root-owned __pycache__.
            out=self.root('python3 -B '+shlex.quote(self.stage+'/linux.py')+' '+shlex.quote(self.stage+'/request.json'))
        lines=[l[len('MANAGER_RESULT:'):] for l in out.splitlines() if l.startswith('MANAGER_RESULT:')]
        if len(lines)!=1: raise RuntimeError(tr('{v0}: {v1} — отсутствует однозначный служебный ответ сервера; проверьте защищённый журнал на VM').format(v0=self.role,v1=action))
        try:
            result=json.loads(lines[0])
            if not isinstance(result,dict):raise ValueError('Expected object')
            return result
        except (ValueError,TypeError) as e:
            raise RuntimeError(tr('{v0}: {v1} — повреждён служебный ответ сервера; проверьте защищённый журнал на VM').format(v0=self.role,v1=action)) from e

    def cleanup(self):
        if not self.stage: return
        # Remove only our generated staging directory; never installation/data directories.
        if self.windows:
            if not re.fullmatch(r'C:/ProgramData/ProdCastManager/inbox/[0-9a-f]{32}',self.stage):raise ValueError('Unsafe staging path')
            code=rf"$ErrorActionPreference='Stop'; $p=[IO.Path]::GetFullPath('{self.stage}'); if(!$p.StartsWith('C:\ProgramData\ProdCastManager\inbox\',[StringComparison]::OrdinalIgnoreCase)){{throw 'Unsafe path'}}; if(Test-Path -LiteralPath $p){{if((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint){{throw 'Unsafe staging link'}}; Remove-Item -LiteralPath $p -Recurse -Force}}"
            self.command(ps(code))
        else:
            if not re.fullmatch(r'/var/tmp/prodcast-manager-[0-9a-f]{32}',self.stage):raise ValueError('Unsafe staging path')
            code=('from pathlib import Path; import shutil; p=Path('+repr(self.stage)+'); '
                  'assert not p.is_symlink() and p.resolve()==p, "Unsafe staging link"; '
                  'shutil.rmtree(p) if p.exists() else None')
            # Agents run under sudo and can create root-owned temporary files.
            self.root('python3 -B -c '+shlex.quote(code),timeout=60)
        self.stage=None

    def cleanup_warning(self,error):
        """Give failures a role/path and redact protected diagnostics locally."""
        diagnostics=getattr(self,'diagnostics',None)
        saved=None
        if diagnostics:
            try:saved=diagnostics.remote(self.role,'cleanup',str(error)+'\n'+self.collect_diagnostics())
            except Exception:pass
        self.log(tr('{v0}: не удалось удалить временный каталог {v1}.').format(v0=self.role,v1=self.stage))
        if saved:self.log(tr('{v0}: диагностика очистки сохранена: {v1}').format(v0=self.role,v1=saved))
