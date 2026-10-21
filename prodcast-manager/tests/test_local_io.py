import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from prodcast_manager.config import atomic_json
from prodcast_manager import local_io
from prodcast_manager.instance import manager_instance


def test_replace_retries_then_succeeds(tmp_path,monkeypatch):
    path=tmp_path/'journal.json';path.write_text('{"old":true}')
    original=os.replace;attempts=[]
    def replace(source,dest):
        attempts.append((source,dest))
        assert json.loads(path.read_text())=={'old':True}
        if len(attempts)<3:raise PermissionError(errno.EACCES,'temporarily locked')
        return original(source,dest)
    monkeypatch.setattr(local_io.os,'replace',replace);monkeypatch.setattr(local_io.time,'sleep',lambda seconds:None)
    atomic_json(path,{'new':True})
    assert len(attempts)==3 and json.loads(path.read_text())=={'new':True}
    assert not list(tmp_path.glob('*.tmp'))


def test_permanent_denial_preserves_old_and_complete_pending_write(tmp_path,monkeypatch):
    path=tmp_path/'journal.json';path.write_text('{"old":true}')
    monkeypatch.setattr(local_io,'REPLACE_ATTEMPTS',3)
    monkeypatch.setattr(local_io.time,'sleep',lambda seconds:None)
    def denied(*args):raise PermissionError(errno.EACCES,'denied')
    monkeypatch.setattr(local_io.os,'replace',denied)
    with pytest.raises(PermissionError,match='Previous file preserved'):atomic_json(path,{'new':True})
    assert json.loads(path.read_text())=={'old':True}
    pending=list(tmp_path.glob('journal.json.*.tmp'))
    assert len(pending)==1 and json.loads(pending[0].read_text())=={'new':True}


def test_non_permission_error_is_not_misreported_as_permissions(tmp_path,monkeypatch):
    path=tmp_path/'journal.json';path.write_text('{}')
    def full(*args):raise OSError(errno.ENOSPC,'No space left on device')
    monkeypatch.setattr(local_io.os,'replace',full)
    with pytest.raises(OSError) as caught:atomic_json(path,{'new':True})
    assert caught.value.errno==errno.ENOSPC
    assert path.read_text()=='{}'


def test_concurrent_ui_reads_and_atomic_writes_are_valid_json(tmp_path):
    path=tmp_path/'journal.json';atomic_json(path,{'step':0});errors=[];finished=threading.Event()
    def reader():
        try:
            while not finished.is_set():assert isinstance(json.loads(local_io.read_text_shared(path))['step'],int)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=reader);thread.start()
    try:
        for step in range(100):atomic_json(path,{'step':step})
    finally:finished.set();thread.join(5)
    assert not errors and not thread.is_alive()


@pytest.mark.skipif(os.name!='nt',reason='Windows file-sharing behavior')
def test_real_windows_read_handle_temporarily_blocks_replace_then_retry_succeeds(tmp_path):
    path=tmp_path/'journal.json';atomic_json(path,{'old':True});errors=[]
    def writer():
        try:atomic_json(path,{'new':True})
        except Exception as error:errors.append(error)
    with path.open('r') as held:
        thread=threading.Thread(target=writer);thread.start();time.sleep(.15)
        assert json.load(held)=={'old':True}
    thread.join(8)
    assert not errors and not thread.is_alive()
    assert json.loads(path.read_text())=={'new':True}


def test_second_gui_process_is_blocked_and_lock_releases_on_exit(tmp_path):
    code="from prodcast_manager.instance import manager_instance; import sys;\ntry:\n with manager_instance(sys.argv[1]): pass\nexcept OSError: sys.exit(23)"
    def child():return subprocess.run([sys.executable,'-c',code,str(tmp_path)],capture_output=True,text=True,timeout=15)
    with manager_instance(tmp_path):assert child().returncode==23
    result=child();assert result.returncode==0,result.stderr


def test_parallel_process_writers_keep_valid_state(tmp_path):
    path=tmp_path/'journal.json';atomic_json(path,{'step':0})
    code="from prodcast_manager.config import atomic_json; import sys;\nfor step in range(15): atomic_json(sys.argv[1],{'step':step})"
    children=[subprocess.Popen([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    for child in children:
        out,err=child.communicate(timeout=20);assert child.returncode==0,out+err
    assert json.loads(path.read_text())=={'step':14} and not list(tmp_path.glob('*.tmp'))
