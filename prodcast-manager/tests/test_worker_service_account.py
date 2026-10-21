"""Real PowerShell identity/health functions, mocked service/account boundaries."""
import subprocess
import pytest
from prodcast_manager.engine import RESOURCES


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
