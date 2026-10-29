import json
import threading
import pytest
from prodcast_manager.operation_history import OperationHistory, pending_operations, filtered_events,operation_summary,journal_blocks_changes
from prodcast_manager.session_logs import SessionLog


def test_core_and_worker_runs_persist_separately_and_filter_by_site(tmp_path):
    history = OperationHistory(tmp_path / 'logs')
    core = history.begin('update', tmp_path / 'site', ['app', 'db', 'worker2'], 'package.zip')
    history.append(core, 'app: migrate\ntrace detail')
    history.append(core, 'worker2: verify')
    history.finish(core)
    worker = history.begin('worker-repair', tmp_path / 'other')
    history.finish(worker, error=True)
    reopened = OperationHistory(tmp_path / 'logs')
    assert [x['mode'] for x in reopened.list(tmp_path / 'site')] == ['update']
    events = reopened.events(core['id'])
    assert len(list(filtered_events(events))) == 2
    assert 'migrate' in next(filtered_events(events, 'app', summary=True))
    assert 'trace detail' not in next(filtered_events(events, 'app', summary=True))
    assert 'trace detail' in next(filtered_events(events, 'app'))
    assert reopened.list(tmp_path / 'other')[0]['status'] == 'failed'


def test_history_receives_redacted_events_and_preserves_failed_status(tmp_path):
    log = SessionLog(tmp_path)
    log.protect({'ssh': {'app': {'password': 'synthetic-secret'}}})
    history = OperationHistory(tmp_path)
    item = history.begin('install', tmp_path / 'site')
    history.append(item, log.redact('ОШИБКА: app: synthetic-secret'))
    history.finish(item)
    assert history.list()[0]['status'] == 'failed'
    assert 'synthetic-secret' not in (history.directory / (item['id'] + '.jsonl')).read_text('utf-8')
    assert 'ОШИБКА' in next(filtered_events(history.events(item['id']), 'app'))


def test_partial_tail_and_bad_metadata_do_not_hide_other_runs(tmp_path):
    history = OperationHistory(tmp_path)
    item = history.begin('update', tmp_path)
    history.append(item, 'db: backup')
    with (history.directory / (item['id'] + '.jsonl')).open('a') as stream: stream.write('{broken')
    (history.directory / 'broken.json').write_text('{broken')
    assert len(history.events(item['id'])) == 1
    assert len(history.list()) == 1
    with pytest.raises(ValueError): history.events('../secrets')


def test_all_journal_families_and_unreadable_journal_are_visible(tmp_path):
    for name in ['journal.json', 'ai-journal.json', 'workers-journal.json', 'worker-action-journal.json', 'app-tls-journal.json']:
        (tmp_path / name).write_text(json.dumps(dict(status='failed', operation='test')))
    (tmp_path / 'maintenance-journal.json').write_text('{broken')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = pending_operations(tmp_path)
    assert len(result) == 6
    assert result[-1]['status'] == 'unreadable'
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize('recovered,released,blocking',[(True,True,False),(False,True,True),(True,False,True),(None,None,True)])
def test_failed_export_attention_is_separate_from_recovery_block(tmp_path,recovered,released,blocking):
    value={'status':'failed','mode':'export','operation':'backup','quiesced':True,'previous_runtime':{'worker1':{'running':True}},'services_recovered':recovered,'leases_released':released}
    path=tmp_path/'maintenance-journal.json';path.write_text(json.dumps(value));before=path.read_bytes()
    summary=operation_summary(tmp_path)
    assert summary['running']==0 and summary['needs_attention']==1 and summary['blocking']==int(blocking)
    assert summary['entries'][0]['blocks_changes'] is blocking
    assert journal_blocks_changes(path.name,value) is blocking
    assert path.read_bytes()==before


def test_running_means_active_session_not_a_stale_running_journal(tmp_path):
    (tmp_path/'journal.json').write_text(json.dumps({'status':'running','mode':'update','operation':'stale'}))
    summary=operation_summary(tmp_path)
    assert summary['running']==0 and summary['needs_attention']==1 and summary['blocking']==1
    summary=operation_summary(tmp_path,{'mode':'update','id':'current-session'})
    assert summary['running']==1 and summary['needs_attention']==0 and summary['blocking']==1
    assert len(summary['entries'])==1


def test_read_only_active_action_has_a_live_entry_and_completed_journals_disappear(tmp_path):
    (tmp_path/'maintenance-journal.json').write_text(json.dumps({'status':'complete','mode':'export'}))
    assert operation_summary(tmp_path)['entries']==[]
    summary=operation_summary(tmp_path,{'mode':'status','id':'live'})
    assert summary['running']==1 and summary['needs_attention']==0
    assert summary['entries'][0]['state']=='running'


def test_concurrent_append_has_complete_events(tmp_path):
    history = OperationHistory(tmp_path); item = history.begin('update', tmp_path)
    threads = [threading.Thread(target=lambda: [history.append(item, 'app: check') for _ in range(20)]) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(history.events(item['id'])) == 60
