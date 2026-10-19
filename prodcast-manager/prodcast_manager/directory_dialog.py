"""AD configuration and connection checks for the Manager deployment."""
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from .directory import read_ca, validate_directory
from .i18n import tr

ERRORS = {
    'plaintext_not_allowed':'LDAP без TLS передаёт пароли без шифрования. Подтвердите небезопасный режим отдельно.',
    'certificate': 'Сертификат LDAPS не прошёл проверку. Проверьте CA, имя сервера и срок действия.',
    'dns': 'App VM не разрешает имя контроллера. Проверьте её DNS.',
    'timeout': 'Контроллер не ответил вовремя. Проверьте маршрут и firewall.',
    'tls': 'Сервер оборвал TLS. Проверьте сертификат и настройку LDAPS на контроллере.',
    'refused': 'Порт LDAPS отклонил соединение.',
    'bind_failed': 'AD отклонил вход. Проверьте учётную запись, пароль и политику AD.',
    'invalid_dn': 'Некорректный DN. Используйте полный DN объекта из Active Directory.',
    'base_not_found': 'Base DN не найден или недоступен служебной учётной записи.',
    'group_not_found': 'Группа допуска не найдена или недоступна служебной учётной записи.',
    'wrong_domain': 'Пользователь относится к другому домену.',
    'use_upn': 'Введите логин или user@domain, без префикса DOMAIN\\.',
    'user_not_found': 'Не найден единственный пользователь в указанном Base DN.',
    'user_disabled_or_locked': 'Учётная запись отключена или заблокирована в AD.',
    'not_in_group': 'Пользователь не входит в группу допуска, включая вложенные группы.',
    'directory_error': 'Проверка LDAP не завершена. Проверьте параметры и политику AD.',
}
NOTICE = 'Настройки применяются при установке или обновлении App. Допуск — по группе AD.\nВ текущем App изменение группы не отзывает уже открытые сессии.'


class DirectoryDialog(tk.Toplevel):
    def __init__(self, parent, saved, on_save, on_probe, on_busy=lambda value: None):
        super().__init__(parent)
        from .ui_controls import apply_branding
        apply_branding(self)
        from .directory_form import build
        build(self, parent, saved, on_save, on_probe, on_busy)

    def add_button(self,parent,label,command):
        button=ttk.Button(parent,text=tr(label),command=command);button.pack(side='left',padx=3);self.inputs.append(button)
        return button

    def value(self,connection_only=False):
        return dict(validate_directory({**{k:v.get() for k,v in self.values.items()},'ca_pem':self.ca,'user_search_attribute':self.lookup.get(),'allow_plain_ldap':self.allow_plain_ldap.get()},connection_only),enabled=self.enabled.get())

    def update_ca_status(self):
        if self.values['url'].get().strip().lower().startswith('ldap://'):
            self.ca_status.set(tr('LDAP без TLS: CA не используется, пароли передаются без шифрования.'))
            return
        self.ca_status.set(tr('CA загружен. Проверка цепочки и имени сервера обязательна.') if self.ca else tr('Без импортированного CA: диагностика использует доверие ОС App VM, вход — доверие контейнера App.'))

    def clear_ca(self):
        self.ca='';self.update_ca_status()

    def pick_ca(self):
        path=filedialog.askopenfilename(parent=self,filetypes=[('CA certificates','*.cer *.crt *.pem')])
        if not path:return
        try:
            if Path(path).stat().st_size>262144:raise ValueError(tr('CA: файл слишком большой.'))
            self.ca=read_ca(Path(path).read_bytes());self.update_ca_status()
        except Exception as e:messagebox.showerror(tr('LDAP / Active Directory'),str(e),parent=self)

    def save(self):
        try:
            self.on_save(self.value() if self.enabled.get() else {'enabled':False});self.test_password.set('')
            self.status.set(tr('Настройки сохранены. Нажмите «Установить» или «Обновить», чтобы применить их в App.'))
        except Exception as e:messagebox.showerror(tr('LDAP / Active Directory'),str(e),parent=self)

    def probe(self,mode):
        if self.busy:return
        try:
            value=self.value(mode=='connection');user=self.test_user.get().strip();password=self.test_password.get() if mode=='user' or not value['bind_dn'] else ''
            if mode=='user' and (not user or not password):raise ValueError(tr('Введите логин и пароль для пробного входа.'))
            self.test_password.set('');self.busy=True;self.on_busy(True)
            self.probe_mode=mode
            self.input_states={widget:str(widget.cget('state')) for widget in self.inputs}
            for widget in self.inputs:widget.configure(state='disabled')
            self.status.set(tr('Проверка выполняется с App VM. Приложение и Active Directory не изменяются.'))
            def task():
                try:self.events.put(self.on_probe(value,mode,user,password))
                except Exception:self.events.put({'ok':False,'code':'ssh_error'})
            threading.Thread(target=task,daemon=False).start()
        except Exception as e:messagebox.showerror(tr('LDAP / Active Directory'),str(e),parent=self)

    def poll(self):
        try:
            result=self.events.get_nowait();self.busy=False;self.on_busy(False)
            for widget in self.inputs:widget.configure(state=getattr(self,'input_states',{}).get(widget,'normal'))
            if result['ok']:
                text='LDAPS: сертификат и соединение проверены с App VM.'
                if result.get('group_found'):text='LDAPS: служебный вход, Base DN и группа допуска проверены.'
                if result.get('user_bind'):text='Пробный вход успешен, пользователь входит в группу допуска. Это не включает вход в App.'
                if result.get('encrypted') is False:
                    text='LDAP: проверка соединения успешна.'
                    if result.get('group_found'):text='LDAP: проверка успешна. Служебный вход, Base DN и группа допуска проверены.'
                    if result.get('user_bind'):text='LDAP: пробный вход успешен, пользователь входит в группу допуска. Это не включает вход в App.'
                    self.status.set(tr(text)+'\n'+tr('Предупреждение: соединение LDAP не зашифровано; сертификат не проверяется.'))
                else:self.status.set(tr(text))
            else:self.status.set(tr(ERRORS.get(result.get('code'),'Ошибка проверки SSH/LDAP. Проверьте SSH-отпечаток, доступ к App VM и настройки LDAP.')))
            if hasattr(self,'test_results'):
                self.test_results[self.probe_mode].set(self.status.get())
        except queue.Empty:pass
        self.timer=self.after(100,self.poll)

    def close(self):
        if self.busy:return
        self.test_password.set('');self.values['bind_password'].set('');self.after_cancel(self.timer);self.destroy()
