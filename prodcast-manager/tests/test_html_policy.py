from PySide6.QtCore import QUrl
from prodcast_manager.html_ui import allowed_resource, WEB_ROOT, LocalPage


def test_only_bundled_assets_and_webchannel_script_can_load(tmp_path):
    assert allowed_resource(QUrl.fromLocalFile(str(WEB_ROOT/'app.js')))
    assert allowed_resource(QUrl('qrc:///qtwebchannel/qwebchannel.js'))
    assert not allowed_resource(QUrl('https://example.com/remote-script.js'))
    assert not allowed_resource(QUrl('http://127.0.0.1:8080/'))
    assert not allowed_resource(QUrl.fromLocalFile(str(tmp_path/'secrets.key')))
    assert not allowed_resource(QUrl('qrc:///unrelated-script.js'))


def test_privileged_page_cannot_navigate_outside_fixed_interface(tmp_path):
    assert LocalPage.acceptNavigationRequest(None,QUrl.fromLocalFile(str(WEB_ROOT/'index.html')),None,True)
    assert not LocalPage.acceptNavigationRequest(None,QUrl('https://example.com'),None,True)
    assert not LocalPage.acceptNavigationRequest(None,QUrl.fromLocalFile(str(tmp_path/'other.html')),None,True)
