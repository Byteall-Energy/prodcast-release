"""Exercise the real embedded UI and bridge, offline, using isolated profiles."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from prodcast_manager.html_ui import application, DesktopWindow
from prodcast_manager.desktop_controller import DesktopController


def run():
    app=application()
    with tempfile.TemporaryDirectory(prefix='manager-html-check-') as folder:
        controller=DesktopController(Path(folder));window=DesktopWindow(controller)
        def evaluate(script):
            result=[];window.page.runJavaScript(script,result.append)
            deadline=time.monotonic()+10
            while not result and time.monotonic()<deadline: app.processEvents();time.sleep(.01)
            assert result,'JavaScript callback timeout'
            return result[0]
        def until(script):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if evaluate(script):return
                app.processEvents();time.sleep(.02)
            raise AssertionError(script)
        def click(selector):
            evaluate(f"document.querySelector({json.dumps(selector)}).click()")
            app.processEvents()
        until('Boolean(window.manager && window.manager.ready)')
        assert evaluate("document.querySelectorAll('#navigation button').length")==7
        click('[data-page="servers"]')
        until("Boolean(document.querySelector('[data-bind=\"c.hosts.app.address\"]'))")
        assert evaluate("document.querySelector('[data-bind=\"c.hosts.app.address\"]').value")=='','Hint became a value'
        assert evaluate("document.querySelector('[data-bind=\"c.hosts.app.address\"]').placeholder")=='192.0.2.10'
        evaluate("var auth=document.querySelector('[data-bind=\"c.hosts.app.auth\"]');auth.value='password';auth.dispatchEvent(new Event('change',{bubbles:true}))")
        until("Boolean(document.querySelector('[data-bind=\"access.password\"]'))")
        evaluate("var secret=document.querySelector('[data-bind=\"access.password\"]');secret.value='synthetic-ui-password';secret.dispatchEvent(new Event('input',{bubbles:true}))")
        click('[data-action="save-access"]')
        until("document.getElementById('notification').textContent.includes('saved') || document.getElementById('notification').textContent.includes('сохранён')")
        assert controller._read_vault()['ssh']['app']['password']=='synthetic-ui-password'
        assert 'synthetic-ui-password' not in (controller.directory/'ui-draft.json').read_text('utf-8')
        for page in ('overview','deploy','ai','settings','backups','operations'):
            click(f'[data-page="{page}"]');until('Boolean(document.querySelector("main h1"))')
        click('[data-page="settings"]')
        until("Boolean(document.querySelector('[data-action=\"settings-tab\"]'))")
        click('[data-action="settings-tab"][data-tab="directory"]')
        until("Boolean(document.querySelector('[data-bind=\"directory.transport\"]'))")
        evaluate("var transport=document.querySelector('[data-bind=\"directory.transport\"]');transport.value='ldap';transport.dispatchEvent(new Event('change',{bubbles:true}))")
        until("Boolean(document.querySelector('[data-bind=\"directory.allow_plain_ldap\"]'))")
        click('[data-page="deploy"]')
        until("Boolean(document.querySelector('[data-bind=\"c.site_id\"]'))")
        evaluate("var site=document.querySelector('[data-bind=\"c.site_id\"]');site.value='<img src=x onerror=window.untrustedRan=true>';site.dispatchEvent(new Event('input',{bubbles:true}))")
        click('[data-page="overview"]')
        until("document.querySelector('main h1').textContent.includes('<img')")
        assert not evaluate('Boolean(window.untrustedRan)')
        assert evaluate("document.querySelectorAll('main img').length")==0
        assert not window.page.errors,window.page.errors
        if '--capture' in sys.argv:
            destination=Path(sys.argv[sys.argv.index('--capture')+1]).resolve()
            destination.parent.mkdir(parents=True,exist_ok=True)
            click('[data-page="servers"]')
            until("Boolean(document.querySelector('[data-bind=\"c.hosts.app.address\"]'))")
            window.show()
            for _ in range(30):app.processEvents();time.sleep(.03)
            assert window.view.grab().save(str(destination))
        window.allow_close=True;window.close();window.page.deleteLater();window.view.deleteLater();app.processEvents()
        print('PASS: real HTML renderer, Python bridge, seven sections, conditional credentials, encrypted access saving, non-value hints, LDAP transport and no JavaScript errors')


if __name__=='__main__':run()
