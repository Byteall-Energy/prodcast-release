"""Import client settings issued by the external licensing authority."""
from .i18n import tr
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from .activation import validate_activation
from .ui_controls import apply_branding


class ActivationDialog(tk.Toplevel):
    def __init__(self, parent, identity, saved, tenant, on_save):
        super().__init__(parent)
        apply_branding(self)
        self.title(tr('Подключение активации App'))
        self.transient(parent)
        self.identity=identity;self.on_save=on_save
        self.files={k:saved.get(k,'') for k in ('ca_pem','document','public_key')}
        frame=ttk.Frame(self,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=tr('Офлайн-файл выпускается только на сервере лицензий.\nЗдесь можно подключить онлайн-сервер или импортировать готовый файл.'),wraplength=640).pack(anchor='w',pady=(0,12))
        self.tabs=ttk.Notebook(frame);self.tabs.pack(fill='both',expand=True)
        online=ttk.Frame(self.tabs,padding=12);offline=ttk.Frame(self.tabs,padding=12)
        self.tabs.add(online,text=tr('Онлайн-сервер'));self.tabs.add(offline,text=tr('Импорт готового файла'))
        self.values={}
        for row,(key,label,value) in enumerate([
            ('tenant_id',tr('ID компании'),saved.get('tenant_id',tenant)),
            ('url',tr('HTTPS адрес сервера'),saved.get('url','')),
            ('token',tr('Клиентский токен компании'),saved.get('token',''))]):
            self.values[key]=tk.StringVar(value=value)
            ttk.Label(online,text=label).grid(row=row,column=0,sticky='w',pady=5)
            ttk.Entry(online,textvariable=self.values[key],width=55,show='*' if key=='token' else '').grid(row=row,column=1,sticky='ew')
        online.columnconfigure(1,weight=1)
        ttk.Button(online,text=tr('Загрузить CA онлайн-сервера…'),command=lambda:self.pick('ca_pem')).grid(row=3,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(online,text=tr('CA нужен, если сервер использует внутренний сертификат.\nКлиентский токен выдаёт администратор сервера.'),wraplength=600).grid(row=4,column=0,columnspan=2,sticky='w')
        ttk.Label(offline,text=tr('Получите у администратора два готовых файла:'),wraplength=600).pack(anchor='w',pady=(0,8))
        self.file_status={}
        for key,label in [('document',tr('Загрузить выданный файл (.license)…')),('public_key',tr('Загрузить публичный ключ (.pem)…'))]:
            ttk.Button(offline,text=label,command=lambda k=key:self.pick(k)).pack(anchor='w',pady=5)
            self.file_status[key]=tk.StringVar(value=tr('Ранее загруженный файл сохранён.') if self.files[key] else tr('Файл не выбран.'))
            ttk.Label(offline,textvariable=self.file_status[key],wraplength=600).pack(anchor='w')
        ttk.Label(offline,text=tr('Компания, срок и права берутся из подписанного файла.\nManager не меняет их. Приватный ключ подписи не требуется.'),wraplength=600).pack(anchor='w',pady=12)
        ttk.Button(offline,text=tr('Показать ID этой установки'),command=self.show_identity).pack(anchor='w')
        self.status=tk.StringVar(value=tr('Настройки применяются при следующей установке или обновлении App.'))
        ttk.Label(frame,textvariable=self.status,wraplength=640).pack(anchor='w',pady=10)
        self.save_button=ttk.Button(frame,text=tr('Сохранить подключение'),command=self.save);self.save_button.pack(anchor='e')
        self.tabs.bind('<<NotebookTabChanged>>',self.update_caption)
        self.tabs.select(1 if saved.get('mode')=='offline' else 0)
        self.update_caption()
        self.grab_set()

    def update_caption(self,event=None):
        self.save_button.configure(text=tr('Импортировать готовый файл') if self.tabs.index('current')==1 else tr('Сохранить подключение'))

    def show_identity(self):
        top=tk.Toplevel(self);top.title(tr('ID установки — только чтение'));top.transient(self)
        apply_branding(top)
        ttk.Label(top,text=tr('Справочный ID для администратора сервера лицензий.'),padding=12).pack()
        entry=ttk.Entry(top,width=48);entry.insert(0,self.identity);entry.configure(state='readonly');entry.pack(padx=12,pady=6)
        def copy():top.clipboard_clear();top.clipboard_append(self.identity)
        ttk.Button(top,text=tr('Копировать ID'),command=copy).pack(pady=8)

    def pick(self,key):
        types=[(tr('Выданный файл активации'),'*.license')] if key=='document' else [(tr('Публичный сертификат / ключ'),'*.pem *.crt *.cer')]
        path=filedialog.askopenfilename(parent=self,title=tr('Импорт готового файла'),filetypes=types+[(tr('Все файлы'),'*.*')])
        if not path:return
        try:
            if Path(path).stat().st_size>65536:raise ValueError(tr('Файл слишком большой.'))
            text=Path(path).read_text('utf-8-sig')
            if 'PRIVATE KEY-----' in text:raise ValueError(tr('Приватный ключ загружать нельзя. Нужен публичный ключ или сертификат.'))
            self.files[key]=text
            if key in self.file_status:self.file_status[key].set(Path(path).name)
            self.status.set(tr('Выбран файл: ')+Path(path).name+tr('. Он будет проверен при импорте.'))
        except Exception as e:messagebox.showerror(tr('Импорт файла'),str(e),parent=self)

    def value(self):
        if self.tabs.index('current')==0:
            value={'mode':'online',**{k:v.get().strip() for k,v in self.values.items()},'ca_pem':self.files['ca_pem']}
        else:
            # Tenant comes from the issued file, never an editable entitlement field.
            raw=self.files['document']
            try:tenant=json.loads(raw)['payload']['tenant_id']
            except (ValueError,KeyError,TypeError):raise ValueError(tr('Выберите готовый .license, выданный сервером лицензий.')) from None
            value={'mode':'offline','tenant_id':tenant,'document':raw,'public_key':self.files['public_key']}
        return validate_activation(value,self.identity)

    def save(self):
        try:
            value=self.value();self.on_save(value);self.destroy()
        except Exception as e:messagebox.showerror(tr('Подключение активации'),str(e) or tr('Не удалось проверить подпись выданного файла.'),parent=self)
