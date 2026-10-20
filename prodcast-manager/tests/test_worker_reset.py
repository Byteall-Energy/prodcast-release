"""Execute the reset stop helper with mocked SCM/process APIs; no real services."""
import json
import os
import subprocess
from pathlib import Path
import pytest


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell behavior')
@pytest.mark.parametrize('case',['graceful','owned-hung','foreign-pid','foreign-service','orphan-child','already-stopped','retry','restore-startup'])
def test_reset_stops_only_the_owned_worker_process(tmp_path,case):
    source=(Path(__file__).parents[1]/'prodcast_manager/resources/worker.ps1').read_text('utf-8')
    helper=source[source.index('function Get-WorkerProcesses('):source.index('function Dispatch {')]
    script=r'''
$ErrorActionPreference='Stop'
$case='CASE'
$service='ProdCastWorker01'
$p=@{operation='update'}
$script:state=[pscustomobject]@{install_root='C:\ProdCast\Managed\test';pending_root=''}
$script:events=New-Object Collections.Generic.List[string]
$script:stopped=$false
$script:startMode='Auto'
if($case -eq 'already-stopped'){$script:stopped=$true}
function Set-Field {param($Object,$Name,$Value) $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force}
function Save-State {}
function Get-ItemProperty {param($LiteralPath,$Name,$ErrorAction) return @{DelayedAutostart=1}}
function Set-Service {param($Name,$StartupType);$script:events.Add('disable:'+ $StartupType);$script:startMode=$StartupType}
function Stop-Service {param($Name,$ErrorAction);$script:events.Add('stop');if($case -in @('graceful','orphan-child','already-stopped','retry','restore-startup')){$script:stopped=$true}else{throw 'Simulated wrapper failure'}}
function Start-Sleep {param($Seconds)}
function Get-CimInstance {param($ClassName,$Filter)
 if($ClassName -eq 'Win32_Service'){return [pscustomobject]@{StartMode=$script:startMode;State=$(if($script:stopped){'Stopped'}else{'Running'});ProcessId=$(if($script:stopped){0}else{1234});PathName=$(if($case -eq 'foreign-service'){'"C:\unrelated\service.exe"'}else{'"C:\ProdCast\Managed\test\ProdCastWorker01.exe"'})}}
 if(!$script:stopped -and (!$Filter -or $Filter -eq 'ProcessId=1234')){[pscustomobject]@{ProcessId=1234;ParentProcessId=1;CreationDate='original';ExecutablePath=$(if($case -eq 'foreign-pid'){'C:\Windows\System32\unrelated.exe'}else{'C:\ProdCast\Managed\test\ProdCastWorker01.exe'})}}
 if($case -eq 'orphan-child' -and (!$Filter -or $Filter -eq 'ProcessId=2345')){[pscustomobject]@{ProcessId=2345;ParentProcessId=1234;CreationDate='child';ExecutablePath='C:\ProdCast\Managed\test\python.exe'}}
}
function taskkill.exe {param($PID,$T,$F);$script:events.Add('kill');$script:stopped=$true;$script:LASTEXITCODE=0}
function Check-Native {param($Name);if($LASTEXITCODE){throw 'Native failure'}}
function Get-Service {param($Name);$v=New-Object PSObject;Add-Member -InputObject $v -MemberType ScriptMethod -Name WaitForStatus -Value {param($status,$timeout)};return $v}
HELPER
$errorText='';try{Stop-OwnedWorker}catch{$errorText=$_.Exception.Message}
if($case -eq 'retry'){
 $script:stopped=$false
 Stop-OwnedWorker
 if($script:state.worker_stop.start_mode -ne 'Auto'){throw 'Retry lost original startup mode'}
}
if($case -eq 'restore-startup'){
 $script:state.worker_stop.delayed_auto=$false
 Restore-WorkerStartup
 if($script:startMode -ne 'Automatic'){throw 'Startup mode not restored after readiness'}
}
@{events=@($script:events);error=$errorText;stopped=$script:stopped;start_mode=$script:startMode}|ConvertTo-Json -Compress
'''.replace('CASE',case).replace('HELPER',helper)
    # Avoid PowerShell's built-in, read-only $PID in the mock parameter list.
    script=script.replace('param($PID,$T,$F)','param([Alias("PID")]$processId,$T,$F)')
    path=tmp_path/'reset-stop-test.ps1';path.write_text(script,encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path)],capture_output=True,text=True,check=True)
    value=json.loads(result.stdout.strip().splitlines()[-1])
    if case=='foreign-service':
        assert not value['events'] and 'unowned' in value['error']
        return
    assert value['events'][:2]==['disable:Disabled','stop']
    assert value['start_mode']==('Automatic' if case=='restore-startup' else 'Disabled')
    if case=='foreign-pid':
        assert 'unowned' in value['error'] and 'kill' not in value['events'] and not value['stopped']
    elif case=='orphan-child':
        assert 'process tree has not fully exited' in value['error'] and 'kill' not in value['events']
    else:
        assert not value['error'] and value['stopped']
        assert ('kill' in value['events'])==(case=='owned-hung')


def test_cached_stop_actions_are_never_reused():
    source=(Path(__file__).parents[1]/'prodcast_manager/resources/worker.ps1').read_text('utf-8')
    cache=next(line for line in source.splitlines() if 'elseif($done -and $p.action -notin' in line)
    assert "'stop'" in cache and "'maintenance-stop'" in cache
    stop=source.split("        'stop' {",1)[1].split("        'backup'",1)[0]
    assert 'Stop-OwnedWorker' in stop and 'StartupType Manual' not in stop
    assert 'Assert-WorkerStopped' in source.split('if($svc -and !$svc.PathName.Contains($installDir))',1)[1].split('sc.exe delete',1)[0]
