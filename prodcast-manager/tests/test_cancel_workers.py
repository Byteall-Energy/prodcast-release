"""Cancellation ownership checks with mocked service retirement, no real SCM changes."""
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell behavior')
@pytest.mark.parametrize('case',['owned','owned-retired','already-clean','foreign-expansion','foreign-operation','foreign-maintenance','old-worker'])
def test_cancel_retires_only_a_worker_claimed_by_this_addition(tmp_path,case):
    source=(Path(__file__).parents[1]/'prodcast_manager/resources/worker.ps1').read_text('utf-8')
    dispatch=source[source.index('function Dispatch {'):source.index('function Dispatch-WorkerRetire {')]
    guard=source[source.index('        $reclaim='):source.index("        if($p.mode -eq 'add-workers'",source.index('        $reclaim='))]
    script=r'''
$ErrorActionPreference='Stop'
$case='CASE'
$p=[pscustomobject]@{action='cancel-worker-addition';mode='cancel-add-workers';operation='addition';role='worker3';worker_expansion=[pscustomobject]@{id='addition';new_workers=@('worker3')}}
$script:state=[pscustomobject]@{worker_expansion='addition';operation='addition';maintenance='addition'}
if($case -eq 'already-clean'){$script:state=$null}
if($case -eq 'foreign-expansion'){$script:state.worker_expansion='another-addition'}
if($case -eq 'foreign-operation'){$script:state.operation='another-operation'}
if($case -eq 'foreign-maintenance'){$script:state.maintenance='another-maintenance'}
if($case -eq 'old-worker'){$p.role='worker1'}
if($case -eq 'owned-retired'){
 $script:state | Add-Member retired $true
 $script:state | Add-Member retirement_operation 'addition'
 $p.worker_expansion | Add-Member reactivated_workers @{worker3=@{operation='old-retirement'}}
}
$script:retired=0
function Dispatch-WorkerRetire {$script:retired++;return @{removed=$true;runtime_preserved=$true}}
function Save-State {}
DISPATCH
$errorText='';$result=$null
try{GUARD;$result=Dispatch}catch{$errorText=$_.Exception.Message}
@{retired=$script:retired;error=$errorText;result=$result}|ConvertTo-Json -Depth 8 -Compress
'''.replace('CASE',case).replace('DISPATCH',dispatch).replace('GUARD',guard)
    path=tmp_path/'cancel-test.ps1';path.write_text(script,encoding='utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    if case in ('owned','owned-retired'):assert value['retired']==1 and not value['error']
    elif case=='already-clean':assert value['retired']==0 and value['result']['already_clean']
    else:assert value['retired']==0 and value['error']
