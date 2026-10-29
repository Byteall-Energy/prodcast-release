param([string]$Python='python',[string]$Output=(Join-Path $PSScriptRoot 'dist'),[switch]$SkipTests,[switch]$SkipSmoke,[switch]$TestsOnly)
$ErrorActionPreference='Stop'
$build=Join-Path $PSScriptRoot '.build'
if($TestsOnly -and $SkipTests){throw 'TestsOnly cannot be combined with SkipTests'}
if(-not $SkipTests){
    $reports=Join-Path $build 'test-results'
    New-Item -ItemType Directory -Path $reports -Force | Out-Null
    $retryReport=Join-Path $reports 'retry.xml'
    if(Test-Path -LiteralPath $retryReport){Remove-Item -LiteralPath $retryReport -Force}
    & $Python -m pytest (Join-Path $PSScriptRoot 'tests') -q --junitxml (Join-Path $reports 'full-suite.xml')
    if($LASTEXITCODE){
        Write-Warning 'Test run failed; retrying only failed tests once to tolerate transient Windows GUI initialization errors.'
        & $Python -m pytest (Join-Path $PSScriptRoot 'tests') -q --last-failed --last-failed-no-failures none --junitxml $retryReport
        if($LASTEXITCODE){throw 'Tests failed after one targeted retry'}
    }
    & $Python (Join-Path $PSScriptRoot 'release/check_tk_ui.py')
    if($LASTEXITCODE){throw 'Tk workspace integration checks failed'}
}
if($TestsOnly){return}
$bundles=Join-Path $build 'onefile'
& $Python -m PyInstaller --noconfirm --clean --distpath $bundles --workpath (Join-Path $build 'gui') (Join-Path $PSScriptRoot 'release/manager.spec')
if($LASTEXITCODE){throw 'GUI build failed'}
& $Python (Join-Path $PSScriptRoot 'release/check_tk_ui.py') --verify-exe (Join-Path $bundles 'ProdCast-Manager.exe')
if($LASTEXITCODE){throw 'Frozen Tk runtime verification failed'}
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
        throw 'Packaged Tk smoke timed out'
    }
    $smoke.Refresh()
    if($smoke.ExitCode){throw 'Packaged Tk workspace smoke failed'}
}
