"""Real PowerShell identity/health functions, mocked service/account boundaries."""
import subprocess
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
function Test-ServiceLogon {param($Credential) $script:validated=$Credential.UserName}
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
if($script:validated -ne 'EXAMPLE\approved'){throw 'Service logon was not checked'}
$script:svc.State='Stopped'
try{Test-ExistingWorker;throw 'Expected health refusal'}catch{if($_.Exception.Message -notmatch 'not running'){throw}}
'OK'
'''
    path=tmp_path/'service-account.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout
