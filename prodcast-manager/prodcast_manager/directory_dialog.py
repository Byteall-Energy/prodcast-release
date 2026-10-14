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
        self.title(tr('LDAP / Active Directory'))
        self.transient(parent); self.resizable(True, False)
        self.on_save=on_save; self.on_probe=on_probe; self.on_busy=on_busy
        self.busy=False; self.events=queue.Queue(); self.ca=saved.get('ca_pem','')
        self.protocol('WM_DELETE_WINDOW', self.close)
        frame=ttk.Frame(self,padding=16);frame.pack(fill='both',expand=True);frame.columnconfigure(1,weight=1)
        ttk.Label(frame,text=tr(NOTICE),wraplength=780).grid(row=0,column=0,columnspan=3,sticky='w',pady=(0,12))
        self.enabled=tk.BooleanVar(value=saved.get('enabled',False))
        ttk.Checkbutton(frame,text=tr('Включить доменный вход в App'),variable=self.enabled).grid(row=1,column=0,columnspan=3,sticky='w',pady=8)
        self.lookup=tk.StringVar(value=saved.get('user_search_attribute','userPrincipalName'))
        ttk.Label(frame,text=tr('Поиск пользователя в AD')).grid(row=8,column=0,sticky='w')
        ttk.Combobox(frame,textvariable=self.lookup,values=('userPrincipalName','sAMAccountName'),state='readonly').grid(row=8,column=1,sticky='ew')
        self.values={};self.inputs=[]
        fields=[('url','Адрес LDAP / LDAPS','ldaps://dc01.example.com:636'),
                ('domain','Домен пользователей','example.com'),
                ('base_dn','Base DN поиска','DC=example,DC=com'),
                ('group_dn','DN группы допуска','CN=ProdCast-Users,OU=Groups,DC=example,DC=com'),
                ('bind_dn','Служебная учётная запись (UPN / DN)','svc-prodcast@example.com'),
                ('bind_password','Пароль служебной учётной записи','')]
        for row,(key,label,example) in enumerate(fields,2):
            self.values[key]=tk.StringVar(value=saved.get(key,''))
            ttk.Label(frame,text=tr(label)).grid(row=row,column=0,sticky='w',padx=(0,10),pady=4)
            entry=ttk.Entry(frame,textvariable=self.values[key],width=64,show='*' if key=='bind_password' else '')
            entry.grid(row=row,column=1,columnspan=2,sticky='ew');self.inputs.append(entry)
        ttk.Label(frame,text=tr('Пример: ldaps://dc01.example.com:636; Base DN: DC=example,DC=com.\nСлужебной учётной записи достаточно прав чтения пользователей и групп.'),wraplength=780).grid(row=9,column=0,columnspan=3,sticky='w',pady=8)
        ca_bar=ttk.Frame(frame);ca_bar.grid(row=10,column=0,columnspan=3,sticky='w')
        self.add_button(ca_bar,'Загрузить CA (.cer / .crt / .pem)…',self.pick_ca)
        self.add_button(ca_bar,'Использовать системные CA',self.clear_ca)
        self.ca_status=tk.StringVar();self.update_ca_status()
        self.values['url'].trace_add('write',lambda *args:self.update_ca_status())
        ttk.Label(frame,textvariable=self.ca_status,wraplength=780).grid(row=11,column=0,columnspan=3,sticky='w',pady=4)
        bar=ttk.Frame(frame);bar.grid(row=12,column=0,columnspan=3,sticky='w',pady=8)
        self.add_button(bar,'Проверить LDAP / LDAPS с App VM',lambda:self.probe('connection'))
        self.add_button(bar,'Проверить учётную запись и группу',lambda:self.probe('directory'))
        ttk.Separator(frame).grid(row=13,column=0,columnspan=3,sticky='ew',pady=8)
        self.test_user=tk.StringVar();self.test_password=tk.StringVar()
        for row,label,var in [(14,'Пробный пользователь (логин / UPN)',self.test_user),(15,'Пароль пробного пользователя',self.test_password)]:
            ttk.Label(frame,text=tr(label)).grid(row=row,column=0,sticky='w')
            entry=ttk.Entry(frame,textvariable=var,show='*' if row==15 else '',width=64)
            entry.grid(row=row,column=1,columnspan=2,sticky='ew',pady=4);self.inputs.append(entry)
        bar=ttk.Frame(frame);bar.grid(row=16,column=0,columnspan=3,sticky='w',pady=8)
        self.add_button(bar,'Проверить пользователя и допуск',lambda:self.probe('user'))
        ttk.Label(frame,text=tr('Пробный пароль не сохраняется. Не повторяйте неверный пароль: AD может заблокировать учётную запись.'),wraplength=780).grid(row=17,column=0,columnspan=3,sticky='w')
        self.status=tk.StringVar(value=tr('Настройки сохраняются только в защищённых данных площадки Manager.'))
        ttk.Label(frame,textvariable=self.status,wraplength=780).grid(row=18,column=0,columnspan=3,sticky='w',pady=12)
        bar=ttk.Frame(frame);bar.grid(row=19,column=0,columnspan=3,sticky='e')
        self.add_button(bar,'Сохранить настройки LDAP',self.save)
        self.add_button(bar,'Закрыть',self.close)
        self.allow_plain_ldap=tk.BooleanVar(value=saved.get('allow_plain_ldap',False))
        warning=ttk.Checkbutton(frame,text=tr('Разрешить ldap:// без TLS: пароли передаются без шифрования (не рекомендуется)'),variable=self.allow_plain_ldap)
        warning.grid(row=20,column=0,columnspan=3,sticky='w',pady=8);self.inputs.append(warning)
        self.timer=self.after(100,self.poll);self.grab_set()

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
            value=self.value(mode=='connection');user=self.test_user.get().strip();password=self.test_password.get() if mode=='user' else ''
            if mode=='user' and (not user or not password):raise ValueError(tr('Введите логин и пароль для пробного входа.'))
            self.test_password.set('');self.busy=True;self.on_busy(True)
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
            for widget in self.inputs:widget.configure(state='normal')
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
        except queue.Empty:pass
        self.timer=self.after(100,self.poll)

    def close(self):
        if self.busy:return
        self.test_password.set('');self.values['bind_password'].set('');self.after_cancel(self.timer);self.destroy()
