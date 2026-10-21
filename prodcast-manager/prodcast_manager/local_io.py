"""Windows-safe atomic local state I/O; never truncate the recovery journal."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_locks={}
_guard=threading.Lock()
REPLACE_ATTEMPTS=24


def _lock(path):
    key=os.path.normcase(str(Path(path).resolve()))
    with _guard:return _locks.setdefault(key,threading.RLock())


def _transient(error):
    return getattr(error,'winerror',None) in (5,32,33) or error.errno in (errno.EACCES,errno.EBUSY,errno.EAGAIN)


def _retry(fn):
    for attempt in range(REPLACE_ATTEMPTS):
        try:return fn()
        except OSError as error:
            if not _transient(error) or attempt==REPLACE_ATTEMPTS-1:raise
            time.sleep(min(.025*(attempt+1),.25))


def read_text_shared(path,encoding='utf-8'):
    path=Path(path)
    with _lock(path):
        if os.name!='nt':return path.read_text(encoding=encoding)
        # CRT file opens do not normally share DELETE. A short UI read must
        # not prevent another process from atomically replacing the journal.
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW
        create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype=wintypes.HANDLE
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
        def open_shared():
            handle=create(str(path.resolve()),0x80000000,0x1|0x2|0x4,None,3,0x80,None)
            if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
            return handle
        handle=_retry(open_shared)
        try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except Exception:close(handle);raise
        with os.fdopen(fd,'r',encoding=encoding) as stream:return stream.read()


def atomic_json(path,obj):
    from .config import file_lock
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with _lock(path):
        # Serialize writers across processes too; each write has a unique tmp.
        def acquire():
            candidate=file_lock(path.with_name(path.name+'.write.lock'))
            candidate.__enter__()
            return candidate
        lock=_retry(acquire)
        try:
            fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
            temporary=Path(name)
            try:
                with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
                    os.chmod(temporary,0o600)
                    json.dump(obj,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
            except Exception:
                temporary.unlink(missing_ok=True);raise
            try:_retry(lambda:os.replace(temporary,path))
            except OSError as error:
                if not _transient(error):raise
                # The old valid journal and the fully written new one survive.
                raise PermissionError(f'Cannot replace local state file after retries: {path}. Previous file preserved; pending write: {temporary}. Windows error: {getattr(error,"winerror",None)}. Close other Manager windows and check file locks/permissions.') from error
        finally:lock.__exit__(None,None,None)
