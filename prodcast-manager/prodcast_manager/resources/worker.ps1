param([Parameter(Mandatory)][string]$Request)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$p=Get-Content -Raw -LiteralPath $Request | ConvertFrom-Json
$root='C:\ProgramData\ProdCastManager'
$stateFile=Join-Path $root 'state.json'
$log=Join-Path $root 'operation.log'
if($p.role -notmatch '^worker([1-9]|1[0-6])$'){throw 'Invalid Worker role'}
$number=([int]$p.role.Substring(6)).ToString('D2')
$service='ProdCastWorker'+$number
$state=$null
$lock=$null
$transcript=$false
$executionMode='windows-appcontainer'
if($p.worker_execution_policy){
    $policy=$p.worker_execution_policy
    if($policy.schema -ne 1 -or $policy.execution_mode -ne 'windows-account' -or $policy.execution_timeout_seconds -ne 0){throw 'Unsupported Worker execution policy'}
    $executionMode='windows-account'
}

$executionQueues=@('workflows','workflows_data','scenarios')
$workerHost=$p.site.hosts.($p.role)
if($workerHost.PSObject.Properties['execution_queues']){
    $value=$workerHost.execution_queues
    if($value -isnot [Array] -or $value.Count -eq 0 -or @($value | Where-Object {$_ -cnotin @('workflows','workflows_data','scenarios')}).Count -or @($value | Select-Object -Unique).Count -ne $value.Count){throw 'Invalid Worker execution_queues'}
    $executionQueues=@($value)
}
function Test-WorkerQueues($siteConfig,$marker) {
    $configured=@('workflows','workflows_data','scenarios')
    if($siteConfig.PSObject.Properties['execution_queues']){$configured=@($siteConfig.execution_queues)}
    $expected=($executionQueues | Sort-Object) -join ','
    return (($configured | Sort-Object) -join ',') -ceq $expected -and ((@($marker.queues) | Sort-Object) -join ',') -ceq $expected
}
function Save-State {
    $tmp=$stateFile+'.tmp'
    [IO.File]::WriteAllText($tmp,($script:state | ConvertTo-Json -Depth 30),(New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $stateFile -Force
}
function Set-Field($obj,[string]$name,$value) { $obj | Add-Member -MemberType NoteProperty -Name $name -Value $value -Force }
function Check-Native([string]$name) { if($LASTEXITCODE -ne 0){throw "$name failed; inspect protected operation.log"} }
function Protect-Directory([string]$path) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    & icacls.exe $path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
    Check-Native 'Set directory ACL'
}
function Read-State {
    if(Test-Path -LiteralPath $stateFile){$script:state=Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json}
}
function Preflight {
    if(!([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'SSH account must be administrator'}
    if(![Environment]::Is64BitProcess){throw 'x64 PowerShell required'}
    $os=(Get-CimInstance Win32_OperatingSystem).Caption
    if($os -notmatch 'Windows Server (2019|2022|2025)'){throw "Supported workers: Windows Server 2019/2022/2025 x64; detected $os"}
    if(!(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $p.site.hosts.($p.role).address)){throw 'Configured IP is not assigned to this VM'}
    Read-State
    if($script:state){
        if($script:state.site -ne $p.site.site_id -or $script:state.topology -ne $p.topology -or $script:state.role -ne $p.role){throw 'Managed topology mismatch'}
        if($p.installation_id -and $script:state.installation_id -ne $p.installation_id){throw 'VM belongs to another vault; use original encrypted installation state'}
        if($script:state.maintenance -and $script:state.maintenance -ne $p.operation -and $p.action -notin @('preflight','release-operation')){throw 'A maintenance operation owns this VM; resume it from the original profile'}
        if($p.mode -in @('install','update','repair') -and $script:state.version -eq $p.version -and $script:state.manifest -ne $p.manifest){throw 'Same release version has a different manifest'}
        if($script:state.operation -and $script:state.operation -ne $p.operation -and $p.mode -in @('install','update','repair') -and $p.action -ne 'release-operation' -and $p.previous_operation -ne $script:state.operation){throw 'Another operation owns this VM; resume original journal'}
        $reclaim=$script:state.retired -eq $true -and $p.worker_expansion.reactivated_workers.($p.role).operation
        if($reclaim -and $script:state.retirement_operation -and $script:state.retirement_operation -ne $p.worker_expansion.reactivated_workers.($p.role).operation){throw 'Worker retirement receipt does not match'}
        if($p.mode -eq 'add-workers' -and $p.worker_expansion.new_workers -contains $p.role -and $script:state.worker_expansion -ne $p.operation -and !$reclaim){throw 'This VM is not owned by this Worker expansion; a cloned active Worker must be repaired or removed before re-adding'}
        return @{managed=$true;version=$script:state.version;hostname=$env:COMPUTERNAME;operation=$script:state.operation;manifest=$script:state.manifest;maintenance=$script:state.maintenance;worker_expansion=$script:state.worker_expansion;retired=($script:state.retired -eq $true)}
    }
    if(Get-Service -Name 'ProdCastWorker*' -ErrorAction SilentlyContinue){throw 'Unmanaged Worker service found; automatic adoption refused'}
    if(Test-Path 'C:\ProdCast\Managed'){throw 'Unmanaged target directory exists'}
    return @{managed=$false;version='';hostname=$env:COMPUTERNAME}
}
function Add-ServiceRights([string]$sid) {
    # Export the existing assignments and preserve each existing member.
    $policy=Join-Path $root 'rights-existing.inf'
    & secedit.exe /export /cfg $policy /areas USER_RIGHTS | Out-Null
    Check-Native 'Export user rights'
    $lines=Get-Content -LiteralPath $policy
    $body=@('[Unicode]','Unicode=yes','[Version]','signature="$CHICAGO$"','Revision=1','[Privilege Rights]')
    foreach($right in @('SeServiceLogonRight','SeDenyInteractiveLogonRight','SeDenyRemoteInteractiveLogonRight')){
        $found=@($lines | Where-Object {$_ -match ('^'+$right+'\s*=')})
        $members=@()
        if($found.Count){$members=@(($found[0] -split '=',2)[1].Trim() -split ',' | Where-Object {$_})}
        $members+=('*'+$sid)
        $body+=($right+' = '+(($members | Select-Object -Unique) -join ','))
    }
    $newPolicy=Join-Path $root 'rights-apply.inf'
    $body | Set-Content -LiteralPath $newPolicy -Encoding Unicode
    & secedit.exe /configure /db (Join-Path $root 'rights.sdb') /cfg $newPolicy /areas USER_RIGHTS /quiet | Out-Null
    Check-Native 'Apply service rights'
}
function Find-WorkerPython {
    $candidates=@('C:\ProdCast\ManagedPython314\python.exe')
    $registered='HKLM:\SOFTWARE\Python\PythonCore\3.14\InstallPath'
    if(Test-Path -LiteralPath $registered){
        $entry=Get-Item -LiteralPath $registered
        $exe=$entry.GetValue('ExecutablePath')
        if($exe){$candidates+=$exe}
        elseif($entry.GetValue('')){$candidates+=(Join-Path $entry.GetValue('') 'python.exe')}
    }
    foreach($candidate in ($candidates | Select-Object -Unique)){
        if(!(Test-Path -LiteralPath $candidate -PathType Leaf)){continue}
        $sig=Get-AuthenticodeSignature -LiteralPath $candidate
        if($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation'){continue}
        $runtime=& $candidate -I -c 'import platform,sys; print(platform.python_version(),64 if sys.maxsize>2**32 else 32)'
        if($LASTEXITCODE -eq 0 -and $runtime.Trim() -eq '3.14.7 64'){return $candidate}
    }
    return $null
}
function Resolve-ServiceIdentity([bool]$prepareManaged=$false) {
    $registration=Get-CimInstance Win32_Service -Filter "Name='$service'"
    $setting=$p.worker_service;$mode=if($setting){$setting.mode}else{'preserve'}
    if($mode -notin @('preserve','managed','custom')){throw 'Unsupported Worker service account mode'}
    $managed=$env:COMPUTERNAME+'\prodcast-managed'
    $account=if($mode -eq 'custom'){$setting.account}elseif($mode -eq 'preserve' -and $registration){$registration.StartName}else{$managed}
    if($account.StartsWith('.\')){$account=$env:COMPUTERNAME+$account.Substring(1)}
    $secret=if($mode -eq 'custom'){$setting.password}elseif($account -eq $managed){$p.secrets.('SVC_W'+$number)}else{''}
    $credential=$null
    if($secret){
        $secure=ConvertTo-SecureString $secret -AsPlainText -Force
        $credential=New-Object Management.Automation.PSCredential($account,$secure)
    }
    if($account -eq $managed -and ($mode -eq 'managed' -or !$registration)){
        if($prepareManaged){
            $user=Get-LocalUser -Name 'prodcast-managed' -ErrorAction SilentlyContinue
            if(!$user){$user=New-LocalUser -Name 'prodcast-managed' -Password $secure -UserMayNotChangePassword -PasswordNeverExpires -Description ('ProdCast Manager '+$p.site.site_id)}
            elseif($user.Description -ne ('ProdCast Manager '+$p.site.site_id)){throw 'Existing service account belongs to another installation'}
            if(Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object SID -eq $user.SID){throw 'Managed service account cannot be an administrator'}
            Add-ServiceRights $user.SID.Value
        }
    }
    $recreate=!$registration -or !$script:state.pending_root -or !$registration.PathName.Contains($script:state.pending_root)
    if($registration -and $script:state.steps -and $script:state.steps.PSObject.Properties[($p.operation+':install')] -and (Test-WorkerRuntime) -and $script:state.install_root.StartsWith(('C:\ProdCast\Managed\'+$p.version+'-'),[StringComparison]::OrdinalIgnoreCase)){$recreate=$false}
    if($mode -eq 'custom' -and !$credential -and $registration -and $registration.StartName -ne $account){throw 'A service password is required to change the existing logon account; no service was changed'}
    if($recreate -and !$credential){throw 'Preserving this service account during Worker replacement requires its credentials. Save the same approved account and password in Worker service account settings first. No service was stopped or changed.'}
    # Credential availability is checked here; SCM authenticates the account
    # when the configured service starts. A separate LogonUser probe can
    # disagree with service startup in a privileged-access session.
    return @{account=$account;credential=$credential;mode=$mode;registration=$registration}
}
function Invoke-WorkerInstaller($package,$installDir,$python,$credential) {
    $installerPath=Join-Path $package 'runtime\Install-Worker.ps1'
    # Narrow compatibility adapter for the verified legacy installer only.
    # Custom administrator accounts are supported. SCM validates credentials
    # at service startup; this does not edit domain policy or grant admin rights.
    $source=[IO.File]::ReadAllText($installerPath)
    $pattern='(?ms)^# Initial profile deliberately supports provisioned non-admin local accounts only\.\r?\n.*?(?=^\$name=)'
    $match=[regex]::Match($source,$pattern)
    if(!$match.Success -or !$match.Value.Contains('Supply a provisioned local service account as COMPUTERNAME\user.') -or !$match.Value.Contains('Administrator service account refused.')){throw 'This Worker installer does not have the reviewed service-account adapter contract; use a compatible Worker release'}
    $installerPath=Join-Path $package 'runtime\Install-Worker-ServiceAccount.ps1'
    [IO.File]::WriteAllText($installerPath,([regex]::Replace($source,$pattern,'# Service credentials are authenticated by SCM at startup.'+[Environment]::NewLine)))
    & $installerPath -InstallDir $installDir -PythonPath $python -WorkerNumber $number -ServiceCredential $credential | Out-Host
    if(!$?){throw 'Worker installer failed'}
}
function Write-WorkerServiceDiagnostic([string]$phase,[datetime]$since,$failure=$null) {
    # Read-only and best effort. Never let diagnostics mask the original failure.
    try{
        $details=Get-CimInstance Win32_Service -Filter "Name='$service'" -ErrorAction Stop
        $sid='unresolved'
        if($details.StartName){
            try{
                $account=$details.StartName
                if($account.StartsWith('.\')){$account=$env:COMPUTERNAME+$account.Substring(1)}
                $sid=(New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
            }catch{}
        }
        Write-Host ('Worker service diagnostic: phase='+$phase+'; host='+$env:COMPUTERNAME+'; operation='+$p.operation+'; service='+$service+'; account='+$details.StartName+'; sid='+$sid+'; state='+$details.State+'; start_mode='+$details.StartMode+'; pid='+$details.ProcessId+'; exit_code='+$details.ExitCode+'; service_exit_code='+$details.ServiceSpecificExitCode)
    }catch{Write-Host 'Worker service diagnostic: service configuration unavailable.'}
    if(!$failure){return}
    try{
        $exception=$failure.Exception
        for($depth=0;$exception -and $depth -lt 8;$depth++){
            Write-Host ('Worker startup exception: type='+$exception.GetType().FullName+'; hresult='+$exception.HResult+'; native_error='+$exception.NativeErrorCode)
            $exception=$exception.InnerException
        }
    }catch{Write-Host 'Worker service diagnostic: exception codes unavailable.'}
    try{
        $until=Get-Date
        $events=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Service Control Manager';StartTime=$since;EndTime=$until;Id=@(7000,7009,7011,7023,7024,7036,7038,7040,7041)} -MaxEvents 256 -ErrorAction Stop)
        $matched=@($events | Where-Object {
            $eventXml=[xml]$_.ToXml()
            @($eventXml.Event.EventData.Data | Where-Object { $_.'#text' -eq $service -or ($details.DisplayName -and $_.'#text' -eq $details.DisplayName) }).Count -gt 0
        } | Select-Object -First 12)
        Write-Host ('Worker SCM events: from='+$since.ToString('o')+'; to='+$until.ToString('o')+'; matched='+$matched.Count+'; limits=256 provider events / 12 matching events')
        foreach($event in $matched){
            $message=$event.Message
            # Do not print request objects or credentials, even in diagnostic errors.
            if($p.worker_service.password){$message=$message.Replace([string]$p.worker_service.password,'[REDACTED]')}
            Write-Host ('Worker SCM event: time='+$event.TimeCreated.ToString('o')+'; id='+$event.Id+'; record='+$event.RecordId+'; message='+$message)
        }
        if(!$matched.Count){Write-Host 'No matching SCM events available yet; this does not prove that service logon succeeded.'}
    }catch{Write-Host 'Worker SCM events unavailable or not yet recorded; inspect the Windows System log for this attempt.'}
}
function Start-InstalledWorker($package,$installDir) {
    $started=Get-Date
    try{
        Write-WorkerServiceDiagnostic 'before-start' $started
        Set-Service -Name $service -StartupType Manual
        & (Join-Path $package 'runtime\Start-Worker.ps1') -InstallDir $installDir -WorkerNumber $number | Out-Host
        if(!$?){throw 'Worker startup script failed; inspect operation.log and Service Control Manager events'}
        $running=Get-CimInstance Win32_Service -Filter "Name='$service'"
        if(!$running -or $running.State -ne 'Running'){throw 'Worker service did not reach Running state'}
        $marker=Get-Content -Raw -LiteralPath (Join-Path $installDir 'logs\scheduler-service-ready.json') | ConvertFrom-Json
        if($marker.mode -ne $executionMode -or !$marker.postgres_tls){throw 'Worker isolation/TLS readiness failed'}
        Write-WorkerServiceDiagnostic 'ready' $started
        return $marker
    }catch{
        $startupFailure=$_
        try{Write-WorkerServiceDiagnostic 'startup-failed' $started $startupFailure}catch{}
        throw
    }
}
function Recover-WorkerStartupFailure {
    # Stop any partial process tree first. Only a fully configured target may
    # be left manually startable; old/incomplete runtimes stay disabled.
    Stop-OwnedWorker
    $prepared=$script:state.worker_start
    if(!$prepared -or $prepared.operation -ne $p.operation){return}
    if($prepared.path -ne $script:state.pending_root){return}
    $registration=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if(!$registration -or $registration.State -ne 'Stopped' -or
       $registration.PathName.Trim('"') -ne (Join-Path $prepared.path ($service+'.exe'))){return}
    foreach($name in @('site.json','scheduler.machine.dpapi','prodcast-db-ca.crt','prodcast-app-ca.crt','payload\service_runner.pyc')){
        if(!(Test-Path -LiteralPath (Join-Path $prepared.path $name) -PathType Leaf)){return}
    }
    Set-Service -Name $service -StartupType Manual
    Write-Host 'Prepared Worker is stopped and enabled for retry; Manager will reapply configured credentials and start it on resume. Original startup mode is retained until readiness succeeds.'
}
function Install-Worker {
    if($p.mode -eq 'add-workers' -and $script:state.worker_expansion -eq $p.operation -and (Test-WorkerRuntime)){
        return @{service=$service;preserved=$true}
    }
    $asset=$p.files.'worker-windows-amd64'
    $zip=Join-Path $p.stage $asset.name
    if((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256){throw 'Worker ZIP checksum mismatch'}
    $package=Join-Path $root ('packages\'+$p.version)
    if(!(Test-Path (Join-Path $package '.complete'))){
        Protect-Directory $package
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z=[IO.Compression.ZipFile]::OpenRead($zip)
        try{
            $seen=@{};[long]$total=0
            foreach($entry in $z.Entries){
                $name=$entry.FullName
                if($name -match '(^/|\\|:|(^|/)\.\.(/|$))' -or $seen.ContainsKey($name.ToLowerInvariant())){throw 'Unsafe ZIP entry'}
                $seen[$name.ToLowerInvariant()]=$true; $total+=$entry.Length
                if($total -gt 8GB){throw 'Worker ZIP too large'}
                $target=[IO.Path]::GetFullPath((Join-Path $package $name))
                if(!$target.StartsWith($package+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe extraction path'}
                if($name.EndsWith('/')){New-Item -ItemType Directory -Force -Path $target | Out-Null}
                else{
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$true)
                }
            }
        }finally{$z.Dispose()}
        [IO.File]::WriteAllText((Join-Path $package '.complete'),$asset.sha256)
    } elseif((Get-Content -Raw (Join-Path $package '.complete')) -ne $asset.sha256){throw 'Release version already cached with another digest'}
    $adapted=$p.files.'worker-windows-amd64'.sha256 -in @('68a8ca761fe02fe0c8899b8e05823c787f661cba7ccd59ba83c8a7427014cd3e','a7d370571eb0355951fb4bb0019e21cf6e7813dfa397bd6ee2374805de7f0b79','f613c85082e99429172be0b0b5aebfeef34ebb4e6a2c4f291a90ef0147037504','96a25f34cbc9541227745a168e9e8ae442cc7f3b92329ea5a62464dd625bfd38')
    if([int]$number -gt 2 -and $adapted){
        foreach($scriptName in @('Install-Worker.ps1','Start-Worker.ps1')){
        $runtimeInstaller=Join-Path $package ('runtime\'+$scriptName)
        $source=[IO.File]::ReadAllText($runtimeInstaller)
        $old="[ValidateSet('01','02')]"
        $replacement="[ValidatePattern('^(0[1-9]|1[0-6])$')]"
        if(!$source.Contains($old) -and !$source.Contains($replacement)){throw 'Unsupported Worker installer adapter'}
        [IO.File]::WriteAllText($runtimeInstaller,$source.Replace($old,$replacement))
        }
    }
    $installer=Join-Path $package 'python-3.14.7-amd64.exe'
    if((Get-FileHash $installer -Algorithm SHA256).Hash -ne '9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649'){throw 'Python installer checksum mismatch'}
    $python=Find-WorkerPython
    if(!$python){
        $sig=Get-AuthenticodeSignature -LiteralPath $installer
        if($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation'){throw 'Python installer signature not valid'}
        $install=Start-Process -FilePath $installer -Wait -PassThru -WindowStyle Hidden -ArgumentList @('/quiet','InstallAllUsers=1','TargetDir=C:\ProdCast\ManagedPython314','PrependPath=0','Include_test=0')
        if($install.ExitCode -eq 3010){throw 'Python installed; reboot Worker VM and repeat operation'}
        if($install.ExitCode -ne 0){throw 'Python installation failed'}
        $python=Find-WorkerPython
        if(!$python){throw 'Python installer completed, but a signed Python 3.14.7 x64 runtime was not found in the managed path or machine registry'}
    }
    $version=& $python -c 'import platform; print(platform.python_version())'
    Check-Native 'Python version check'
    if($version.Trim() -ne '3.14.7'){throw 'Worker requires Python 3.14.7 exactly'}
    $identity=Resolve-ServiceIdentity $true
    $account=$identity.account;$credential=$identity.credential
    Write-Host ('Worker service credentials: mode='+$identity.mode+'; account='+$account+'; credentials_present='+[bool]$credential)
    Set-Field $script:state 'worker_start' $null; Save-State
    if(!$script:state.pending_root){
        Set-Field $script:state 'pending_root' ('C:\ProdCast\Managed\'+$p.version+'-'+[guid]::NewGuid().ToString('N').Substring(0,8))
        Save-State
    }
    $installDir=$script:state.pending_root
    $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if($svc -and !$svc.PathName.Contains($installDir)){
        if(!$script:state.install_root -or !$svc.PathName.Contains($script:state.install_root)){throw 'Refusing to replace an unowned service'}
        Assert-WorkerStopped
        & sc.exe delete $service | Out-Null; Check-Native 'Remove stopped old service registration'
        for($i=0;$i -lt 30 -and (Get-Service $service -ErrorAction SilentlyContinue);$i++){Start-Sleep -Seconds 1}
        $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
        if($svc){throw 'Service deletion pending; close service consoles and retry'}
    }
    if(!$svc){
        # A failed partial directory is retained, and a fresh version directory is used.
        if(Test-Path -LiteralPath $installDir){
            $installDir='C:\ProdCast\Managed\'+$p.version+'-'+[guid]::NewGuid().ToString('N').Substring(0,8)
            Set-Field $script:state 'pending_root' $installDir; Save-State
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installDir) | Out-Null
        Write-Host 'Worker service credentials: creating replacement registration with supplied credentials.'
        Invoke-WorkerInstaller $package $installDir $python $credential
        Write-Host 'Worker service credentials: installer completed; service logon is not verified until startup.'
    }
    elseif($identity.mode -in @('custom','managed') -and ($credential -or $svc.StartName -ne $account)){
        if(!$credential){throw 'A password is required to explicitly change service logon'}
        if($svc.State -ne 'Stopped'){throw 'Stop the selected Worker through its reviewed update/repair action before changing the service account'}
        Write-Host 'Worker service credentials: reapplying supplied account and password to the existing stopped service.'
        $change=Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{StartName=$account;StartPassword=$credential.GetNetworkCredential().Password}
        Write-Host ('Worker service credentials: Windows Change returned '+$change.ReturnValue+'; service logon is not verified until startup.')
        if($change.ReturnValue -ne 0){throw ('Service account change failed: '+$change.ReturnValue)}
        $sid=(New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
        & icacls.exe $installDir /grant:r ('*'+$sid+':(OI)(CI)(RX)') | Out-Null;Check-Native 'Grant service runtime access'
        foreach($folder in @('logs','jobs','recovery')){& icacls.exe (Join-Path $installDir $folder) /grant:r ('*'+$sid+':(OI)(CI)(M)') | Out-Null;Check-Native 'Grant service data access'}
    }else{Write-Host 'Worker service credentials: preserving existing Windows service credentials; no password rewrite.'}
    if([int]$number -gt 2 -and $adapted){
        $runner=Join-Path $p.stage 'worker-service-runner.py'
        if((Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant() -ne '9e8e41e6b84f202c579e93bbad8c33cb81be38af64ae239c4f32ab2969a87ad2'){throw 'Worker adapter checksum mismatch'}
        $runnerTarget=Join-Path $installDir 'payload\service_runner.pyc'
        & $python -I -c 'import py_compile,sys;py_compile.compile(sys.argv[1],cfile=sys.argv[2],doraise=True)' $runner $runnerTarget
        Check-Native 'Compile compatible Worker entry point'
    }
    $site=[ordered]@{worker_number=$number;postgres_host=$p.site.hosts.db.address;postgres_port=5432;postgres_db='prodcast2';postgres_user=('prodcast_worker_'+$number);redis_host=$p.site.hosts.db.address;redis_port=6380;redis_user=('prodcast_worker_'+$number+'_scheduler');main_server_url=$p.site.public_url.TrimEnd('/')}
    if($p.worker_execution_policy){
        $site.execution_mode=$executionMode
        $site.execution_timeout_seconds=0
        $site.scenario_queue='scenarios'
        $site.workflow_model_queue='workflows'
        $site.workflow_data_queue='workflows_data'
    }
    $site.execution_queues=@($executionQueues)
    $site | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installDir 'site.json') -Encoding UTF8
    foreach($name in @('prodcast-db-ca.crt','prodcast-app-ca.crt')){[IO.File]::WriteAllText((Join-Path $installDir $name),$p.tls.'ca.crt')}
    Add-Type -AssemblyName System.Security
    $values=@{postgres_password=$p.secrets.('PW_W'+$number);redis_password=$p.secrets.('REDIS_W'+$number);media_key=$p.secrets.MEDIA_KEY;signing_key=$p.secrets.SIGNING_KEY}
    $plain=[Text.Encoding]::UTF8.GetBytes(($values | ConvertTo-Json -Compress))
    try{[IO.File]::WriteAllBytes((Join-Path $installDir 'scheduler.machine.dpapi'),[Security.Cryptography.ProtectedData]::Protect($plain,$null,[Security.Cryptography.DataProtectionScope]::LocalMachine))}
    finally{[Array]::Clear($plain,0,$plain.Length);$values.Clear()}
    $svc=Get-Service $service
    if($svc.Status -eq 'Running'){Stop-Service $service; $svc.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(90))}
    # Only the fully prepared target runtime is allowed to start. The old
    # service remains disabled throughout the backup/replacement interval.
    # Persist this only after credentials, ACLs, configuration and secrets are
    # ready. Never enable an incomplete replacement from the error handler.
    Set-Field $script:state 'worker_start' @{operation=$p.operation;path=$installDir}
    Save-State
    $marker=Start-InstalledWorker $package $installDir
    if(!(Test-WorkerQueues ([pscustomobject]$site) $marker)){throw 'Worker queue readiness mismatch'}
    Restore-WorkerStartup
    Set-Field $script:state 'previous_root' $script:state.install_root
    Set-Field $script:state 'install_root' $installDir
    Set-Field $script:state 'pending_root' ''; Save-State
    return @{service=$service;mode=$marker.mode;postgres_tls=$true;path=$installDir}
}
function Test-WorkerRuntime {
    if(!$script:state.install_root){return $false}
    $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if(!$svc -or $svc.State -ne 'Running' -or !$svc.PathName.Contains($script:state.install_root)){return $false}
    foreach($name in @('site.json','scheduler.machine.dpapi','payload\service_runner.pyc','logs\scheduler-service-ready.json')){
        if(!(Test-Path -LiteralPath (Join-Path $script:state.install_root $name) -PathType Leaf)){return $false}
    }
    return $true
}
function Wait-WorkerMaintenanceReady([int]$timeoutSeconds=120) {
    $started=Get-Date
    $attempts=[Math]::Max(1,[Math]::Ceiling($timeoutSeconds/2))
    for($attempt=0;$attempt -le $attempts;$attempt++){
        if(Test-WorkerRuntime){return}
        if($attempt -eq 0){Write-Host ('Waiting for Worker service and scheduler readiness; maximum '+$timeoutSeconds+' seconds, returning as soon as ready.')}
        if($attempt -lt $attempts){Start-Sleep -Seconds 2}
    }
    try{Write-WorkerServiceDiagnostic 'maintenance-readiness-timeout' $started}catch{}
    foreach($name in @('site.json','scheduler.machine.dpapi','payload\service_runner.pyc','logs\scheduler-service-ready.json')){
        Write-Host ('Worker maintenance readiness: file='+$name+'; exists='+[bool](Test-Path -LiteralPath (Join-Path $script:state.install_root $name) -PathType Leaf))
    }
    throw ('Worker failed its runtime check after restart; readiness was not reached within '+$timeoutSeconds+' seconds. Inspect the Worker logs and service state.')
}
function Repair-Worker {
    if(Test-WorkerRuntime){
        $siteConfig=Get-Content -Raw -LiteralPath (Join-Path $script:state.install_root 'site.json') | ConvertFrom-Json
        $marker=Get-Content -Raw -LiteralPath (Join-Path $script:state.install_root 'logs\scheduler-service-ready.json') | ConvertFrom-Json
        if(!(Test-WorkerQueues $siteConfig $marker)){throw 'Worker queues differ; use Update or selected Worker repair to apply the saved queues'}
        return @{service=$service;preserved=$true}
    }
    $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if($svc){
        if(!$script:state.install_root -or !$svc.PathName.Contains($script:state.install_root)){throw 'Refusing to replace an unowned service'}
        if($svc.State -ne 'Stopped'){throw 'Worker is still running; stop its jobs and service before repairing damaged files'}
    }
    # Install-Worker preserves the old directory, repairs dependencies and creates
    # a fresh runtime when the owned service is stopped or missing.
    return Install-Worker
}
function Test-ExistingWorker {
    if(!$script:state -or $script:state.operation -notin @('',$null,$p.operation) -or $script:state.maintenance){throw 'Worker healthcheck ownership conflict'}
    $registration=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if(!$registration -or $registration.State -ne 'Running'){throw 'Worker service is not running'}
    if(($script:state.version -ne $p.version -or $script:state.manifest -ne $p.manifest) -and $script:state.operation -ne $p.operation){throw 'Worker release receipt does not match the selected installed release'}
    if($p.worker_service -and $p.worker_service.mode -eq 'custom' -and $registration.StartName -ne $p.worker_service.account){throw 'Running Worker service account differs from the configured account; healthcheck does not change it'}
    $candidate=$null
    foreach($path in @($script:state.install_root,$script:state.pending_root)){
        if($path -and $registration.PathName.Trim('"') -eq (Join-Path $path ($service+'.exe'))){$candidate=$path;break}
    }
    if(!$candidate -or !$candidate.StartsWith(('C:\ProdCast\Managed\'+$p.version+'-'),[StringComparison]::OrdinalIgnoreCase)){throw 'Worker runtime path does not match the expected installed release'}
    foreach($name in @('site.json','scheduler.machine.dpapi','payload\service_runner.pyc','logs\scheduler-service-ready.json')){
        if(!(Test-Path -LiteralPath (Join-Path $candidate $name) -PathType Leaf)){throw ('Worker readiness file missing: '+$name)}
    }
    $siteConfig=Get-Content -Raw -LiteralPath (Join-Path $candidate 'site.json') | ConvertFrom-Json
    $marker=Get-Content -Raw -LiteralPath (Join-Path $candidate 'logs\scheduler-service-ready.json') | ConvertFrom-Json
    if($siteConfig.worker_number -ne $number -or $siteConfig.postgres_host -ne $p.site.hosts.db.address -or $marker.mode -ne $executionMode -or !$marker.postgres_tls){throw 'Worker identity/isolation/TLS readiness mismatch'}
    if(!(Test-WorkerQueues $siteConfig $marker)){throw 'Worker queue readiness mismatch'}
    return @{healthy=$true;service=$service;account=$registration.StartName;path=$candidate;postgres_tls=$true;services_restarted=$false}
}
function Get-WorkerProcesses($registration) {
    if(!$registration.ProcessId){return @()}
    $all=@(Get-CimInstance Win32_Process)
    $ids=@([int]$registration.ProcessId)
    do {
        $children=@($all | Where-Object {$_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids})
        $ids+=@($children | ForEach-Object {[int]$_.ProcessId})
    } while($children.Count)
    return @($all | Where-Object {$_.ProcessId -in $ids} | ForEach-Object {@{pid=[int]$_.ProcessId;created=[string]$_.CreationDate}})
}
function Assert-WorkerStopped {
    $current=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if($current -and ($current.State -ne 'Stopped' -or $current.StartMode -ne 'Disabled' -or $current.ProcessId)){
        throw 'Worker must be stopped and disabled throughout replacement; no service registration was removed'
    }
    foreach($tracked in @($script:state.worker_stop.processes)){
        if(!$tracked){continue}
        $live=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$tracked.pid)
        if($live -and [string]$live.CreationDate -eq $tracked.created){throw 'Owned Worker process tree has not fully exited; replacement refused'}
    }
}
function Restore-WorkerStartup {
    $saved=$script:state.worker_stop
    if(!$saved -or $saved.operation -ne $p.operation){return}
    $mode=switch($saved.start_mode){'Disabled'{'Disabled'};'Manual'{'Manual'};default{'Automatic'}}
    Set-Service -Name $service -StartupType $mode
    if($mode -eq 'Automatic' -and $saved.delayed_auto){
        & sc.exe config $service start= delayed-auto | Out-Null;Check-Native 'Restore Worker startup mode'
    }
}
function Stop-OwnedWorker {
    # Caller either resets explicitly or has already paused/drained App jobs.
    # Prevent SCM recovery from racing maintenance.
    $registration=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if(!$registration){return}
    $registeredOwner=$false
    foreach($candidate in @($script:state.install_root,$script:state.pending_root)){
        if($candidate -and $registration.PathName.Trim('"') -eq (Join-Path $candidate ($service+'.exe'))){$registeredOwner=$true}
    }
    if(!$registeredOwner){throw 'Refusing to stop an unowned Worker service'}
    $processes=Get-WorkerProcesses $registration
    $saved=$script:state.worker_stop
    if(!$saved -or $saved.operation -ne $p.operation){
        $delayed=(Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\'+$service) -Name DelayedAutostart -ErrorAction SilentlyContinue).DelayedAutostart
        $saved=@{operation=$p.operation;start_mode=$registration.StartMode;delayed_auto=[bool]$delayed;processes=@()}
    }
    $saved.processes=@($saved.processes)+@($processes)
    Set-Field $script:state 'worker_stop' $saved;Save-State
    Set-Service -Name $service -StartupType Disabled
    Write-Host 'Worker disabled for maintenance; automatic recovery cannot restart it during replacement.'
    try {Stop-Service -Name $service -ErrorAction Stop} catch {Write-Host 'Waiting for the managed Worker to stop.'}
    Write-Host 'Checking service shutdown and tracked processes; allowing up to 90 seconds for graceful stop before the owned-process fallback.'
    for($i=0;$i -lt 45;$i++){
        $current=Get-CimInstance Win32_Service -Filter "Name='$service'"
        try {Assert-WorkerStopped;Write-Host 'Worker service and tracked process tree are stopped; service remains disabled.';return} catch {}
        Start-Sleep -Seconds 2
    }
    $current=Get-CimInstance Win32_Service -Filter "Name='$service'"
    if(!$current -or $current.State -eq 'Stopped'){Assert-WorkerStopped;return}
    # A broken wrapper may refuse STOP. Terminate only the owned service tree,
    # after checking the live PID executable against this installation's roots.
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$current.ProcessId)
    $owned=$false
    foreach($candidate in @($script:state.install_root,$script:state.pending_root)){
        if($candidate -and $process.ExecutablePath -and
           [IO.Path]::GetFullPath($process.ExecutablePath) -eq (Join-Path $candidate ($service+'.exe')) -and
           $current.PathName.Trim('"') -eq $process.ExecutablePath){$owned=$true}
    }
    if(!$owned){throw 'Refusing to terminate an unowned Worker process'}
    Write-Host 'Graceful stop timed out; terminating the verified managed Worker process tree.'
    & taskkill.exe /PID $current.ProcessId /T /F | Out-Null
    Check-Native 'Terminate owned Worker for maintenance'
    (Get-Service -Name $service).WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))
    Assert-WorkerStopped
}
function Dispatch {
    switch($p.action){
        'maintenance-claim' {
            if($script:state.operation -and $p.mode -notlike 'reset-*'){throw 'Finish or reset the interrupted deployment first'}
            if($script:state){Set-Field $script:state 'maintenance' $p.operation;Save-State}
            return @{claimed=$true}
        }
        'maintenance-release' {
            if($script:state){$script:state.PSObject.Properties.Remove('maintenance');Save-State}
            return @{released=$true}
        }
        'maintenance-snapshot' {
            $svc=Get-Service -Name $service -ErrorAction SilentlyContinue
            $details=Get-CimInstance Win32_Service -Filter "Name='$service'"
            $delayed=(Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\'+$service) -Name DelayedAutostart -ErrorAction SilentlyContinue).DelayedAutostart
            return @{running=($svc -and $svc.Status -eq 'Running');start_mode=$details.StartMode;delayed_auto=[bool]$delayed}
        }
        'maintenance-stop' {
            $svc=Get-Service -Name $service -ErrorAction SilentlyContinue
            if($svc){Stop-OwnedWorker}
            return @{stopped=$true}
        }
        'maintenance-resume' {
            if(!(Get-Service -Name $service -ErrorAction SilentlyContinue)){
                if($p.previous_runtime.running){throw 'Previously running Worker service is missing'}
                return @{started=$false}
            }
            $mode=switch($p.previous_runtime.start_mode){'Disabled'{'Disabled'};'Manual'{'Manual'};default{'Automatic'}}
            if($p.previous_runtime.running){
                Set-Service -Name $service -StartupType Manual
                Start-Service -Name $service
            }
            Set-Service -Name $service -StartupType $mode
            if($mode -eq 'Automatic' -and $p.previous_runtime.delayed_auto){
                & sc.exe config $service start= delayed-auto | Out-Null;Check-Native 'Restore Worker startup mode'
            }
            return @{started=$true}
        }
        'maintenance-health' {
            if($p.previous_runtime.running){Wait-WorkerMaintenanceReady}
            return @{healthy=$true}
        }
        'reset' {
            if(!$script:state){return @{reset=$true;already_clean=$true}}
            $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
            if($svc){
                $owned=$false
                foreach($candidate in @($script:state.install_root,$script:state.pending_root)){
                    if($candidate -and $svc.PathName.Contains($candidate)){$owned=$true}
                }
                if(!$owned){throw 'Unowned service; reset refused'}
                Stop-OwnedWorker
                & sc.exe delete $service | Out-Null;Check-Native 'Delete managed Worker service'
                for($i=0;$i -lt 30 -and (Get-Service $service -ErrorAction SilentlyContinue);$i++){Start-Sleep -Seconds 1}
                if(Get-Service $service -ErrorAction SilentlyContinue){throw 'Service removal is pending; close service consoles and retry'}
            }
            if($p.mode -eq 'reset-full'){
                $target=[IO.Path]::GetFullPath('C:\ProdCast\Managed')
                if($target -ne 'C:\ProdCast\Managed'){throw 'Unsafe reset target'}
                if(Test-Path -LiteralPath $target){
                    if((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reset target is a reparse point'}
                    if(Get-ChildItem -LiteralPath $target -Recurse -Force | Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}){throw 'Reparse point in managed directory'}
                    Remove-Item -LiteralPath $target -Recurse -Force
                }
                $managedUser=Get-LocalUser -Name 'prodcast-managed' -ErrorAction SilentlyContinue
                if($managedUser -and $managedUser.Description -eq ('ProdCast Manager '+$p.site.site_id)){
                    if(Get-CimInstance Win32_Service | Where-Object {$_.StartName -eq ($env:COMPUTERNAME+'\prodcast-managed')}){throw 'Another service still uses the managed account'}
                    Remove-LocalUser -Name 'prodcast-managed'
                }
                Remove-Item -LiteralPath $stateFile -Force
                $script:state=$null
            }else{
                Set-Field $script:state 'operation' '';Set-Field $script:state 'steps' ([pscustomobject]@{});Save-State
            }
            return @{reset=$true;data_preserved=($p.mode -ne 'reset-full');dependencies_preserved=$true;server_backups_preserved=$true}
        }
        'worker-retire' {
            if($p.mode -ne 'worker-remove' -or $p.worker_action.role -ne $p.role -or $script:state.maintenance -ne $p.operation){throw 'Invalid Worker removal ownership'}
            $result=Dispatch-WorkerRetire
            return $result
        }
        'claim' {
            if(!$script:state){$script:state=[pscustomobject]@{site=$p.site.site_id;topology=$p.topology;installation_id=$p.installation_id;role=$p.role;version='';steps=[pscustomobject]@{}}}
            if($p.mode -eq 'add-workers'){Set-Field $script:state 'worker_expansion' $p.operation}
            if($p.mode -eq 'add-workers' -and $script:state.retired){
                Set-Field $script:state 'retired' $false
                Set-Field $script:state 'steps' ([pscustomobject]@{})
                Set-Field $script:state 'pending_root' ''
            }
            Set-Field $script:state 'operation' $p.operation; Save-State; return @{}
        }
        'release-operation' {
            if($script:state.operation -and $script:state.operation -ne $p.operation){throw 'The VM is owned by a different operation; refusing to release it'}
            Set-Field $script:state 'operation' ''; Save-State
            return @{released=$true;operation=$p.operation}
        }
        'install' {return Install-Worker}
        'service-preflight' {$identity=Resolve-ServiceIdentity $false;return @{credentials_available=$true;authentication='service-start';account=$identity.account;preserved=($identity.mode -eq 'preserve')}}
        'healthcheck' {return Test-ExistingWorker}
        'reconcile' {
            $ready=Test-ExistingWorker
            if($script:state.install_root -ne $ready.path){Set-Field $script:state 'previous_root' $script:state.install_root}
            Set-Field $script:state 'install_root' $ready.path;Set-Field $script:state 'pending_root' ''
            Set-Field $script:state 'version' $p.version;Set-Field $script:state 'manifest' $p.manifest
            Set-Field $script:state 'operation' '';Save-State
            return $ready
        }
        'verify' {
            if(!(Test-WorkerRuntime)){throw 'Worker service readiness failed'}
            $marker=Get-Content -Raw -LiteralPath (Join-Path $script:state.install_root 'logs\scheduler-service-ready.json') | ConvertFrom-Json
            if($marker.mode -ne $executionMode -or !$marker.postgres_tls){throw 'Worker isolation/TLS readiness failed'}
            $siteConfig=Get-Content -Raw -LiteralPath (Join-Path $script:state.install_root 'site.json') | ConvertFrom-Json
            if(!(Test-WorkerQueues $siteConfig $marker)){throw 'Worker queue readiness mismatch'}
            return @{healthy=$true;service=$service;postgres_tls=$true}
        }
        'repair' {
            if($script:state.version -ne $p.version -or $script:state.manifest -ne $p.manifest){throw 'Recovery requires the exact installed release'}
            return Repair-Worker
        }
        'stop' {
            Stop-OwnedWorker
            return @{stopped=$true;disabled=$true;live_state_verified=$true}
        }
        'backup' {
            # Compatibility for an older pending action: runtime is reinstallable,
            # models are backed up separately, and no Worker ZIP is created.
            Write-Host 'Worker backup skipped: runtime is reinstalled from the release; model files require a separate user backup.'
            return @{skipped=$true;reason='reinstallable-worker-runtime';models_included=$false}
        }
        'status' {
            $svc=Get-CimInstance Win32_Service -Filter "Name='$service'"
            return @{version=$script:state.version;service=$service;state=$svc.State;account=$svc.StartName;path=$script:state.install_root}
        }
        'commit' {
            Set-Field $script:state 'version' $p.version; Set-Field $script:state 'manifest' $p.manifest
            Set-Field $script:state 'operation' ''; Save-State
            return @{version=$p.version}
        }
        default {throw 'Unsupported Worker action'}
    }
}
function Dispatch-WorkerRetire {
    $svc=Get-Service -Name $service -ErrorAction SilentlyContinue
    if($svc){
        Stop-OwnedWorker
        & sc.exe delete $service | Out-Null;Check-Native 'Remove selected Worker service'
        for($i=0;$i -lt 30 -and (Get-Service $service -ErrorAction SilentlyContinue);$i++){Start-Sleep -Seconds 1}
        if(Get-Service $service -ErrorAction SilentlyContinue){throw 'Service removal pending; close service consoles and retry'}
    }
    Set-Field $script:state 'retired' $true
    Set-Field $script:state 'retirement_operation' $p.operation
    Set-Field $script:state 'operation' ''
    Save-State
    return @{removed=$true;runtime_preserved=$true;logs_preserved=$true}
}
try{
    $info=Preflight
    if($p.action -eq 'preflight'){$result=$info}
    else{
        Protect-Directory $root
        $lock=[IO.File]::Open((Join-Path $root 'operation.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        Start-Transcript -Path $log -Append | Out-Null; $transcript=$true
        $key=$p.operation+':'+$p.action
        $done=if($script:state -and $script:state.steps){$script:state.steps.PSObject.Properties[$key]}else{$null}
        if($done -and $p.action -eq 'install' -and !(Test-WorkerRuntime)){
            $result=Repair-Worker
            Set-Field $script:state.steps $key $result; Save-State
        }
        elseif($done -and $p.action -notin @('claim','commit','status','repair','verify','healthcheck','reconcile','service-preflight','stop','maintenance-stop')){$result=$done.Value}
        else{
            $result=Dispatch
            if($p.mode -in @('install','update','repair','add-workers')){Set-Field $script:state.steps $key $result; Save-State}
        }
    }
    if($transcript){Stop-Transcript | Out-Null;$transcript=$false}
    Write-Output ('MANAGER_RESULT:'+($result | ConvertTo-Json -Depth 20 -Compress))
}catch{
    $failure=$_
    if($p.action -in @('install','repair') -and $script:state.worker_stop.operation -eq $p.operation){
        try{Recover-WorkerStartupFailure}catch{Write-Host 'Worker failure recovery could not complete; Manager will recheck the owned service on retry. Inspect the operation log.'}
    }
    if($transcript){Write-Output $failure.Exception.Message; Write-Output $failure.ScriptStackTrace; Stop-Transcript | Out-Null}
    # Third-party error details stay on the server; never echo credentials or command bodies.
    Write-Output 'MANAGER_ERROR:Worker action failed. Inspect C:\ProgramData\ProdCastManager\operation.log and resume the same operation.'
    exit 1
}finally{if($lock){$lock.Dispose()}}
