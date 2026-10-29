import base64
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from prodcast_manager.ssh import Remote, parse_probe_output, PROBE_MARKER, ps, windows_probe_script, linux_probe_script

INFO={'hostname':'PC-WORKER-01','os':'Microsoft Windows Server 2025','free':40*1024**3}

def reply(info=INFO):return PROBE_MARKER+base64.b64encode(json.dumps(info).encode()).decode()

@pytest.mark.parametrize('noise',['','1 device has a firmware upgrade available.\r\n','Expanded Security Maintenance\n', '#< CLIXML\n<Objs><S>Preparing modules for first use</S></Objs>\n'])
def test_probe_ignores_nonprotocol_stdout(noise):
    assert parse_probe_output(noise+'\ufeff'+reply()+'\r\n'+noise,'worker1')==INFO

@pytest.mark.parametrize('data',['','\n','Welcome\n','#< CLIXML\n<Objs/>',PROBE_MARKER+'not-base64',reply()+ '\n'+reply(),PROBE_MARKER+base64.b64encode(b'not JSON').decode(),reply({'hostname':'worker1','os':'Windows','free':None}),reply({'hostname':'worker1','os':'Windows','free':True}),reply({'hostname':'worker1','os':'Windows','free':-1})])
def test_bad_probe_is_a_named_error_not_json_traceback(data):
    with pytest.raises(RuntimeError,match='worker1: OS and permissions check') as caught:parse_probe_output(data,'worker1')
    assert 'Expecting value' not in str(caught.value)

class Channel:
    def __init__(self,stdout,stderr,code=0):
        self.out=[stdout];self.err=[stderr] if stderr else [];self.code=code;self.closed=False
    def exec_command(self,cmd):pass
    def shutdown_write(self):pass
    # Simulate stderr arriving before stdout, as with PowerShell progress CLIXML.
    def recv_ready(self):return not self.err and bool(self.out)
    def recv_stderr_ready(self):return bool(self.err)
    def recv(self,size):return self.out.pop(0)
    def recv_stderr(self,size):return self.err.pop(0)
    def exit_status_ready(self):return not self.out and not self.err
    def recv_exit_status(self):return self.code
    def close(self):self.closed=True

def remote(channel):
    obj=object.__new__(Remote);obj.role='worker1';obj.windows=True;obj.log=lambda s:None
    obj.client=Mock();obj.client.get_transport.return_value.open_session.return_value=channel
    return obj

def test_stderr_clixml_never_pollutes_stdout_or_probe():
    channel=Channel(reply().encode(),b'#< CLIXML\r\n<Objs><S>Preparing modules</S></Objs>')
    assert remote(channel).probe()==INFO
    assert channel.closed

def test_shell_banner_is_tolerated_in_actual_probe_path():
    channel=Channel(('Expanded Security Maintenance\n'+reply()).encode(),b'fwupd notice')
    assert remote(channel).probe()==INFO


def test_progress_is_streamed_across_chunks_without_exposing_remote_text():
    channel=Channel(b'',b'MANAGER_PROGRESS:ai-images\nprivate stderr')
    channel.out=[b'private stdout\nMANAGER_PRO',b'GRESS:ai-images\r',b'\nMANAGER_PROGRESS:secret-token\nMANAGER_PROGRESS:ai-driver\nMANAGER_PROGRESS:ai-driver-ready\n',
                 b'x'*900+b'MANAGER_PROGRESS:ai-generation\n',b'MANAGER_PROGRESS:ai-generation\nMANAGER_RESULT:{}\n']
    obj=remote(channel);messages=[];obj.log=messages.append
    output=obj.command('test')
    assert len(messages)==2
    assert 'loading AI containers' in messages[0] and 'checking response generation' in messages[1]
    assert not any('driver' in message.lower() for message in messages)
    assert not any('private' in x or 'secret' in x for x in messages)
    assert 'MANAGER_RESULT:{}' in output

def test_unsuccessful_command_cannot_succeed_just_because_json_is_present():
    channel=Channel(reply().encode(),b'private password must not be printed',code=1)
    with pytest.raises(RuntimeError) as caught:remote(channel).probe()
    assert 'private password' not in str(caught.value)

@pytest.mark.parametrize('output',['', 'MANAGER_RESULT:', 'MANAGER_RESULT:[1,2]', 'MANAGER_RESULT:{bad}', 'MANAGER_RESULT:{}\nMANAGER_RESULT:{}'])
def test_action_reports_role_and_step_for_invalid_response(output):
    obj=object.__new__(Remote);obj.role='worker1';obj.windows=True;obj.stage='C:/stage'
    obj.put_bytes=lambda *a:None;obj.command=lambda *a:output
    with pytest.raises(RuntimeError,match='worker1: preflight'):obj.action({},'preflight',None)

@pytest.mark.skipif(sys.platform!='win32',reason='Requires Windows PowerShell')
def test_real_windows_powershell_text_protocol_and_probe():
    code="$probe=@{hostname='PC-WORKER-01';os='Microsoft Windows Server 2025';free=[long]42949672960}; $json=$probe | ConvertTo-Json -Compress; [Console]::WriteLine('MANAGER_PROBE_V1:'+([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))))"
    p=subprocess.run(ps(code),capture_output=True,timeout=30)
    assert p.returncode==0
    assert parse_probe_output(p.stdout.decode('utf-8-sig'),'worker1')==INFO
    # The actual probe is read-only. On a non-elevated test runner it must reject
    # the missing privilege, rather than return malformed JSON or a success.
    p=subprocess.run(ps(windows_probe_script()),capture_output=True,timeout=30)
    output=p.stdout.decode('utf-8-sig')
    if p.returncode==0:assert parse_probe_output(output,'worker1')['hostname']
    else:assert 'MANAGER_ERROR:SSH account must be administrator' in output

def test_linux_probe_source_compiles():compile(linux_probe_script(),'probe.py','exec')
