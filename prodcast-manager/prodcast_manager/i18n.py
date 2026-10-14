"""Local UI language; never part of a deployment's identity or credentials."""
import re
import string
from .translations import EN
from .directory_translations import EN as DIRECTORY_EN
EN.update(DIRECTORY_EN)
from .maintenance_translations import EN as MAINTENANCE_EN
EN.update(MAINTENANCE_EN)
EN.update({
    'Укажите ldaps://имя-контроллера:636 или ldap://имя-контроллера:389. Требуется корректное DNS-имя.':'Enter ldaps://controller-name:636 or ldap://controller-name:389. A valid DNS hostname is required.',
    'LDAP без TLS: CA не используется, пароли передаются без шифрования.':'LDAP without TLS: no CA is used; passwords are sent unencrypted.',
    'LDAP без TLS передаёт пароли без шифрования. Подтвердите небезопасный режим отдельно.':'LDAP without TLS sends passwords unencrypted. Explicitly acknowledge insecure mode.',
    'Разрешить ldap:// без TLS: пароли передаются без шифрования (не рекомендуется)':'Allow ldap:// without TLS: passwords are sent unencrypted (not recommended)',
    'Адрес LDAP / LDAPS':'LDAP / LDAPS address',
    'Проверить LDAP / LDAPS с App VM':'Check LDAP / LDAPS from App VM',
    'LDAP без TLS: проверка выполнена, соединение не зашифровано и сертификат не проверяется.':'LDAP without TLS: check completed; connection is unencrypted and no certificate is verified.',
    'пропущен — существующая установка сохранена':'skipped — existing installation preserved',
    'AI: пакеты не выбраны; обновление пропущено, существующая установка сохранена.':'AI: no packages selected; update skipped and existing installation preserved.',
    'Предыдущая операция AI уже начиналась: журнал восстановления сохранён. При необходимости повторите или остановите её отдельно.':'A previous AI operation already started; its recovery journal is preserved. Retry or stop it separately if needed.',
    'ZIP моделей AI для офлайн-установки:':'AI models ZIP for offline installation:',
    'Выбрать модели…':'Select models…',
    'Добавить Workers…':'Add Workers…',
    'Для расширения установленной площадки используйте «Добавить Workers…».':'To expand an installed site, use “Add Workers…”.',
    'Откройте профиль установленной площадки.':'Open the installed site profile.',
    'Будут установлены только новые Workers. Выберите тот же релиз, который уже установлен. App и прежние Workers не перезапускаются.':'Only new Workers will be installed. Select the exact installed release. App and existing Workers will not be restarted.',
})

_language='ru'
def set_language(language):
    global _language
    if language not in ('ru','en'):raise ValueError('Unsupported language')
    _language=language
def language():return _language
def tr(text):return EN.get(text,text) if _language=='en' else text

def rendered(text):
    """Translate already displayed labels when changing language without rebuilding forms."""
    for ru,en in EN.items():
        source,target=(ru,en) if _language=='en' else (en,ru)
        if text==source:return target
        if '{v' in source:
            pattern='';names=[]
            for literal,field,spec,conversion in string.Formatter().parse(source):
                pattern+=re.escape(literal)
                if field is not None:pattern+='(.*?)';names.append(field)
            match=re.fullmatch(pattern,text,re.S)
            if match:
                values=dict(zip(names,match.groups()))
                # Values are already formatted; preserve their exact displayed bytes.
                result=''
                for literal,field,*_ in string.Formatter().parse(target):
                    result+=literal
                    if field is not None:result+=values[field]
                return result
    for ru,en in sorted(EN.items(),key=lambda pair:len(pair[0]),reverse=True):
        source,target=(ru,en) if _language=='en' else (en,ru)
        if source and ('{v' not in source) and text.startswith(source):return target+text[len(source):]
    return text

def localize(root):
    import tkinter as tk
    from tkinter import ttk
    def visit(widget):
        if isinstance(widget,(tk.Tk,tk.Toplevel)):widget.title(rendered(widget.title()))
        if isinstance(widget,ttk.Notebook):
            for tab in widget.tabs():widget.tab(tab,text=rendered(widget.tab(tab,'text')))
        if isinstance(widget,tk.Menu):
            end=widget.index('end')
            for i in range((end+1) if end is not None else 0):
                if widget.type(i) not in ('separator','tearoff'):
                    widget.entryconfigure(i,label=rendered(widget.entrycget(i,'label')))
        if isinstance(widget,(ttk.Label,ttk.Button,ttk.Checkbutton,tk.Label,tk.Button)):
            variable=widget.cget('textvariable')
            if variable:widget.setvar(variable,rendered(str(widget.getvar(variable))))
            else:widget.configure(text=rendered(widget.cget('text')))
        for child in widget.winfo_children():visit(child)
    visit(root)
