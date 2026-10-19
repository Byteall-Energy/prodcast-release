"""Local, append-only operation logs; independent of deployment ownership journals."""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .config import atomic_json

JOURNALS = ('journal.json', 'ai-journal.json', 'workers-journal.json',
            'worker-action-journal.json', 'app-tls-journal.json', 'maintenance-journal.json')


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def target_of(text):
    match = re.match(r'^(?:(?:ОШИБКА|ERROR):\s*)?(app|db|ai|worker[1-9][0-9]*):\s*', text, re.I)
    return match.group(1).lower() if match else ''


def pending_operations(directory):
    """Read all supported journals without changing any recovery state."""
    result = []
    for name in JOURNALS:
        path = Path(directory) / name
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text('utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Expected object')
            if value.get('status') in ('running', 'failed'):
                result.append(dict(journal=name, mode=value.get('mode', ''),
                                   status=value['status'], operation=value.get('operation', ''),
                                   role=value.get('role', ''), phase=value.get('phase', '')))
        except (OSError, ValueError):
            result.append(dict(journal=name, status='unreadable', mode='', role='',
                               operation='', phase=''))
    return result


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
                    value = json.loads(path.read_text('utf-8'))
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
