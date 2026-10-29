"""Real PowerShell identity/health functions, mocked service/account boundaries."""
import subprocess
import pytest
from prodcast_manager.engine import RESOURCES


@pytest.mark.parametrize('case',['immediate','delayed','never-ready','foreign-path'])
def test_maintenance_waits_for_worker_readiness_without_restarting_it(tmp_path,case):
    script=r'''
param($Source,$Case)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Test-WorkerRuntime','Wait-WorkerMaintenanceReady')){
 $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($fn.Extent.Text))
}
$service='ProdCastWorker01';$script:state=@{install_root='C:\ProdCast\Managed\current'}
$script:sleeps=0;$script:diagnostics=0
function Get-CimInstance {
 $path=if($Case -eq 'foreign-path'){'C:\foreign\worker.exe'}else{'C:\ProdCast\Managed\current\ProdCastWorker01.exe'}
 return [pscustomobject]@{State='Running';PathName=$path}
}
function Test-Path {param($LiteralPath,$PathType)
 if($LiteralPath.EndsWith('scheduler-service-ready.json')){
  return $Case -eq 'immediate' -or ($Case -eq 'delayed' -and $script:sleeps -ge 2)
 }
 return $true
}
function Start-Sleep {param($Seconds) $script:sleeps++}
function Write-WorkerServiceDiagnostic {$script:diagnostics++}
function Start-Service {throw 'Health check must not restart service'}
function Set-Service {throw 'Health check must not configure service'}
$failure=''
try{Wait-WorkerMaintenanceReady 4}catch{$failure=$_.Exception.Message}
if($Case -in @('immediate','delayed')){
 if($failure){throw $failure}
 if($script:diagnostics){throw 'Unexpected failure diagnostics'}
 if($Case -eq 'immediate' -and $script:sleeps){throw 'Ready Worker should not wait'}
 if($Case -eq 'delayed' -and $script:sleeps -ne 2){throw 'Must wait for readiness marker'}
}else{
 if($failure -notmatch 'within 4 seconds' -or $script:diagnostics -ne 1 -or $script:sleeps -ne 2){throw 'Timeout must be bounded and diagnosed'}
}
'OK'
'''
    path=tmp_path/'maintenance-ready.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1'),'-Case',case],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout


def test_existing_identity_is_preserved_and_credentials_checked_before_mutations(tmp_path):
    script=r'''
param([string]$Source)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Resolve-ServiceIdentity','Test-ExistingWorker')){
 $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($fn.Extent.Text))
}
$service='ProdCastWorker01';$number='01';$executionMode='windows-appcontainer'
$p=[pscustomobject]@{operation='original';version='v0.6.6';manifest='expected';secrets=@{};site=@{hosts=@{db=@{address='192.0.2.1'}}}}
$script:state=[pscustomobject]@{operation='original';version='';manifest='';install_root='';pending_root='C:\ProdCast\Managed\v0.6.6-owned';steps=[pscustomobject]@{}}
$script:svc=[pscustomobject]@{State='Running';StartName='EXAMPLE\approved';PathName='C:\ProdCast\Managed\v0.6.6-owned\ProdCastWorker01.exe'}
function Get-CimInstance {param($ClassName,$Filter) return $script:svc}
function New-LocalUser {throw 'Must not create an account'}
function Add-ServiceRights {throw 'Must not edit service logon rights'}
function Test-ServiceLogon {throw 'Separate logon probe must not block SCM startup'}
function ConvertTo-SecureString {param($String,[switch]$AsPlainText,[switch]$Force) return (New-Object Security.SecureString)}
function Test-Path {param($LiteralPath,$PathType) return $true}
function Get-Content {param([switch]$Raw,$LiteralPath)
 if($LiteralPath.EndsWith('site.json')){return '{"worker_number":"01","postgres_host":"192.0.2.1"}'}
 return '{"mode":"windows-appcontainer","postgres_tls":true}'
}
$identity=Resolve-ServiceIdentity $true
if($identity.account -ne 'EXAMPLE\approved' -or $identity.credential){throw 'Existing account was not preserved'}
$healthy=Test-ExistingWorker
if(!$healthy.healthy -or $healthy.services_restarted){throw 'Read-only healthcheck failed'}
$script:state.pending_root=''
try{Resolve-ServiceIdentity $false;throw 'Expected credential refusal'}catch{if($_.Exception.Message -notmatch 'requires its credentials'){throw}}
$p | Add-Member worker_service @{mode='custom';account='EXAMPLE\approved';password='synthetic-only'}
$identity=Resolve-ServiceIdentity $true
if($identity.credential.UserName -ne 'EXAMPLE\approved'){throw 'Service credentials were not preserved for SCM'}
$script:svc.State='Stopped'
try{Test-ExistingWorker;throw 'Expected health refusal'}catch{if($_.Exception.Message -notmatch 'not running'){throw}}
'OK'
'''
    path=tmp_path/'service-account.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout


@pytest.mark.parametrize('case', ['prepared', 'incomplete', 'foreign-service', 'old-operation', 'stale-target'])
def test_failed_start_recovery_and_next_attempt_need_no_manual_enable(tmp_path, case):
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'Start-Worker.ps1').write_text(r'''
param($InstallDir,$WorkerNumber)
if($global:startMode -eq 'Disabled'){throw 'Service disabled: 1058'}
if($global:firstAttempt){throw 'Initial service startup failure'}
$global:running=$true
''','utf-8')
    script=r'''
param($Source,$Package,$Case)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
function Write-WorkerServiceDiagnostic {}
foreach($name in @('Start-InstalledWorker','Recover-WorkerStartupFailure','Restore-WorkerStartup')){
 $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($fn.Extent.Text))
}
$service='ProdCastWorker01';$number='01';$executionMode='windows-account';$p=@{operation='update'}
$target='C:\ProdCast\Managed\target'
$script:state=[pscustomobject]@{pending_root=$target;worker_start=@{operation='update';path=$target};worker_stop=@{operation='update';start_mode='Auto';delayed_auto=$false}}
$global:startMode='Disabled';$global:running=$false;$global:firstAttempt=$true
$global:servicePath=Join-Path $target ($service+'.exe')
function Set-Service {param($Name,$StartupType) $global:startMode=$StartupType}
function Stop-OwnedWorker {$global:running=$false;$global:startMode='Disabled'}
function Get-CimInstance {param($ClassName,$Filter,$ErrorAction)
 return [pscustomobject]@{StartName='NCOC\approved';PathName=$global:servicePath;State=$(if($global:running){'Running'}else{'Stopped'});ExitCode=0}
}
function Get-Content {param([switch]$Raw,$LiteralPath) return '{"mode":"windows-account","postgres_tls":true}'}
function Test-Path {param($LiteralPath,$PathType) return ($Case -ne 'incomplete')}
try{Start-InstalledWorker $Package $target;throw 'Expected initial failure'}catch{if($_.Exception.Message -notmatch 'Initial service startup failure'){throw}}
if($Case -eq 'foreign-service'){$global:servicePath='C:\other\ProdCastWorker01.exe'}
if($Case -eq 'old-operation'){$script:state.worker_start.operation='old'}
if($Case -eq 'stale-target'){$script:state.worker_start.path='C:\old'}
Recover-WorkerStartupFailure
if($Case -eq 'prepared'){
 if($global:startMode -ne 'Manual' -or $global:running){throw 'Prepared target must remain stopped and manually startable'}
 $global:startMode='Disabled' # Also support services disabled by older Manager builds.
 $global:firstAttempt=$false
 $ready=Start-InstalledWorker $Package $target
 if(!$global:running -or !$ready.postgres_tls){throw 'Retry must enable and start target automatically'}
 Restore-WorkerStartup
 if($global:startMode -ne 'Automatic'){throw 'Original startup mode must survive failure/retry'}
 if((Get-CimInstance Win32_Service).StartName -ne 'NCOC\approved'){throw 'Account changed'}
}elseif($global:startMode -ne 'Disabled'){throw 'Unprepared or unowned target must stay disabled'}
'OK'
'''
    path=tmp_path/'retry-test.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1'),'-Package',str(tmp_path),'-Case',case],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout


def test_custom_administrator_credentials_are_accepted_without_warning(tmp_path):
    script=r'''
param([string]$Source,[string]$Stage)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Invoke-WorkerInstaller')){
 $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($fn.Extent.Text))
}
# No real Windows accounts or services are modified.
$number='01';$secure=New-Object Security.SecureString
$runtime=Join-Path $Stage 'runtime';New-Item -ItemType Directory $runtime | Out-Null
$installer=@'
param($InstallDir,$PythonPath,$WorkerNumber,$ServiceCredential)
$account=$ServiceCredential.UserName
# Initial profile deliberately supports provisioned non-admin local accounts only.
$prefix=$env:COMPUTERNAME+'\'
if (!$account.StartsWith($prefix)) { throw 'Supply a provisioned local service account as COMPUTERNAME\user.' }
throw 'Administrator service account refused.'
$name='ProdCastWorker'+$WorkerNumber
Write-Host ('installed='+$account)
'@
[IO.File]::WriteAllText((Join-Path $runtime 'Install-Worker.ps1'),$installer)
foreach($account in @('DOMAIN\AdminUser',($env:COMPUTERNAME+'\AdminUser'))){
 $credential=New-Object Management.Automation.PSCredential($account,$secure)
 Invoke-WorkerInstaller $Stage 'unused' 'unused' $credential
}
'OK'
'''
    path=tmp_path/'admin-account.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1'),'-Stage',str(tmp_path)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'installed=DOMAIN\\AdminUser' in result.stdout and 'OK' in result.stdout
    assert 'warning' not in result.stdout.lower()


@pytest.mark.parametrize('case', ['success', 'scm-failure', 'not-running', 'not-ready'])
def test_worker_startup_uses_service_result_and_requires_readiness(tmp_path, case):
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'Start-Worker.ps1').write_text(r'''
param($InstallDir,$WorkerNumber)
$global:startupAttempted=$true
if($global:case -eq 'scm-failure'){throw 'SCM logon failure 1069'}
$global:serviceRunning=($global:case -ne 'not-running')
''','utf-8')
    script=r'''
param($Source,$Package,$Case)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-InstalledWorker'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
$service='ProdCastWorker01';$number='01';$executionMode='windows-account'
$global:case=$Case;$global:startupAttempted=$false;$global:serviceRunning=$false
function Write-WorkerServiceDiagnostic {param($phase,$since,$failure)
 if($phase -eq 'startup-failed'){throw 'Diagnostic read denied; must not replace startup failure'}
}
function Set-Service {param($Name,$StartupType)}
function Get-CimInstance {param($ClassName,$Filter,$ErrorAction)
 return [pscustomobject]@{StartName='DOMAIN\approved';State=$(if($global:serviceRunning){'Running'}else{'Stopped'});ExitCode=1069}
}
function Get-Content {param([switch]$Raw,$LiteralPath)
 if($global:case -eq 'not-ready'){return '{"mode":"wrong","postgres_tls":false}'}
 return '{"mode":"windows-account","postgres_tls":true}'
}
$failure=''
try{$marker=Start-InstalledWorker $Package 'unused'}catch{$failure=$_.Exception.Message}
if(!$global:startupAttempted){throw 'Must attempt actual service startup'}
if($Case -eq 'success'){
 if($failure -or !$marker.postgres_tls){throw 'Healthy startup must succeed'}
}else{
 if(!$failure){throw 'Unhealthy startup must fail'}
 if($Case -eq 'scm-failure' -and $failure -notmatch '1069'){throw 'Must preserve actual SCM failure'}
}
'OK'
'''
    path=tmp_path/'start-test.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1'),'-Package',str(tmp_path),'-Case',case],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout


@pytest.mark.parametrize('case', ['events', 'unavailable', 'none'])
def test_service_diagnostics_scope_events_redact_password_and_never_mutate(tmp_path, case):
    script=r'''
param($Source,$Case)
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Write-WorkerServiceDiagnostic'},$true)
. ([scriptblock]::Create($fn.Extent.Text))
$service='ProdCastWorker01';$p=@{operation='test-operation';worker_service=@{password='synthetic-secret'}}
$since=Get-Date
function Set-Service {throw 'Diagnostics must not mutate service'}
function Invoke-CimMethod {throw 'Diagnostics must not mutate credentials'}
function Add-ServiceRights {throw 'Diagnostics must not mutate rights'}
function Get-CimInstance {
 if($Case -eq 'unavailable'){throw 'Read denied'}
 return [pscustomobject]@{StartName='NT AUTHORITY\SYSTEM';DisplayName='ProdCast Worker 01';State='Stopped';StartMode='Manual';ProcessId=0;ExitCode=0;ServiceSpecificExitCode=0}
}
function Get-WinEvent {param($FilterHashtable,$MaxEvents,$ErrorAction)
 if($FilterHashtable.StartTime -ne $since -or $FilterHashtable.ProviderName -ne 'Service Control Manager' -or $MaxEvents -ne 256){throw 'Incorrect diagnostic bounds'}
 if($Case -eq 'unavailable'){throw 'Event read denied'}
 if($Case -eq 'none'){return @()}
 foreach($name in @('UnrelatedService','ProdCastWorker01','ProdCast Worker 01')){
  $event=[pscustomobject]@{TimeCreated=$since;Id=7041;RecordId=42;Message=($name+' logon right missing synthetic-secret');Service=$name}
  $event | Add-Member ScriptMethod ToXml {return ('<Event><EventData><Data Name="param1">'+$this.Service+'</Data></EventData></Event>')}
  $event
 }
}
try{throw (New-Object ComponentModel.Win32Exception(1069))}catch{$failure=$_}
Write-WorkerServiceDiagnostic 'startup-failed' $since $failure
'OK'
'''
    path=tmp_path/'diagnostics.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1'),'-Case',case],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout
    assert 'synthetic-secret' not in result.stdout+result.stderr
    assert 'native_error=1069' in result.stdout
    if case=='events':
        assert 'matched=2' in result.stdout
        assert 'UnrelatedService' not in result.stdout
        assert '[REDACTED]' in result.stdout
    elif case=='none':
        assert 'No matching SCM events' in result.stdout
    else:
        assert 'service configuration unavailable' in result.stdout
        assert 'Worker SCM events unavailable' in result.stdout
