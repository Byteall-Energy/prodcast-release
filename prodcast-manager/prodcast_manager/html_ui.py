"""Offline HTML/CSS desktop shell with a narrow, local-only Qt WebChannel."""
import json
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Slot, Qt
from PySide6.QtGui import QIcon, QDesktopServices
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                     QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView

from . import __version__
from .desktop_controller import DesktopController

RESOURCES = Path(__file__).parent / 'resources'
WEB_ROOT = (RESOURCES / 'webui').resolve()


def allowed_resource(url):
    if url.scheme() == 'qrc': return url.path() == '/qtwebchannel/qwebchannel.js'
    if url.scheme() == 'data': return False
    if not url.isLocalFile(): return False
    path = Path(url.toLocalFile()).resolve()
    return path.is_relative_to(WEB_ROOT) or path == (RESOURCES / 'prodcast-manager.png').resolve()


class LocalRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if not allowed_resource(info.requestUrl()): info.block(True)


class LocalPage(QWebEnginePage):
    def __init__(self, profile, parent):
        super().__init__(profile,parent)
        self.errors = []

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.isLocalFile() and Path(url.toLocalFile()).resolve() == WEB_ROOT / 'index.html'

    def createWindow(self, window_type):
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append(str(message))


class Bridge(QObject):
    def __init__(self, window, controller):
        super().__init__(window)
        self.window = window; self.controller = controller

    @Slot(str, result=str)
    def request(self, raw):
        try:
            if len(raw) > 2 * 1024 * 1024: raise ValueError('Desktop request too large')
            message = json.loads(raw)
            action = message['action']; payload = message.get('payload',{})
            if not isinstance(payload,dict): raise ValueError('Invalid desktop payload')
            if action == 'pick_file': value = self.pick_file(payload)
            elif action == 'copy':
                text = payload.get('text','')
                if not isinstance(text,str): raise ValueError('Clipboard text required')
                QApplication.clipboard().setText(text); value = True
            elif action == 'open_logs':
                value = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.log.directory)))
            elif action == 'legacy_log': value = self.legacy_log()
            elif action == 'close':
                if self.controller.busy: raise ValueError('Wait for the operation before closing')
                if payload.get('choice') != 'discard': raise ValueError('Close confirmation required')
                self.window.allow_close = True; QTimer.singleShot(0,self.window.close); value = True
            else: value = self.controller.dispatch(action,payload)
            return json.dumps(dict(ok=True,value=value),ensure_ascii=False,default=str)
        except Exception as error:
            return json.dumps(dict(ok=False,error=self.controller.log.redact(str(error)),code=type(error).__name__),ensure_ascii=False)

    def pick_file(self, payload):
        self.controller._idle()
        kind = payload.get('kind','zip')
        filters = {'zip':'ZIP archives (*.zip)', 'models':'AI models (*.zip *.gguf)', 'key':'SSH private key (*)',
                   'pfx':'PKCS12 (*.pfx *.p12)', 'ca':'CA certificates (*.cer *.crt *.pem)',
                   'license':'Issued license (*.license)', 'public-key':'Public key (*.pem)',
                   'profile':'Site configuration (site.json *.json)', 'backup':'ProdCast backup (*.pcbackup)',
                   'backup-save':'ProdCast backup (*.pcbackup)', 'directory':'Directory'}
        if kind not in filters: raise ValueError('Unsupported file picker')
        dialog = QFileDialog(self.window)
        dialog.setWindowIcon(self.window.windowIcon())
        dialog.setWindowTitle('ProdCast Manager — ' + filters[kind])
        if kind == 'directory':
            dialog.setFileMode(QFileDialog.FileMode.Directory)
        else:
            dialog.setNameFilter(filters[kind])
            dialog.setFileMode(QFileDialog.FileMode.AnyFile if kind == 'backup-save' else QFileDialog.FileMode.ExistingFile)
        if kind == 'backup-save':
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave); dialog.setDefaultSuffix('pcbackup')
        return dialog.selectedFiles()[0] if dialog.exec() and dialog.selectedFiles() else ''

    def legacy_log(self):
        dialog = QFileDialog(self.window,'ProdCast Manager — session log',str(self.controller.log.directory),'Logs (*.log)')
        dialog.setWindowIcon(self.window.windowIcon()); dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        if not dialog.exec(): return ''
        path = Path(dialog.selectedFiles()[0]).resolve()
        if not path.is_relative_to(self.controller.log.directory.resolve()): raise ValueError('Choose a file from the Manager logs folder')
        if path.stat().st_size > 8 * 1024 * 1024: raise ValueError('Log is too large; open it from the logs folder')
        return self.controller.log.redact(path.read_text('utf-8',errors='replace'))


class DesktopWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller; self.allow_close = False
        self.setWindowTitle('ProdCast Manager ' + __version__)
        self.setWindowIcon(QIcon(str(RESOURCES / 'prodcast-manager.ico')))
        self.resize(1180,850); self.setMinimumSize(900,650)
        self.profile = QWebEngineProfile(self)  # Off-the-record: no persistent browser state/password store.
        self.interceptor = LocalRequests(self.profile); self.profile.setUrlRequestInterceptor(self.interceptor)
        self.profile.downloadRequested.connect(lambda download: download.cancel())
        self.page = LocalPage(self.profile,self)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,False)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,False)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,False)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled,False)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled,False)
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(self,controller); self.channel.registerObject('manager',self.bridge)
        self.page.setWebChannel(self.channel)
        self.view = QWebEngineView(self); self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCentralWidget(self.view)
        self.view.load(QUrl.fromLocalFile(str(WEB_ROOT / 'index.html')))

    def closeEvent(self, event):
        if self.controller.busy:
            event.ignore(); self.page.runJavaScript('window.manager && window.manager.requestClose()'); return
        if not self.allow_close:
            event.ignore(); self.page.runJavaScript('window.manager && window.manager.requestClose()'); return
        event.accept()


def application():
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ProdCast.Manager')
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('ProdCast Manager'); app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(RESOURCES / 'prodcast-manager.ico')))
    return app


def smoke_test(home=None):
    app = application()
    temporary = tempfile.TemporaryDirectory(prefix='manager-html-smoke-',dir=home)
    controller = DesktopController(Path(temporary.name))
    window = DesktopWindow(controller)
    result = {'ok':False,'error':'HTML interface did not finish initializing'}
    timer = QTimer()
    def probe():
        window.page.runJavaScript('Boolean(window.manager && window.manager.ready)',ready)
    def ready(value):
        if not value: return
        window.page.runJavaScript("JSON.stringify({nav:document.querySelectorAll('#navigation button').length,passwords:document.querySelectorAll('input[type=password]').length,hasTitle:document.body.textContent.includes('ProdCast Manager')})",check)
    def check(raw):
        if result['ok']: return
        value = json.loads(raw)
        result['ok'] = value['nav'] == 7 and value['hasTitle'] and not window.page.errors
        result['error'] = '; '.join(window.page.errors) or str(value)
        timer.stop(); window.allow_close = True; window.close(); app.quit()
    timer.timeout.connect(probe); timer.start(200)
    QTimer.singleShot(30000,app.quit)
    app.exec()
    window.page.deleteLater(); window.view.deleteLater(); app.processEvents()
    temporary.cleanup()
    if not result['ok']: raise RuntimeError(result['error'])
    return True


def main():
    app = application(); controller = DesktopController()
    window = DesktopWindow(controller); window.show()
    return app.exec()
