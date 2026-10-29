"""Recovery regressions: real adapter functions with isolated system boundaries."""
import json
import subprocess
from pathlib import Path
import pytest
from test_remote_configuration import agent
from test_manager import engine, FakeRemote
from prodcast_manager.engine import RESOURCES


def database_data(m):
    pg=m.ROOT/'db/data/postgres/pgdata'
    (pg/'global').mkdir(parents=True);(pg/'base').mkdir()
    (pg/'PG_VERSION').write_text('18');(pg/'global/pg_control').write_bytes(b'preserve-pg-control')
    redis=m.ROOT/'db/data/redis';redis.mkdir();(redis/'dump.rdb').write_bytes(b'preserve-redis')
    return pg,redis


@pytest.mark.parametrize('state,health',[('exited',''),('restarting',''),('running','unhealthy'),('running','starting'),('created','')])
def test_failed_container_invalidates_completed_install(agent,monkeypatch,state,health):
    agent.P.update(role='db',action='install',operation='op')
    agent.ST['step_tls']={'op:install':agent.tls_revision()}
    rows=[dict(Service='postgres',State=state,Health=health),dict(Service='redis',State='running',Health='')]
    monkeypatch.setattr(agent,'dc',lambda *a:json.dumps(rows))
    assert not agent.cached_runtime_present()


@pytest.mark.parametrize('missing',['all','PG_VERSION','global/pg_control','base','redis'])
def test_database_loss_stops_before_any_docker_or_filesystem_mutation(agent,monkeypatch,missing):
    agent.ST['version']='v0.3'
    if missing!='all':
        pg,redis=database_data(agent)
        if missing=='redis':(redis/'dump.rdb').unlink();redis.rmdir()
        elif missing=='base':(pg/'base').rmdir()
        else:(pg/missing).unlink()
    monkeypatch.setattr(agent,'run',lambda *a,**kw:pytest.fail('must not execute commands'))
    with pytest.raises(RuntimeError,match='data is missing'):agent.install_db()


@pytest.mark.parametrize('format_array',[True,False])
@pytest.mark.parametrize('broken',['missing','unhealthy','stopped','healthy'])
def test_repair_recreates_only_failed_containers_and_preserves_data(agent,monkeypatch,format_array,broken):
    agent.P.update(role='db',version='v0.3',manifest='digest')
    agent.ST.update(version='v0.3',manifest='digest')
    pg,redis=database_data(agent)
    before={str(p):p.read_bytes() for p in (pg/'PG_VERSION',pg/'global/pg_control',redis/'dump.rdb')}
    rows=[dict(Service='redis',State='running',Health='')]
    if broken!='missing':rows.append(dict(Service='postgres',State='exited' if broken=='stopped' else 'running',Health='unhealthy' if broken=='unhealthy' else 'healthy'))
    calls=[]
    def dc(role,*args,**kw):
        calls.append(args)
        if args[0]=='ps':return json.dumps(rows) if format_array else '\n'.join(map(json.dumps,rows))
        if args[0]=='up':
            assert args[-1]=='postgres' and 'redis' not in args
            rows[:]=[dict(Service=s,State='running',Health='healthy') for s in ('postgres','redis')]
        return 'PONG'
    monkeypatch.setattr(agent,'dc',dc);monkeypatch.setattr(agent,'psql',lambda *a:'1')
    result=agent.repair()
    assert result['recreated']==([] if broken=='healthy' else ['postgres'])
    assert {p:Path(p).read_bytes() for p in before}==before
    assert not any('down' in c or 'rm' in c or '-v' in c for c in calls)


def test_app_missing_media_volume_stops_recovery(agent,monkeypatch):
    agent.P.update(role='app',version='v0.3',manifest='digest',external_activation=True)
    agent.ST.update(version='v0.3',manifest='digest')
    monkeypatch.setattr(agent,'run',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('volume missing')))
    monkeypatch.setattr(agent,'dc',lambda *a,**kw:pytest.fail('must not create empty media volume'))
    with pytest.raises(RuntimeError,match='volume missing'):agent.repair()


def test_bootstrap_retries_partial_docker_install_after_rights_fixed(agent,monkeypatch):
    calls=[];compose_missing=True
    monkeypatch.setattr(agent,'supported_os',lambda:('ubuntu','jammy'))
    monkeypatch.setattr(agent.shutil,'which',lambda name:'/usr/bin/'+name)
    monkeypatch.setattr(agent,'write',lambda *a:None)
    monkeypatch.setattr(agent.Path,'mkdir',lambda *a,**kw:None)
    class Download:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'key'
    monkeypatch.setattr(agent.urllib.request,'urlopen',lambda *a,**kw:Download())
    def run(args,**kw):
        nonlocal compose_missing
        calls.append(args)
        if args[:3]==['docker','compose','version']:
            if compose_missing:raise RuntimeError('plugin absent')
            return '2.35.0'
        if args[0]=='gpg':return '9DC858229FC7DD38854AE2D88D81803C0EBFCD88'
        if 'docker-compose-plugin' in args:compose_missing=False
        return ''
    monkeypatch.setattr(agent,'run',run)
    assert agent.bootstrap()=={'compose':'2.35.0'}
    assert any('docker-compose-plugin' in c for c in calls)
    agent.P.update(role='db',action='bootstrap');assert not agent.cached_runtime_present()


@pytest.mark.parametrize('failure',[('app','preflight'),('db','bootstrap'),('app','bootstrap'),('worker1','install')])
def test_install_retry_after_permissions_failure_keeps_operation(tmp_path,failure):
    e=engine(tmp_path);e.c['install_ai']=False
    FakeRemote.installed=False;FakeRemote.fail=failure
    with pytest.raises(RuntimeError):e.run('install')
    operation=json.loads((tmp_path/'journal.json').read_text())['operation']
    FakeRemote.fail=None;e.remotes={};e.run('install')
    assert json.loads((tmp_path/'journal.json').read_text())['status']=='complete'
    assert all(op==operation for _,_,op in FakeRemote.history)


def test_repair_same_release_and_retry_without_migration_or_drain(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False
    e.vault.initialize(e.c)
    class Remote(FakeRemote):
        fail=('app','repair')
        def action(self,p,action,resources):
            result=super().action(p,action,resources)
            if action=='preflight':result.update(version=p['version'],manifest=p['manifest'])
            return result
    e.factory=Remote
    with pytest.raises(RuntimeError):e.run('repair')
    op=json.loads((tmp_path/'journal.json').read_text())['operation']
    Remote.fail=None;e.remotes={};e.run('repair')
    assert all(operation==op for _,_,operation in Remote.history)
    actions=[(role,action) for role,action,_ in Remote.history]
    assert actions.index(('db','repair'))<actions.index(('app','repair'))
    assert not any(action in ('migrate','drain','pause','stop','install') for _,action in actions)
    assert json.loads((tmp_path/'journal.json').read_text())['status']=='complete'


def test_repair_refuses_different_release_before_mutation(tmp_path):
    e=engine(tmp_path);e.c['install_ai']=False
    e.vault.initialize(e.c)
    with pytest.raises(RuntimeError):e.run('repair')
    assert not any(a=='claim' for _,a,_ in FakeRemote.history)


def test_existing_db_container_is_started_before_database_queries(agent,monkeypatch):
    agent.ST['version']='v0.3';database_data(agent)
    agent.P['external_activation']=True
    started=False;queries=[]
    monkeypatch.setattr(agent,'run',lambda args,**kw:'999' if 'id' in args else '')
    monkeypatch.setattr(agent,'firewall',lambda role:None)
    def dc(role,*args,**kw):
        nonlocal started
        if args[0]=='up':started=True
        return 'PONG'
    def psql(sql,*args):
        assert started,'PostgreSQL container must exist before querying it'
        queries.append(sql);return ''
    monkeypatch.setattr(agent,'dc',dc);monkeypatch.setattr(agent,'psql',psql)
    agent.install_db()
    assert agent.ST['database_initialized']
    assert not any('CREATE DATABASE license_db' in q for q in queries)


def test_redis_process_running_but_unresponsive_does_not_keep_cached_success(agent,monkeypatch):
    agent.P.update(role='db',action='install',operation='op')
    agent.ST['step_tls']={'op:install':agent.tls_revision()}
    monkeypatch.setattr(agent,'healthy_services',lambda role:{'postgres','redis'})
    monkeypatch.setattr(agent,'psql',lambda *a:'1')
    monkeypatch.setattr(agent,'dc',lambda *a,**kw:'NOAUTH')
    assert not agent.cached_runtime_present()


def test_actual_worker_recovery_functions_without_system_mutations(tmp_path):
    # Parse the full shipped script, execute only its recovery functions with fake
    # service/filesystem boundaries. No service, account or directory is changed.
    script=r'''
param([string]$Source)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Test-WorkerRuntime','Test-WorkerQueues','Repair-Worker')){
    $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$service='ProdCastWorker01';$script:state=[pscustomobject]@{install_root='C:\ProdCast\Managed\owned'}
$executionQueues=@('workflows','workflows_data','scenarios')
function Get-Content {param([switch]$Raw,$LiteralPath)
 if($LiteralPath.EndsWith('site.json')){return '{}'}
 return '{"queues":["workflows","workflows_data","scenarios"]}'
}
$script:calls=0;$script:missing=$false
function Test-Path {param($LiteralPath,$PathType) return !$script:missing}
function Get-CimInstance {param($ClassName,$Filter) return $script:svc}
function Install-Worker {$script:calls++;return @{restored=$true}}
$script:svc=[pscustomobject]@{State='Running';PathName='C:\ProdCast\Managed\owned\worker.exe'}
if(!(Test-WorkerRuntime)){throw 'Healthy service should be retained'}
$r=Repair-Worker;if($script:calls -ne 0){throw 'Healthy service was reinstalled'}
$script:missing=$true
if(Test-WorkerRuntime){throw 'Deleted payload must invalidate success'}
try{Repair-Worker;throw 'Expected refusal'}catch{if($_.Exception.Message -notmatch 'still running'){throw}}
$script:svc.State='Stopped';$r=Repair-Worker
if($script:calls -ne 1){throw 'Stopped broken service was not repaired'}
$script:svc=$null;$r=Repair-Worker
if($script:calls -ne 2){throw 'Missing service was not repaired'}
$script:svc=[pscustomobject]@{State='Stopped';PathName='C:\foreign\worker.exe'}
try{Repair-Worker;throw 'Expected refusal'}catch{if($_.Exception.Message -notmatch 'unowned'){throw}}
'OK'
'''
    path=tmp_path/'worker-recovery.ps1';path.write_text(script,'utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path),'-Source',str(RESOURCES/'worker.ps1')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'OK' in result.stdout
