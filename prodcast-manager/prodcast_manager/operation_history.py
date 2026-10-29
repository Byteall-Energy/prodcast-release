"""Local, append-only operation logs; independent of deployment ownership journals."""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .config import atomic_json
from .local_io import read_text_shared

JOURNALS = ('journal.json', 'ai-journal.json', 'workers-journal.json',
            'worker-action-journal.json', 'app-tls-journal.json', 'maintenance-journal.json')


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def target_of(text):
    match = re.match(r'^(?:(?:ОШИБКА|ERROR):\s*)?(app|db|ai|worker[1-9][0-9]*):\s*', text, re.I)
    return match.group(1).lower() if match else ''


def journal_blocks_changes(name,value):
    """A failed export is informational once services and remote leases recovered."""
    if value.get('status') not in ('running','failed','unreadable'):return False
    if name=='maintenance-journal.json' and value.get('mode')=='export' and value.get('status')=='failed':
        services_safe=value.get('services_recovered') is True or not (value.get('previous_runtime') or value.get('quiesced'))
        return not (services_safe and value.get('leases_released') is True)
    return True


def operation_state(name,value):
    blocking=journal_blocks_changes(name,value)
    if not blocking:
        code='export_recovered'
        reason='The backup export failed. Services are restored and server locks are released. Retry export when ready; other changes are allowed.'
    elif value.get('status')=='unreadable':
        code='unreadable'
        reason='The recovery journal cannot be read. Restore or inspect it before making conflicting changes.'
    elif name=='maintenance-journal.json' and value.get('mode')=='export':
        code='recovery_unconfirmed'
        reason='Service recovery or server lock release is not confirmed. Retry the backup export before making conflicting changes.'
    else:
        code='incomplete_changes'
        reason='This operation may leave services paused or retain server locks. Resume its original action, or use its supported stop/recovery action before conflicting changes.'
    return dict(state='needs_attention',blocks_changes=blocking,reason_code=code,blocking_reason=reason)


def pending_operations(directory):
    """Read all supported journals without changing any recovery state."""
    result = []
    for name in JOURNALS:
        path = Path(directory) / name
        if not path.exists():
            continue
        try:
            value = json.loads(read_text_shared(path))
            if not isinstance(value, dict):
                raise ValueError('Expected object')
            if value.get('status') in ('running', 'failed'):
                result.append(dict(journal=name, mode=value.get('mode', ''),
                                   status=value['status'], operation=value.get('operation', ''),
                                   role=value.get('role', ''), phase=value.get('phase', ''),**operation_state(name,value)))
        except (OSError, ValueError):
            result.append(dict(journal=name, status='unreadable', mode='', role='',
                               operation='', phase='',**operation_state(name,{'status':'unreadable'})))
    return result


def operation_summary(directory,active=None):
    entries=pending_operations(directory)
    if active:
        mode=active.get('mode','')
        family=('maintenance-journal.json' if mode in ('export','restore','reset-keep','reset-full') else
                'ai-journal.json' if mode=='ai' else 'app-tls-journal.json' if mode=='app-tls' else
                'workers-journal.json' if mode=='add-workers' else 'worker-action-journal.json' if mode.startswith('worker-') else
                'journal.json' if mode in ('install','update','repair') else '')
        entry=next((x for x in entries if x['journal']==family),None)
        if entry is None:
            entry=dict(journal=family,mode=mode,status='running',operation=active.get('id',''),role='',phase='')
            entries.insert(0,entry)
        entry.update(state='running',blocks_changes=True,reason_code='active',blocking_reason='An operation is executing in this Manager. Wait for it to finish before starting another change.')
    return dict(running=sum(x['state']=='running' for x in entries),
                needs_attention=sum(x['state']=='needs_attention' for x in entries),
                blocking=sum(x['blocks_changes'] for x in entries),entries=entries)


class OperationHistory:
    def __init__(self, directory):
        self.directory = Path(directory) / 'operations'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def begin(self, mode, profile, targets=(), release=''):
        with self.lock:
            identity = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:12]
            item = dict(id=identity, mode=mode, profile=str(Path(profile).resolve()),
                        targets=list(targets), release=str(release), started=now(),
                        status='running', finished='', error=False)
            atomic_json(self.directory / (identity + '.json'), item)
            (self.directory / (identity + '.jsonl')).touch(exist_ok=False)
            return item

    def append(self, item, text):
        """Caller passes already-redacted text; preserve order and target per event."""
        with self.lock:
            target = target_of(text)
            error = bool(re.search(r'(^|\n)(ОШИБКА:|ERROR:)', text))
            item['error'] = item['error'] or error
            event = dict(time=now(), target=target, text=text, error=error)
            with (self.directory / (item['id'] + '.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')

    def finish(self, item, error=False):
        with self.lock:
            item.update(status='failed' if error or item['error'] else 'finished', finished=now())
            atomic_json(self.directory / (item['id'] + '.json'), item)

    def list(self, profile=None):
        items = []
        with self.lock:
            for path in self.directory.glob('*.json'):
                try:
                    value = json.loads(read_text_shared(path))
                    if not isinstance(value, dict) or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{12}', value.get('id', '')):
                        continue
                    if path.stem != value['id']:
                        continue
                    if profile is None or value.get('profile') == str(Path(profile).resolve()):
                        items.append(value)
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda item: item['id'], reverse=True)

    def events(self, identity):
        if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{12}', identity):
            raise ValueError('Invalid operation ID')
        result = []
        with self.lock:
            try:
                with (self.directory / (identity + '.jsonl')).open(encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict) and isinstance(event.get('text'), str):
                                result.append(event)
                        except ValueError:
                            continue  # Interrupted final line must not hide earlier logs.
            except FileNotFoundError:
                pass
        return result


def filtered_events(events, target='', summary=False):
    for event in events:
        if target and event.get('target') != target:
            continue
        text = event['text']
        if summary:
            # Summary is the first line of each message, not an inferred health result.
            text = text.splitlines()[0] if text.splitlines() else ''
        yield f"{event['time']} {text}"
