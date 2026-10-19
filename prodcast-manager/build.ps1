param([string]$Python='python',[string]$Output=(Join-Path $PSScriptRoot 'dist'),[switch]$SkipTests,[switch]$SkipSmoke)
$ErrorActionPreference='Stop'
$build=Join-Path $PSScriptRoot '.build'
if(-not $SkipTests){
    & $Python -m pytest (Join-Path $PSScriptRoot 'tests') -q
    if($LASTEXITCODE){
        Write-Warning 'Test run failed; retrying only failed tests once to tolerate transient Windows GUI initialization errors.'
        & $Python -m pytest (Join-Path $PSScriptRoot 'tests') -q --last-failed --last-failed-no-failures none
        if($LASTEXITCODE){throw 'Tests failed after one targeted retry'}
    }
    & $Python (Join-Path $PSScriptRoot 'release/check_html_ui.py')
    if($LASTEXITCODE){throw 'HTML desktop integration checks failed'}
}
$bundles=Join-Path $build 'onefile'
& $Python -m PyInstaller --noconfirm --clean --distpath $bundles --workpath (Join-Path $build 'gui') (Join-Path $PSScriptRoot 'release/manager.spec')
if($LASTEXITCODE){throw 'GUI build failed'}
New-Item -ItemType Directory -Path $Output -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $bundles 'ProdCast-Manager.exe') -Destination (Join-Path $Output 'ProdCast-Manager.exe') -Force
New-Item -ItemType Directory -Path (Join-Path $Output 'prodcast-data\logs'),(Join-Path $Output 'prodcast-data\sites') -Force | Out-Null
if(-not $SkipSmoke){
    $smoke=Start-Process -FilePath (Join-Path $Output 'ProdCast-Manager.exe') -ArgumentList 'self-test','--gui-smoke' -WindowStyle Hidden -PassThru
    if(-not $smoke.WaitForExit(120000)) {
        function Stop-SmokeTree([int]$TestProcessId) {
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$TestProcessId" | ForEach-Object { Stop-SmokeTree $_.ProcessId }
            Stop-Process -Id $TestProcessId -ErrorAction SilentlyContinue
        }
        Stop-SmokeTree $smoke.Id
        throw 'Packaged HTML smoke timed out'
    }
    $smoke.Refresh()
    if($smoke.ExitCode){throw 'Packaged HTML renderer smoke failed'}
}
