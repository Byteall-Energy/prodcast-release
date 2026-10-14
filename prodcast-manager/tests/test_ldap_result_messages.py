import queue
from types import SimpleNamespace
import pytest
from prodcast_manager.directory_dialog import DirectoryDialog
from prodcast_manager.i18n import set_language


@pytest.mark.parametrize('language',['ru','en'])
@pytest.mark.parametrize('extra,detail',[
    ({},('соединения успешна','connection check successful')),
    ({'group_found':True},('Base DN','Base DN')),
    ({'group_found':True,'user_bind':True},('пробный вход успешен','test login successful')),
])
def test_plain_ldap_success_retains_detail_and_warning(language,extra,detail):
    messages=[];events=queue.Queue();events.put(dict(ok=True,encrypted=False,**extra))
    dialog=SimpleNamespace(events=events,inputs=[],status=SimpleNamespace(set=messages.append),on_busy=lambda value:None,after=lambda *args:None,poll=lambda:None)
    try:
        set_language(language);DirectoryDialog.poll(dialog)
        assert detail[language=='en'] in messages[0]
        assert ('не зашифровано' if language=='ru' else 'unencrypted') in messages[0]
        assert ('успеш' if language=='ru' else 'successful') in messages[0]
    finally:set_language('ru')
