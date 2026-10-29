"""Run the shipped PowerShell validation/readiness code without service changes."""
import itertools
import json
import shutil
import subprocess

import pytest
from prodcast_manager.engine import RESOURCES

POWERSHELL=shutil.which('powershell.exe') or shutil.which('pwsh')
pytestmark=pytest.mark.skipif(not POWERSHELL,reason='PowerShell runtime is required')
QUEUES=['workflows','workflows_data','scenarios']
COMBINATIONS=[list(q) for n in range(1,4) for q in itertools.combinations(QUEUES,n)]


def run_profile(tmp_path,host,checks):
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'role':'worker1','site':{'hosts':{'worker1':host}}}))
    source=(RESOURCES/'worker.ps1').read_text()
    path=tmp_path/'queues.ps1'
    # Execute only configuration parsing and queue validation; no service/FS writes.
    preamble=source.split('function Save-State',1)[0]
    # Unix PowerShell needs a temporary in-process C: mapping for Windows paths.
    first,body=preamble.split('\n',1)
    mount="if(!(Get-PSDrive C -ErrorAction SilentlyContinue)){New-PSDrive -Name C -PSProvider FileSystem -Root (Split-Path $Request) | Out-Null}\n"
    path.write_text(first+'\n'+mount+body+checks)
    return subprocess.run([POWERSHELL,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Request',str(request)],capture_output=True,text=True,timeout=30)


@pytest.mark.parametrize('queues',COMBINATIONS)
def test_readiness_checks_exact_profile_and_preserves_json_array(tmp_path,queues):
    result=run_profile(tmp_path,{'execution_queues':queues},r'''
$site=[pscustomobject]@{execution_queues=@($executionQueues)}
$marker=[pscustomobject]@{queues=@($executionQueues)}
if(!(Test-WorkerQueues $site $marker)){throw 'Matching queues must pass'}
$stored=$site | ConvertTo-Json | ConvertFrom-Json
if($stored.execution_queues -isnot [Array]){throw 'Single queue must remain an array'}
$marker.queues=@($executionQueues)+@('unselected')
if(Test-WorkerQueues $site $marker){throw 'Extra subscription must fail'}
$marker.queues=@()
if(Test-WorkerQueues $site $marker){throw 'Missing subscription must fail'}
'OK'
''')
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout


@pytest.mark.parametrize('value',[[],None,'workflows',['default'],['workflows','workflows'],['WORKFLOWS']])
def test_invalid_profile_fails_before_any_service_operation(tmp_path,value):
    result=run_profile(tmp_path,{'execution_queues':value},"throw 'Invalid profile was accepted'")
    assert result.returncode!=0
    assert 'Invalid Worker execution_queues' in result.stdout+result.stderr


def test_old_profile_and_readiness_use_all_queues(tmp_path):
    result=run_profile(tmp_path,{},r'''
$site=[pscustomobject]@{}
$marker=[pscustomobject]@{queues=@('scenarios','workflows_data','workflows')}
if(!(Test-WorkerQueues $site $marker)){throw 'Old profiles must keep all queues'}
'OK'
''')
    assert result.returncode==0,result.stdout+result.stderr
