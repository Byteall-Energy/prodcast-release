"""Append Worker access to a running managed DB, without recreating containers."""
import hashlib
import ipaddress
import json
import os
import re
import stat
from pathlib import Path


def dispatch(a):
    p=a.P; st=a.ST; spec=p['worker_expansion']; operation=p['operation']
    if p['role']!='db' or p['mode']!='add-workers' or spec['id']!=operation:
        raise RuntimeError('Invalid Worker expansion request')
    if st.get('maintenance') not in (None,'',operation):raise RuntimeError('Another operation owns the DB')
    if st.get('operation') not in (None,'',operation):raise RuntimeError('Another deployment owns the DB')
    if not re.fullmatch('[a-f0-9]{32}',operation):raise RuntimeError('Invalid operation ID')
    if st.get('version')!=p['version'] or st.get('manifest')!=p['manifest']:
        raise RuntimeError('Worker expansion DB identity differs from its bound component deployment record')
    new=spec['new_workers']; previous=spec['previous_workers']
    if (not new or len(set(new))!=len(new) or any(r in previous or not re.fullmatch(r'worker([1-9]|1[0-6])',r) for r in new)):
        raise RuntimeError('New Workers must use distinct unused IDs from 1 to 16')
    if len(previous)+len(new)>16:raise RuntimeError('Too many Workers')
    if set(a.worker_roles())!=set(previous)|set(new):raise RuntimeError('Worker expansion request does not match the configured roster')
    for role in new:ipaddress.IPv4Address(a.C['hosts'][role]['address'])
    transaction=a.STATE/'workers-transaction.json'
    receipt=json.loads(transaction.read_text()) if transaction.exists() else {}
    if receipt and not receipt.get('complete') and receipt.get('operation')!=operation:
        raise RuntimeError('Resume the existing Worker expansion')
    if receipt.get('operation')==operation and receipt.get('target')!=spec['target']:
        raise RuntimeError('Worker expansion target changed')
    if p['action']=='workers-cancel':return cancel(a,receipt,transaction)
    if receipt.get('cancelling') or receipt.get('cancelled'):
        raise RuntimeError('Worker expansion cancellation must be completed before installing Workers')
    reactivated=spec.get('reactivated_workers',{})
    if any(role not in new for role in reactivated):raise RuntimeError('Invalid reactivated Worker roster')
    if receipt.get('operation')==operation:
        if receipt.get('reactivated_workers',{})!=reactivated:raise RuntimeError('Worker reactivation receipts changed')
    else:
        retired_path=a.STATE/'worker-retirement.json'
        last_retired=json.loads(retired_path.read_text()) if retired_path.exists() else {}
        for role,proof in reactivated.items():
            saved=st.get('retired_workers',{}).get(role,{})
            legacy=(last_retired.get('operation')==proof.get('operation') and last_retired.get('complete')
                    and last_retired.get('revoked') and last_retired.get('target')==previous)
            if not proof.get('operation') or not (saved.get('operation')==proof['operation'] or legacy):
                raise RuntimeError('Worker reactivation requires the original DB retirement receipt')
    if p['action']=='workers-commit':
        if receipt.get('operation')!=operation or not receipt.get('prepared'):
            raise RuntimeError('Worker expansion is not prepared')
        receipt['complete']=True;a.write(transaction,json.dumps(receipt))
        for role in new:st.get('retired_workers',{}).pop(role,None)
        st['maintenance']='';st['worker_hosts']={r:a.C['hosts'][r]['address'] for r in a.worker_roles()};a.save()
        return {'complete':True}
    if p['action']!='workers-prepare':raise RuntimeError('Unknown Worker expansion action')
    a.require_database_data();a.database_ready()
    folder=a.ROOT/'db/config';hba=folder/'pg_hba.conf';acl=folder/'users.acl'
    names=['prodcast_worker_'+f'{int(r[6:]):02}' for r in new]
    if receipt.get('operation')!=operation:
        # Refuse to take over pre-existing accounts, even if their names match.
        for role,name in zip(new,names):
            if a.psql("SELECT 1 FROM pg_roles WHERE rolname='"+name+"';").strip():
                if role not in reactivated:raise RuntimeError('A proposed Worker database account already exists')
                safe=a.psql("SELECT CASE WHEN NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole THEN 'safe' ELSE 'unsafe' END FROM pg_roles WHERE rolname='"+name+"';").strip()
                if safe!='safe':raise RuntimeError('Retired Worker account must remain disabled and least-privilege')
            if re.search(r'^user '+name+r'_scheduler\s',acl.read_text(),re.M):
                raise RuntimeError('A proposed Worker Redis account already exists')
        snapshot=a.STATE/'worker-backups'/operation
        a.write(snapshot/'pg_hba.conf',hba.read_text())
        a.write(snapshot/'users.acl',acl.read_text())
        receipt={'operation':operation,'target':spec['target'],'complete':False,'reactivated_workers':reactivated}
        a.write(transaction,json.dumps(receipt))
    st['maintenance']=operation;a.save()
    # Writes remain idempotent across disconnects. Never rotate an old account.
    for role,name in zip(new,names):
        number=f'{int(role[6:]):02}';password=a.S['PW_W'+number]
        if not re.fullmatch('[a-f0-9]{64}',password):raise RuntimeError('Invalid generated Worker password')
        if not a.psql("SELECT 1 FROM pg_roles WHERE rolname='"+name+"';").strip():
            a.psql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;")
        elif role in reactivated:
            a.psql(f"ALTER ROLE {name} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;")
    a.psql('GRANT CONNECT ON DATABASE prodcast2 TO '+','.join(names)+';')
    grants=(Path(p['stage'])/'worker-grants.sql').read_text()
    a.psql(re.sub(r'prodcast_worker_01,\s*prodcast_worker_02',','.join(names),grants),'prodcast2')
    hba_text=hba.read_text();reject='host all all 0.0.0.0/0 reject\n'
    if hba_text.count(reject)!=1:raise RuntimeError('Unexpected managed PostgreSQL access configuration')
    for role,name in zip(new,names):
        line=f"hostssl prodcast2 {name} {a.C['hosts'][role]['address']}/32 scram-sha-256\n"
        if line not in hba_text:hba_text=hba_text.replace(reject,line+reject)
    replace(a,hba,hba_text)
    if a.psql('SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;').strip()!='0':
        raise RuntimeError('PostgreSQL access configuration validation failed')
    if a.psql('SELECT pg_reload_conf();').strip()!='t':raise RuntimeError('PostgreSQL reload failed')
    # Reuse the existing least-privilege Worker ACL, changing only name/password/queue.
    text=acl.read_text()
    templates=[(line,re.match(r'^user prodcast_worker_([0-9]{2})_scheduler ',line)) for line in text.splitlines()]
    template,match=next(((line,match) for line,match in templates if match and 'worker'+str(int(match[1])) in previous),(None,None))
    if not template or template.count('#')!=1:raise RuntimeError('Unexpected managed Redis Worker ACL')
    for role,name in zip(new,names):
        number=f'{int(role[6:]):02}';prefix='user '+name+'_scheduler '
        line=template.replace('prodcast_worker_'+match[1]+'_scheduler',name+'_scheduler').replace('~worker'+match[1]+'.*','~worker'+number+'.*')
        line=re.sub(r'#[a-f0-9]{64}', '#'+hashlib.sha256(a.S['REDIS_W'+number].encode()).hexdigest(),line)
        existing=[x for x in text.splitlines() if x.startswith(prefix)]
        if existing and existing!=[line]:raise RuntimeError('Worker Redis account differs from expansion plan')
        if not existing:text=text.rstrip('\n')+'\n'+line+'\n'
    replace(a,acl,text)
    result=a.dc('db','exec','-T','redis','sh','-c','read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --tls --cacert /etc/prodcast-certs/ca.crt -h 127.0.0.1 -p 6380 --user db_admin ACL LOAD',input=a.S['REDIS_ADMIN']+'\n')
    if result.strip()!='OK':raise RuntimeError('Redis ACL reload failed')
    if a.firewalld_active():a.firewalld_rules('db')
    else:
        path=a.STATE/'firewall.sh';script=path.read_text();marker='iptables -A PCMAN-DB -j DROP\n'
        if script.count(marker)!=1:raise RuntimeError('Unexpected managed firewall configuration')
        for role in new:
            address=a.C['hosts'][role]['address']+'/32'
            line=f'iptables -A PCMAN-DB -s {address} -j ACCEPT\n'
            if line not in script:script=script.replace(marker,line+marker)
        a.write(path,script,0o700)
        for role in new:
            rule=['PCMAN-DB','-s',a.C['hosts'][role]['address']+'/32','-j','ACCEPT']
            try:a.run(['iptables','-C',*rule])
            except RuntimeError:a.run(['iptables','-I',*rule])
    a.database_ready();receipt['prepared']=True;a.write(transaction,json.dumps(receipt))
    return {'prepared':True,'new_workers':new,'containers_recreated':False}


def replace(a,path,text):
    """Preserve ownership and SELinux label of bind-mounted DB configuration."""
    info=path.stat()
    label=os.getxattr(path,'security.selinux') if a.selinux_enabled() else None
    a.write(path,text,stat.S_IMODE(info.st_mode));os.chown(path,info.st_uid,info.st_gid)
    if label is not None:os.setxattr(path,'security.selinux',label)


def cancel(a,receipt,path):
    """Revoke only access created/reactivated by this exact expansion."""
    spec=a.P['worker_expansion'];operation=a.P['operation'];new=spec['new_workers'];previous=spec['previous_workers']
    if receipt.get('operation')!=operation:
        if a.ST.get('maintenance')==operation:raise RuntimeError('Owned Worker expansion receipt is missing; cannot infer cancellation scope')
        return {'cancelled':True,'transaction_found':False}
    if receipt.get('cancelled'):return {'cancelled':True,'transaction_found':True}
    roster=a.ST.get('worker_hosts',previous)
    target={r:a.C['hosts'][r]['address'] for r in previous.keys()|set(new)}
    if roster not in (previous,target):raise RuntimeError('DB Worker membership changed outside this expansion')
    receipt['cancelling']=True;a.write(path,json.dumps(receipt));a.ST['maintenance']=operation;a.save()
    names=['prodcast_worker_'+f'{int(r[6:]):02}' for r in new]
    for name in names:
        if a.psql("SELECT 1 FROM pg_roles WHERE rolname='"+name+"';").strip():
            a.psql('ALTER ROLE '+name+' NOLOGIN;')
            a.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='"+name+"';")
    folder=a.ROOT/'db/config';hba=folder/'pg_hba.conf';acl=folder/'users.acl'
    replace(a,hba,''.join(line for line in hba.read_text().splitlines(True) if not set(line.split()).intersection(names)))
    replace(a,acl,''.join(line for line in acl.read_text().splitlines(True) if not any(line.startswith('user '+name+'_scheduler ') for name in names)))
    if a.psql('SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;').strip()!='0':raise RuntimeError('PostgreSQL cancellation access configuration is invalid')
    if a.psql('SELECT pg_reload_conf();').strip()!='t':raise RuntimeError('PostgreSQL reload failed during cancellation')
    def redis(*args):
        return a.dc('db','exec','-T','redis','sh','-c',
            'read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --tls --cacert /etc/prodcast-certs/ca.crt -h 127.0.0.1 -p 6380 --user db_admin "$@"',
            'redis-admin',*args,input=a.S['REDIS_ADMIN']+'\n')
    if redis('ACL','LOAD').strip()!='OK':raise RuntimeError('Redis cancellation ACL reload failed')
    for name in names:redis('CLIENT','KILL','USER',name+'_scheduler','SKIPME','yes')
    if a.firewalld_active():
        old_hosts=a.C['hosts'];a.C['hosts']={r:h for r,h in old_hosts.items() if r not in new}
        try:a.firewalld_rules('db')
        finally:a.C['hosts']=old_hosts
    else:
        script=a.STATE/'firewall.sh';text=script.read_text()
        for role in new:
            address=a.C['hosts'][role]['address']+'/32'
            text=text.replace('iptables -A PCMAN-DB -s '+address+' -j ACCEPT\n','')
            rule=['PCMAN-DB','-s',address,'-j','ACCEPT']
            try:a.run(['iptables','-C',*rule])
            except RuntimeError:continue
            a.run(['iptables','-D',*rule])
        a.write(script,text,0o700)
    a.ST['worker_hosts']=previous
    for role in new:a.ST.setdefault('retired_workers',{})[role]={'operation':operation,'address':a.C['hosts'][role]['address']}
    a.ST['maintenance']='';a.save()
    receipt.update(cancelled=True,complete=True);a.write(path,json.dumps(receipt))
    return {'cancelled':True,'transaction_found':True,'retired_workers':new,'original_workers_preserved':True}
