"""Exercise the real remote configuration writers without running system commands."""
import importlib.util
import json
import sys
import tarfile
import types
from pathlib import Path
import pytest

from prodcast_manager.config import example
from prodcast_manager.engine import RESOURCES
from prodcast_manager.release import Release

@pytest.fixture
def agent(tmp_path,monkeypatch):
    if sys.platform=='win32':monkeypatch.setitem(sys.modules,'fcntl',types.SimpleNamespace())
    spec=importlib.util.spec_from_file_location('test_linux_agent',RESOURCES/'linux.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.ROOT=tmp_path/'opt';m.ROOT.mkdir();m.STATE=tmp_path/'state';m.STATE.mkdir()
    m.OLLAMA_HOME=tmp_path/'ollama-home';(m.OLLAMA_HOME/'models').mkdir(parents=True)
    m.C=example();m.ST={'site':'test','steps':{}}
    keys='PG_ADMIN PW_APP PW_LICENSE PW_AI PW_W01 PW_W02 REDIS_ADMIN REDIS_APP REDIS_W01 REDIS_W02 DJANGO_KEY MODULE_KEY RESOLVE_KEY MEDIA_KEY SIGNING_KEY METRICS_KEY LICENSE_SALT LICENSE_BOOTSTRAP AI_KEY AI_BASIC ADMIN_PASSWORD FERNET_KEY'.split()
    m.S={k:'a'*64 for k in keys}
    m.P={'version':'v0.2','stage':str(RESOURCES),'site':m.C,'tls':{n:'CERT' for n in ['ca.crt','site.crt','site.key','license.crt','license.key','postgres.crt','postgres.key','redis.crt','redis.key']}}
    monkeypatch.setattr(m.os,'chown',lambda *a:None,raising=False)
    return m

def test_db_writer_emits_tls_only_scoped_access(agent,monkeypatch):
    m=agent;commands=[];sql=[]
    def run(args,**kw):
        commands.append(args)
        return '999\n' if 'id' in args else ''
    monkeypatch.setattr(m,'run',run);monkeypatch.setattr(m,'firewall',lambda role:None)
    monkeypatch.setattr(m,'psql',lambda text,db='postgres':sql.append((db,text)) or '')
    monkeypatch.setattr(m,'dc',lambda *a,**kw:'PONG')
    result=m.install_db();b=m.ROOT/'db'
    cfg=json.loads((b/'compose.json').read_text())
    assert cfg['services']['postgres']['network_mode']=='host'
    assert '@sha256:' in cfg['services']['postgres']['image']
    assert 'port 0' in (b/'config/redis.conf').read_text()
    assert '192.0.2.21/32 scram-sha-256' in (b/'config/pg_hba.conf').read_text()
    acl=(b/'config/users.acl').read_text()
    assert 'user default off' in acl
    workerline=next(l for l in acl.splitlines() if l.startswith('user prodcast_worker_01'))
    assert '+@all' not in workerline and '~worker01.*' in workerline
    assert m.S['REDIS_W01'] not in acl
    assert result['redis_tls']
    assert not any('DROP' in t for _,t in sql)


def test_fresh_install_process_cannot_wait_for_debconf_or_mok(agent,monkeypatch):
    # Regression: bootstrap ran in a different SSH process and its env was lost.
    for name in ('DEBIAN_FRONTEND','APT_LISTCHANGES_FRONTEND','NEEDRESTART_MODE'):
        monkeypatch.delenv(name,raising=False)
    calls=[]
    def execute(args,**kwargs):
        calls.append((args,kwargs))
        return types.SimpleNamespace(stdout='ok',stderr='',returncode=0)
    monkeypatch.setattr(agent.subprocess,'run',execute)
    for command in (['apt-get','install','-y','nvidia-driver-595-server'],['ubuntu-drivers','install','--gpgpu']):
        assert agent.run(command)=='ok'
    for command,options in calls:
        assert options['env']['DEBIAN_FRONTEND']=='noninteractive'
        assert options['env']['APT_LISTCHANGES_FRONTEND']=='none'
        assert options['env']['NEEDRESTART_MODE']=='l'
        assert options['input']==''
    assert '--force-confold' in ' '.join(calls[0][0])
    assert 'DEBIAN_FRONTEND' not in agent.os.environ
    agent.run(['docker','exec','-i','postgres','psql'],input='SELECT 1;')
    assert calls[-1][1]['input']=='SELECT 1;'
    monkeypatch.setattr(agent.subprocess,'run',lambda *a,**kw:types.SimpleNamespace(stdout='',stderr='private',returncode=1))
    with pytest.raises(RuntimeError,match='Command failed: apt-get'):agent.run(['apt-get','install','broken'])

def test_public_ollama_runtime_permissions_leave_secrets_untouched(agent,tmp_path,monkeypatch):
    runtime=tmp_path/'ollama'/'0.34.0';(runtime/'bin').mkdir(parents=True)
    binary=runtime/'bin'/'ollama.exe';binary.write_bytes(b'public binary')
    library=runtime/'lib.so';library.write_bytes(b'public library')
    secret=tmp_path/'vault.json';secret.write_text('secret')
    modes={}
    monkeypatch.setattr(Path,'chmod',lambda p,mode:modes.update({p:mode}))
    agent.prepare_ollama_runtime(runtime)
    assert modes[runtime.parent]==modes[runtime]==modes[runtime/'bin']==0o755
    assert modes[library]==0o644
    assert secret not in modes
    assert set(modes)=={runtime.parent,runtime,runtime/'bin',binary,library}

def test_ollama_runtime_rejects_symlink_before_chmod(agent,tmp_path,monkeypatch):
    runtime=tmp_path/'runtime';runtime.mkdir();called=[]
    original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda p:p==runtime or original(p))
    monkeypatch.setattr(Path,'chmod',lambda *a:called.append(a))
    with pytest.raises(RuntimeError,match='symlink'):agent.prepare_ollama_runtime(runtime)
    assert not called


@pytest.mark.parametrize('existing',['absent','home','models'])
def test_ollama_home_recreated_without_recreating_account_or_losing_models(agent,tmp_path,monkeypatch,existing):
    home=tmp_path/'data'/'prodcast-ollama';models=home/'models'
    if existing in ('home','models'):home.mkdir(parents=True)
    if existing=='models':models.mkdir();(models/'cached-model').write_bytes(b'preserve-model')
    ownership=[];modes={};original_chmod=Path.chmod
    monkeypatch.setattr(agent.os,'chown',lambda p,uid,gid:ownership.append((p,uid,gid)))
    def chmod(path,mode):modes[path]=mode;original_chmod(path,mode)
    monkeypatch.setattr(Path,'chmod',chmod)
    assert agent.prepare_ollama_home(home,991,991)==models
    assert home.is_dir() and models.is_dir()
    assert ownership==[(home,991,991),(models,991,991)]
    assert modes=={home:0o750,models:0o750}
    agent.prepare_ollama_home(home,991,991)
    if existing=='models':assert (models/'cached-model').read_bytes()==b'preserve-model'


@pytest.mark.parametrize('target',['home','models'])
def test_ollama_home_rejects_symlink_before_changes(agent,tmp_path,monkeypatch,target):
    home=tmp_path/'home';models=home/'models';calls=[]
    monkeypatch.setattr(Path,'is_symlink',lambda p:p==(home if target=='home' else models))
    monkeypatch.setattr(Path,'mkdir',lambda *a,**kw:calls.append(a))
    monkeypatch.setattr(agent.os,'chown',lambda *a:calls.append(a))
    with pytest.raises(RuntimeError,match='symlink'):agent.prepare_ollama_home(home,991,991)
    assert not calls


def test_removed_ollama_models_invalidates_cached_ai_install(agent,monkeypatch):
    agent.P.update(role='ai',action='install',operation='test')
    agent.ST['step_tls']={'test:install':agent.tls_revision()}
    monkeypatch.setattr(agent,'dc',lambda *a:json.dumps([{'Service':'api','State':'running','Health':''}]))
    monkeypatch.setattr(agent,'request',lambda *a,**kw:{'status':'ready'})
    assert agent.cached_runtime_present()
    (agent.OLLAMA_HOME/'models').rmdir();agent.OLLAMA_HOME.rmdir()
    assert not agent.cached_runtime_present()

def test_readiness_error_identifies_service(agent,monkeypatch):
    from urllib.error import URLError
    def fail(*a,**kw):raise URLError('connection refused')
    monkeypatch.setattr(agent,'request',fail);monkeypatch.setattr(agent.time,'sleep',lambda t:None)
    with pytest.raises(RuntimeError,match='Ollama.*URLError'):
        agent.wait_http('http://127.0.0.1:11434/api/version',service='Ollama')

def test_ai_environment_serializes_mcp_collection_as_json(agent):
    agent.S['AI_HASH']='scrypt$test'
    env=agent.ai_environment('qwen3:4b-instruct')
    # Empty text fails in EnvSettingsSource before the app's field validator.
    assert json.loads(env['MCP_ALLOWED_SERVERS'])==[]
    assert all(isinstance(v,str) and '\n' not in v for v in env.values())
    assert env['APP_ENV']=='production'
    assert env['NOTEBOOK_API_KEY']==agent.S['AI_KEY']
    assert env['MCP_CODE_ENABLED']==env['MCP_ENGINEERING_ENABLED']==env['MCP_TEMPLATES_ENABLED']=='false'
    assert 'sslmode=verify-full' in env['DATABASE_URL']

@pytest.mark.parametrize('role,action,services,expected',[
    ('db','install','',False),('db','install','postgres\n',False),
    ('db','install','postgres\nredis\n',True),('ai','install','',False),
    ('ai','install','api\n',True),('app','configure','api\n',False),
    ('app','configure','api\nlicense-proxy\n',True),('app','migrate','',True),
    ('app','start','',False),('app','beat','',False),('app','beat','celery-beat\n',True),
])
def test_cached_steps_require_running_dependency_containers(agent,monkeypatch,role,action,services,expected):
    agent.P.update(role=role,action=action,operation='test-op')
    agent.ST['step_tls']={'test-op:'+action:agent.tls_revision()}
    monkeypatch.setattr(agent,'dc',lambda *args:json.dumps([{'Service':s,'State':'running','Health':''} for s in services.split()]))
    monkeypatch.setattr(agent,'request',lambda *a,**kw:{'status':'ready'})
    monkeypatch.setattr(agent,'database_ready',lambda:None)
    assert agent.cached_runtime_present() is expected

def test_certificate_change_invalidates_completed_runtime_step(agent,monkeypatch):
    agent.P.update(role='db',action='install',operation='test-op')
    agent.ST['step_tls']={'test-op:install':agent.tls_revision()}
    monkeypatch.setattr(agent,'dc',lambda *args:json.dumps([{'Service':s,'State':'running','Health':''} for s in ('postgres','redis')]))
    monkeypatch.setattr(agent,'database_ready',lambda:None)
    assert agent.cached_runtime_present()
    agent.P['tls']['ca.crt']='updated certificate'
    assert not agent.cached_runtime_present()

@pytest.mark.parametrize('message,ca,retry',[
    ('Missing Authority Key Identifier','private-ca.crt',True),
    ('Missing Subject Key Identifier','private-ca.crt',True),
    ('Hostname mismatch','private-ca.crt',False),
    ('certificate has expired','private-ca.crt',False),
    ('unable to get local issuer certificate','private-ca.crt',False),
    ('Missing Authority Key Identifier',None,False),
])
def test_legacy_tls_retry_preserves_ca_and_hostname_verification(agent,monkeypatch,message,ca,retry):
    import ssl
    from urllib.error import URLError
    context=ssl.create_default_context();context.verify_flags|=ssl.VERIFY_X509_STRICT
    seen=[]
    def create_context(cafile):
        assert cafile=='private-ca.crt'
        return context
    monkeypatch.setattr(agent.ssl,'create_default_context',create_context)
    error=ssl.SSLCertVerificationError(1,message);error.verify_message=message
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"status":"ready"}'
    def urlopen(req,context,timeout):
        seen.append(context.verify_flags if context else None)
        if len(seen)==1:raise URLError(error)
        assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
        assert not context.verify_flags & ssl.VERIFY_X509_STRICT
        return Response()
    monkeypatch.setattr(agent.urllib.request,'urlopen',urlopen)
    if retry:
        assert agent.request('https://ai:8443/readyz',ca=ca)=={'status':'ready'}
        assert len(seen)==2
    else:
        with pytest.raises(URLError):agent.request('https://ai:8443/readyz',ca=ca)
        assert len(seen)==1

@pytest.mark.parametrize('customer',[False,True])
def test_app_writer_uses_release_runtime_and_separate_license_token(agent,monkeypatch,customer):
    root=Path(__file__).parents[2]/'deliverables/releases/v0.2'
    if not root.exists():pytest.skip('external release fixture absent')
    m=agent;r=Release(root,root);m.P['images']=r.images
    if customer:m.P['app_tls']={'url':'https://prodcast.customer.local','certificate':'CUSTOM CERT','private_key':'CUSTOM KEY','ca':'CUSTOM CA'}
    def load():
        for part in ('app','license'):
            folder=m.target()/part;folder.mkdir(parents=True)
            with tarfile.open(r.files[part+'-deployment']) as t:t.extractall(folder,filter='data')
            (folder/'packages.site.env').write_text('')
    monkeypatch.setattr(m,'load_packages',load)
    commands=[]
    def run(args,**kw):
        commands.append(args)
        if args[:3]==['docker','network','ls']:return ''
        if args[:3]==['ip','-j','-4']:return '[]'
        if args[-1]=='/etc/nginx/nginx.conf.template':return gateway_fixture()
        return 'SYSTEM ROOTS\n'
    monkeypatch.setattr(m,'run',run);monkeypatch.setattr(m,'dc',lambda *a,**kw:'')
    def request(url,data=None,headers=None,**kw):
        if url.endswith('/admin/tokens'):return {'token':'client-token-distinct','meta':{'id':1}}
        return {'tenant_id':m.C['tenant_id']}
    monkeypatch.setattr(m,'request',request)
    m.configure_app();b=m.target()/'app/runtime'
    env=(b/'runtime.env').read_text()
    assert 'LICENSE_SERVICE_TOKEN=client-token-distinct' in env
    assert 'POSTGRES_HOST=192.0.2.128' in env
    assert 'PRODCAST_AI_BASE_URL=https://192.0.2.76:8443' in env
    assert 'MODEL_STORAGE_ENABLED=false' in (b/'site.env').read_text()
    override=json.loads((b/'site.compose.json').read_text())
    assert override['volumes']['media_data']['name']=='prodcast-managed-media'
    license_override=json.loads((m.target()/'license/runtime/site.compose.json').read_text())
    assert 'ports' not in license_override['services']['license-proxy']
    assert m.ST['company'] and not m.ST['token_request_pending']
    if customer:
        assert 'PRODCAST_PUBLIC_URL=https://prodcast.customer.local' in env
        assert (b/'certs/customer.key').read_text()=='CUSTOM KEY'
        assert (b/'certs/site.key').read_text()=='CERT'
        assert not (m.target()/'license/runtime/certs/customer.key').exists()

def test_firewall_preserves_other_chains_and_ssh(agent,monkeypatch):
    m=agent;written={}
    original=m.write
    def write(path,text,mode=0o600):
        if str(path).startswith('/etc/'):written[str(path)]=text
        else:original(path,text,mode)
    monkeypatch.setattr(m,'write',write);monkeypatch.setattr(m,'run',lambda *a,**kw:'')
    m.firewall('db');script=(m.STATE/'firewall.sh').read_text()
    assert 'iptables -F PCMAN-DB' in script
    assert 'iptables -F\n' not in script and '--dport 22' not in script
    assert '--dports 5432,6380' in script and '192.0.2.76/32 -p tcp --dport 5432' in script

def test_managed_host_rejects_new_vault_even_for_same_topology(agent,monkeypatch):
    m=agent
    m.P.update(role='app',topology='same-topology',installation_id='new-vault',mode='update')
    (m.STATE/'state.json').write_text(json.dumps({'site':m.C['site_id'],'role':'app','topology':'same-topology','installation_id':'original-vault'}))
    monkeypatch.setattr(m.os,'geteuid',lambda:0,raising=False)
    monkeypatch.setattr(m.platform,'machine',lambda:'x86_64')
    monkeypatch.setattr(m.platform,'freedesktop_os_release',lambda:{'ID':'ubuntu','VERSION_ID':'24.04'})
    with pytest.raises(RuntimeError,match='another vault'):m.preflight()


@pytest.mark.parametrize('distro,version,codename',[
    ('ubuntu','22.04','jammy'),('ubuntu','24.04','noble'),('ubuntu','26.04','resolute'),
    ('debian','12','bookworm'),('debian','13','trixie'),('rhel','9','9'),('rhel','9.8','9')])
def test_supported_linux_release_mapping(agent,distro,version,codename):
    assert agent.supported_os({'ID':distro,'VERSION_ID':version})==(distro,codename)


@pytest.mark.parametrize('distro,version',[('ubuntu','20.04'),('debian','11'),('linuxmint','22'),('ubuntu','99.04'),('rhel','8.10'),('rhel','10'),('rocky','9')])
def test_unsupported_linux_release_is_not_silently_accepted(agent,distro,version):
    with pytest.raises(RuntimeError,match='detected'):
        agent.supported_os({'ID':distro,'VERSION_ID':version})


def test_deployment_extraction_without_python312_filter(agent,tmp_path):
    import io
    m=agent;archive=tmp_path/'deployment.tar.gz'
    with tarfile.open(archive,'w:gz') as t:
        member=tarfile.TarInfo('runtime/start.sh');data=b'#!/bin/sh\nexit 0\n'
        member.size=len(data);member.mode=0o755;t.addfile(member,io.BytesIO(data))
    m.P['stage']=str(tmp_path);m.P['files']={'ai-deployment':{'name':archive.name,'sha256':m.digest(archive)}}
    m.load_packages()
    assert (m.target()/'ai/runtime/start.sh').read_bytes()==data


def test_cpu_model_is_accepted_and_reported(agent):
    assert agent.model_compute([{'name':'qwen3:4b-instruct','size_vram':0}]) is False
    assert agent.model_compute([{'size_vram':1024}]) is True


@pytest.mark.parametrize('legacy_flag',[True,False])
def test_driver_installation_removed_even_for_legacy_profile(agent,monkeypatch,legacy_flag):
    agent.C['install_gpu_driver']=legacy_flag
    calls=[]
    def stop():
        calls.append('load_packages')
        raise RuntimeError('stop before downloads')
    monkeypatch.setattr(agent,'load_packages',stop)
    monkeypatch.setattr(agent,'run',lambda *a,**kw:pytest.fail('No driver commands should run'))
    with pytest.raises(RuntimeError,match='stop before downloads'):agent.install_ai()
    assert calls==['load_packages']
    assert agent.model_compute([]) is False
    assert agent.model_compute([{'size_vram':1024}]) is True


def gateway_fixture():
    path=Path(__file__).parents[2]/'sources/prodcast-app/release/gateway.nginx.conf'
    if not path.exists():pytest.skip('release source template unavailable')
    return path.read_text('utf-8')


def test_customer_sni_preserves_original_gateway_security_and_internal_cert(agent):
    template=gateway_fixture();out=agent.customer_gateway(template,'prodcast.customer.local')
    assert out.count('listen 443 ssl;')==2 and out.count('listen 8443 ssl;')==1
    assert out.count('ssl_certificate /etc/nginx/certs/site.crt;')==2
    assert out.count('ssl_certificate /etc/nginx/certs/customer.crt;')==1
    assert 'server_name prodcast.customer.local;' in out
    assert out.count('location = /readyz')==2
    assert out.count('proxy_ssl_verify on;')==2
    assert 'if ($host != "prodcast.customer.local")' in out
    assert 'if ($host != "${prodcast_public_host}")' in out
    assert out.count('Content-Security-Policy "')==2*template.count('Content-Security-Policy "')
    assert out[out.index('  # Loopback/admin-channel listener'):]==template[template.index('  # Loopback/admin-channel listener'):]
    with pytest.raises(RuntimeError):agent.customer_gateway('different release','prodcast.customer.local')
    with pytest.raises(RuntimeError):agent.customer_gateway(template,'name; malicious')


def prepare_app_tls(agent):
    m=agent;m.P.update(role='app',operation='a'*32,mode='app-tls',app_tls={
        'url':'https://prodcast.customer.local','certificate':'CUSTOM CERT','private_key':'CUSTOM KEY','ca':'CUSTOM CA'})
    m.ST.update(version='v0.2');b=m.target()/'app/runtime';b.mkdir(parents=True)
    env='PRODCAST_PUBLIC_URL=https://192.0.2.23\nDJANGO_ALLOWED_HOSTS=192.0.2.23\nCORS_ALLOWED_ORIGINS=https://192.0.2.23\nCSRF_TRUSTED_ORIGINS=https://192.0.2.23\nUNCHANGED_SECRET=preserved\n'
    (b/'runtime.env').write_text(env)
    (b/'site.compose.json').write_text(json.dumps({'volumes':{'media_data':{'name':'keep'}}}))
    return b


@pytest.mark.parametrize('failure',[None,'syntax','readiness'])
def test_customer_apply_transaction_and_rollback(agent,monkeypatch,failure):
    m=agent;b=prepare_app_tls(m);original={p.name:p.read_bytes() for p in b.iterdir()};calls=[]
    def run(args,**kw):
        if 'cat' in args:return gateway_fixture()
        if '{{.Image}}' in args:return 'sha256:image'
        if '{{json .Config.Cmd}}' in args:return json.dumps(['/bin/sh','-c',"envsubst; nginx -g 'daemon off;'"])
        raise AssertionError(args)
    def dc(*args,**kw):
        calls.append(args)
        if args[1:]==('ps','-q','gateway'):return 'a'*64
        if args[1]=='run' and failure=='syntax':raise RuntimeError('bad nginx configuration')
        return ''
    def verify(base):
        if failure=='readiness':raise RuntimeError('HTTPS failed')
        return {'https':True}
    monkeypatch.setattr(m,'run',run);monkeypatch.setattr(m,'dc',dc)
    monkeypatch.setattr(m,'verify_customer_https',verify);monkeypatch.setattr(m,'request',lambda *a,**kw:{'status':'ok'})
    if failure:
        with pytest.raises(RuntimeError,match='restored'):m.apply_app_tls()
        for name,data in original.items():assert (b/name).read_bytes()==data
        assert not (b/'certs/customer.key').exists()
        assert not json.loads((m.STATE/'app-tls-transaction.json').read_text())['complete']
        # Repeat the same operation after correcting the cause; original backup stays intact.
        failure=None;assert m.apply_app_tls()['https']
    else:
        assert m.apply_app_tls()['https']
    env=(b/'runtime.env').read_text()
    assert 'PRODCAST_PUBLIC_URL=https://prodcast.customer.local' in env
    assert 'UNCHANGED_SECRET=preserved' in env
    assert 'CSRF_TRUSTED_ORIGINS=https://prodcast.customer.local,https://192.0.2.23' in env
    override=json.loads((b/'site.compose.json').read_text())
    assert override['volumes']['media_data']['name']=='keep'
    assert len(override['services']['gateway']['volumes'])==1
    assert (b/'certs/customer.key').read_text()=='CUSTOM KEY'
    assert json.loads((m.STATE/'app-tls-transaction.json').read_text())['complete']
    syntax=next(i for i,c in enumerate(calls) if c[1]=='run')
    restart=next(i for i,c in enumerate(calls) if c[1]=='up')
    assert syntax<restart
    assert not any(c[0]!='app' or 'migrate' in c for c in calls)
    # Reapplication must not accumulate template mounts or overwrite the original rollback snapshot.
    m.apply_app_tls()
    assert len(json.loads((b/'site.compose.json').read_text())['services']['gateway']['volumes'])==1


def test_customer_renewal_invalidates_configure_cache(agent,monkeypatch):
    agent.P.update(role='app',action='configure',operation='op',app_tls={'certificate':'old','url':'https://prodcast'})
    agent.ST['step_tls']={'op:configure':agent.tls_revision()}
    monkeypatch.setattr(agent,'dc',lambda *a:json.dumps([{'Service':s,'State':'running','Health':''} for s in ('api','license-proxy')]))
    assert agent.cached_runtime_present()
    agent.P['app_tls']['certificate']='renewed'
    assert not agent.cached_runtime_present()


def test_directory_change_invalidates_migration_cache(agent):
    agent.P.update(role='app',action='migrate',operation='test',directory={'mode':'disabled'})
    agent.ST['step_tls']={'test:migrate':agent.tls_revision()}
    assert agent.cached_runtime_present()
    agent.P['directory']={'mode':'ldaps','server_uri':'ldaps://dc.example.test'}
    assert not agent.cached_runtime_present()


def test_migration_applies_directory_after_database_migration(agent,monkeypatch):
    calls=[];agent.ST['initialized']=True;agent.P['directory']={'mode':'disabled'}
    monkeypatch.setattr(agent,'dc',lambda *a,**kw:calls.append(('dc',a)))
    monkeypatch.setattr(agent,'app_python',lambda code:calls.append(('config',code)))
    agent.migrate()
    assert calls[0][1][-1]=='migrate'
    code=calls[1][1];compile(code,'apply-directory','exec')
    assert 'DirectoryConfiguration' in code and 'full_clean()' in code and "'mode': 'disabled'" in code


@pytest.mark.parametrize('initialized',[False,True])
def test_migration_syncs_petex_on_install_update_and_retry(agent,monkeypatch,initialized):
    calls=[];agent.ST['initialized']=initialized
    monkeypatch.setattr(agent,'dc',lambda *a,**kw:calls.append(a))
    for _ in range(2):
        calls.clear()
        assert agent.migrate()=={'migrations':True}
        assert calls[0][-1]=='migrate'
        sync=[call for call in calls if 'sync_integration_plugins' in call]
        assert len(sync)==1
        assert sync[0]==('app','run','--rm','--no-deps','--pull','never',
                         '--entrypoint','python','admin-service','manage.pyc',
                         'sync_integration_plugins','--package','prodcast-petex-plugins')
        assert any('init_data' in call for call in calls) is (not initialized)
        assert any('load_units' in call for call in calls) is (not initialized)
        assert agent.ST['initialized'] is True
        initialized=True


@pytest.mark.parametrize('initialized',[False,True])
@pytest.mark.parametrize('failed_command',['migrate','sync_integration_plugins'])
def test_migration_failure_stops_install_or_update(agent,monkeypatch,initialized,failed_command):
    calls=[];agent.ST['initialized']=initialized
    def dc(*args,**kwargs):
        calls.append(args)
        if failed_command in args:
            raise RuntimeError('migration step failed')
    monkeypatch.setattr(agent,'dc',dc)
    with pytest.raises(RuntimeError,match='migration step failed'):
        agent.migrate()
    assert failed_command in calls[-1]
    assert agent.ST['initialized'] is initialized
    if failed_command=='migrate':
        assert not any('sync_integration_plugins' in call for call in calls)
