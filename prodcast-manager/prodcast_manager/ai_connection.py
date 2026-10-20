"""Restore an installed AI connection without installing packages or Workers."""
import json
from .config import atomic_json


def reconnect_existing_ai(engine,operation):
    from .engine import RESOURCES
    e=engine;path=e.dir/'ai-journal.json'
    prior=json.loads(path.read_text('utf-8')) if path.exists() else {}
    if prior.get('status') in ('running','failed') and not prior.get('ai_link'):
        raise ValueError('Resume or stop the pending AI package operation before reconnecting existing AI.')
    if prior.get('ai_link') and prior.get('status') in ('running','failed') and prior.get('operation')!=operation:
        raise ValueError('Resume the AI connection with its original core operation/profile.')
    remotes={};started=False;journal=dict(prior,operation=operation,status='running',ai_link=True,steps=[])
    try:
        # Validate every owner before modifying DB access or App configuration.
        for role in ('ai','db','app'):
            remote=e.factory(role,e.c['hosts'][role],e.vault.data.get('ssh',{}).get(role,{}),e.log)
            remotes[role]=remote;remote.diagnostics=e.diagnostics
            remote.probe();remote.prepare_stage();remote.put_bytes('linux.py',(RESOURCES/'linux.py').read_bytes())
            status=remote.action(e.payload(role,operation,'check',include_files=False),'preflight',RESOURCES)
            if not status.get('managed') or not status.get('version') or status.get('maintenance') or status.get('operation') not in ('',None,operation):
                raise ValueError(role+': an installed, idle component from this profile is required for AI reconnect')
        remotes['ai'].action(e.payload('ai',operation,'check',include_files=False),'ai-present',RESOURCES)
        if prior and not prior.get('ai_link'):
            history=e.dir/'history';history.mkdir(exist_ok=True)
            atomic_json(history/('ai-before-reconnect-'+operation+'.json'),prior)
        e.vault.data['ai_installed']=True
        if hasattr(e.vault,'save'):e.vault.save()
        atomic_json(path,journal)
        started=True
        for role,action in (('db','ai-access'),('ai','ai-ready'),('app','ai-reconnect'),('app','verify-ai')):
            journal['inflight']=action;atomic_json(path,journal);e.log(role+': '+action)
            result=remotes[role].action(e.payload(role,operation,'check',include_files=False),action,RESOURCES)
            journal['steps'].append(dict(role=role,action=action,result=result))
            journal.pop('inflight',None);atomic_json(path,journal)
        journal.update(status='complete');atomic_json(path,journal)
        return dict(status='complete',existing_ai_preserved=True,models_reinstalled=False,workers_changed=False)
    except Exception:
        if started and journal.get('status')=='running':
            journal['status']='failed';atomic_json(path,journal)
        raise
    finally:
        for remote in remotes.values():
            try:remote.cleanup()
            except Exception:e.log('AI reconnect staging cleanup needs attention; installed models were not removed.')
            try:remote.close()
            except Exception:pass
