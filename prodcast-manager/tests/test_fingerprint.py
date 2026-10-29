import contextlib
import socket
import sys
import threading
import time
from pathlib import Path
import pytest
import paramiko

sys.path.insert(0,str(Path(__file__).parents[1]))
from prodcast_manager.ssh import scan, ScanError, fingerprint

@contextlib.contextmanager
def listener(handler):
    stop=threading.Event();errors=[]
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(3)
    host={'address':'127.0.0.1','port':server.getsockname()[1]}
    def serve():
        try:
            conn,_=server.accept()
            with conn:handler(conn,stop)
        except Exception as e:errors.append(e)
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    try:yield host
    finally:stop.set();server.close();thread.join(timeout=4)

def test_tcp_timeout_reports_endpoint_before_auth(monkeypatch):
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:(_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(ScanError) as caught:scan({'address':'10.20.30.23','port':2222})
    assert caught.value.stage=='tcp'
    assert '10.20.30.23:2222' in str(caught.value) and 'No password or SSH key' in str(caught.value)

def test_example_address_rejected_before_network(monkeypatch):
    def no_network(*a,**kw):raise AssertionError('Example address must not be contacted')
    monkeypatch.setattr(socket,'create_connection',no_network)
    with pytest.raises(ScanError,match='template address') as caught:scan({'address':'192.0.2.23','port':22})
    assert caught.value.stage=='configuration'

def test_refused_port_has_separate_message(monkeypatch):
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:(_ for _ in ()).throw(ConnectionRefusedError()))
    with pytest.raises(ScanError,match='connection refused'):scan({'address':'127.0.0.1','port':22})

def test_open_tcp_without_banner_is_not_reported_as_closed_port():
    with listener(lambda conn,stop:stop.wait(3)) as host:
        with pytest.raises(ScanError) as caught:scan(host,banner_timeout=.2,handshake_timeout=.3)
    assert caught.value.stage=='banner'

def test_ssh_banner_without_key_exchange_reports_handshake():
    def serve(conn,stop):conn.sendall(b'SSH-2.0-TestServer\r\n');stop.wait(3)
    with listener(serve) as host:
        with pytest.raises(ScanError) as caught:scan(host,banner_timeout=.2,handshake_timeout=.3)
    assert caught.value.stage=='handshake'

def test_real_loopback_ssh_fingerprint_without_authentication():
    key=paramiko.RSAKey.generate(2048);attempts=[];progress=[]
    class Server(paramiko.ServerInterface):
        def check_auth_none(self,username):attempts.append(username);return paramiko.AUTH_FAILED
        def check_auth_password(self,username,password):attempts.append(username);return paramiko.AUTH_FAILED
        def check_auth_publickey(self,username,key):attempts.append(username);return paramiko.AUTH_FAILED
    def serve(conn,stop):
        time.sleep(.2)  # Delayed banner, as on a busy SSH host.
        transport=paramiko.Transport(conn);transport.add_server_key(key)
        try:transport.start_server(server=Server());stop.wait(3)
        finally:transport.close()
    with listener(serve) as host:result=scan(host,progress.append,banner_timeout=2,handshake_timeout=2)
    assert result==fingerprint(key) and attempts==[]
    assert any('TCP connected' in s for s in progress)

def test_scan_cancel_closes_connection():
    cancel=threading.Event()
    with listener(lambda conn,stop:stop.wait(3)) as host:
        timer=threading.Timer(.2,cancel.set);timer.start()
        try:
            with pytest.raises(ScanError) as caught:scan(host,cancel=cancel,banner_timeout=2,handshake_timeout=2)
            assert caught.value.stage=='cancelled'
        finally:timer.cancel()

@pytest.mark.parametrize('confirm',[False,True])
def test_gui_scan_runs_in_background_and_needs_confirmation(monkeypatch,confirm,tmp_path):
    import tkinter as tk
    from prodcast_manager import gui
    root=tk.Tk();root.withdraw();app=gui.App(root,log_dir=tmp_path)
    release=threading.Event();entered=threading.Event()
    expected='SHA256:'+'a'*43
    def fake_scan(host,progress,cancel):
        assert host=={'address':'10.20.30.23','port':22}
        entered.set();progress('TCP доступен');release.wait(3);return expected
    monkeypatch.setattr('prodcast_manager.ssh.scan',fake_scan)
    monkeypatch.setattr(gui.messagebox,'askyesno',lambda *a,**k:confirm)
    def fail(*a,**k):raise AssertionError(str(a))
    monkeypatch.setattr(gui.messagebox,'showerror',fail)
    def descendants(widget):
        for w in widget.winfo_children():yield w;yield from descendants(w)
    try:
        app.hostvars['app']['address'].set('10.20.30.23')
        app.hostvars['db']['address'].set('other VM still unconfigured')
        started=time.monotonic();app.credentials('app');root.update()
        assert time.monotonic()-started<.3 and entered.wait(1)
        assert app.hostvars['app']['fingerprint'].get()==''
        release.set();deadline=time.monotonic()+3
        def trust_button():
            return next((w for w in descendants(root) if isinstance(w,gui.ttk.Button) and w.cget('text')=='Сверено — доверять этому отпечатку'),None)
        while trust_button() is None and time.monotonic()<deadline:root.update();time.sleep(.02)
        assert trust_button() is not None
        assert app.hostvars['app']['fingerprint'].get()==''
        if confirm:trust_button().invoke()
        assert app.hostvars['app']['fingerprint'].get()==(expected if confirm else '')
    finally:release.set();root.destroy()
