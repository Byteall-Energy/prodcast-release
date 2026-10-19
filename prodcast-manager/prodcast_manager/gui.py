import json
import os
import queue
import threading
import ipaddress
import socket
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from .config import example, validate, atomic_json, roles, core_roles, worker_roles, MAX_WORKERS
from .vault import Vault
from .release import Release
from .engine import Engine, plan
from .ssh import scan
from . import __version__
from .i18n import set_language, language, localize, rendered, tr
from .session_logs import SessionLog, application_directory
from .portable import (data_directory, profile_directory, runtime_config, save_config, import_site,
                       open_credentials, LegacyPasswordRequired, remember, last_directory)
from cryptography.fernet import InvalidToken
from .app_certificate import read_pfx, store_app_certificate, validate_for_site

class AdminAccessDialog(tk.Toplevel):
    def __init__(self, parent, url, username, password):
        super().__init__(parent)
        self.title(tr('Доступ администратора App'))
        self.transient(parent)
        self.columnconfigure(1, weight=1)
        self.entries = {}
        self.copy_buttons = {}
        ttk.Label(self, text=tr('Начальные данные установки. Если пароль меняли в App, используйте новый.'),
                  padding=12).grid(row=0, column=0, columnspan=3, sticky='w')
        for row, (name, label, value) in enumerate((('url', tr('Адрес App'), url),
                                                   ('username', tr('Логин'), username),
                                                   ('password', tr('Пароль'), password)), 1):
            ttk.Label(self, text=label).grid(row=row, column=0, padx=12, pady=6, sticky='w')
            entry = ttk.Entry(self, width=72, show='*' if name == 'password' else '')
            entry.insert(0, value)
            entry.configure(state='readonly')
            entry.grid(row=row, column=1, padx=6, pady=6, sticky='ew')
            self.entries[name] = entry
            button = ttk.Button(self, text=tr('Копировать'), command=lambda e=entry: self.copy(e))
            button.grid(row=row, column=2, padx=12, pady=6)
            self.copy_buttons[name] = button
            entry.bind('<Control-KeyPress>', lambda event, e=entry: self.shortcut(event, e))
            menu = tk.Menu(self, tearoff=False)
            menu.add_command(label=tr('Копировать'), command=lambda e=entry: self.copy(e, selection=True))
            menu.add_command(label=tr('Выделить всё'), command=lambda e=entry: self.select_all(e))
            entry.bind('<Button-3>', lambda event, m=menu: self.popup(event, m))
        self.show_password = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text=tr('Показать пароль'), variable=self.show_password,
                        command=lambda: self.entries['password'].configure(
                            show='' if self.show_password.get() else '*')).grid(row=4, column=1, sticky='w', padx=6)
        self.copy_status = tk.StringVar(value=tr('Пароль копируется полностью, даже когда скрыт звёздочками.'))
        ttk.Label(self, textvariable=self.copy_status, padding=12).grid(row=5, column=0, columnspan=3, sticky='w')
        ttk.Button(self, text=tr('Закрыть'), command=self.destroy).grid(row=6, column=2, padx=12, pady=12)

    def copy(self, entry, selection=False):
        value = entry.get()
        if selection and entry.selection_present():
            value = value[entry.index('sel.first'):entry.index('sel.last')]
        self.clipboard_clear()
        self.clipboard_append(value)
        self.copy_status.set(tr('Скопировано в буфер обмена.'))
        return 'break'

    @staticmethod
    def select_all(entry):
        entry.selection_range(0, 'end')
        entry.focus_set()
        return 'break'

    def shortcut(self, event, entry):
        if event.keycode == 67 or event.keysym.lower() == 'c':
            return self.copy(entry, selection=True)
        if event.keycode == 65 or event.keysym.lower() == 'a':
            return self.select_all(entry)
        if event.keycode == 88 or event.keysym.lower() == 'x':
            return 'break'

    @staticmethod
    def popup(event, menu):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

class Actions:

    def change_language(self,event=None):
        if self.busy:
            self.language_choice.set('English' if language()=='en' else tr('Русский'))
            messagebox.showinfo(tr('Операция выполняется'),tr('Дождитесь завершения операции перед сменой языка.'))
            return
        chosen='en' if self.language_choice.get()=='English' else 'ru'
        set_language(chosen);localize(self.root)
        atomic_json(data_directory(self.home)/'ui.json',{'language':chosen})


    def visible_workers(self):
        original=worker_roles(getattr(self,'original_config',example()))
        count=int(self.worker_count.get())
        selected=list(original[:count])
        for number in range(1,MAX_WORKERS+1):
            if len(selected)>=count:break
            role='worker'+str(number)
            if role not in selected:selected.append(role)
        return tuple(sorted(selected,key=lambda r:int(r[6:])))


    def populate(self,c):
        self.original_config=dict(c)
        self.hostvars.clear()
        self.worker_count.set(str(len(worker_roles(c))))
        for k,v in self.vars.items():v.set(c.get(k,''))
        self.install_ai.set(c.get('install_ai',True))
        for r in roles(c):
            for k,w in self.hostvars[r].items():w.set(c['hosts'][r].get(k,''))
        if hasattr(self,'worker_selector'):
            self.worker_selector.configure(values=worker_roles(c))
            self.selected_worker.set(worker_roles(c)[0])

    def config(self):
        c={**getattr(self,'original_config',example()),'schema':1,**{k:v.get().strip() for k,v in self.vars.items()},'install_gpu_driver':False,'install_ai':self.install_ai.get(),'hosts':{}}
        for k in ('license_users','license_sessions'): c[k]=int(c[k])
        active=('app','db','ai')+self.visible_workers()
        for r in active:
            v=self.hostvars[r];c['hosts'][r]={k:w.get().strip() for k,w in v.items()};c['hosts'][r]['port']=int(c['hosts'][r]['port'] or '22') if r!='ai' or self.install_ai.get() else 22
        return validate(c)

    def activation_dialog(self):
        if self.busy:return
        try:
            from .activation import installation_id
            from .activation_dialog import ActivationDialog
            v=self.vault();v.initialize(self.config())
            def save_connection(value):
                v.data['activation']=value;self.session_log.protect(v.data);v.save()
                self.log(tr('Подключение активации App сохранено.') if value['mode']=='online' else tr('Готовый офлайн-файл проверен и импортирован.'))
            ActivationDialog(self.root,installation_id(v),v.data.get('activation',{}),self.config()['tenant_id'],save_connection)
        except Exception as e:messagebox.showerror(tr('Подключение активации'),str(e))

    def directory_dialog(self):
        if self.busy:return
        try:
            from .directory import save_directory, probe_directory
            from .directory_dialog import DirectoryDialog
            c=self.prepare_profile();v=self.vault()
            def save(value):
                save_directory(v,value);self.session_log.protect(v.data)
                self.log(tr('Настройки сохранены. Нажмите «Установить» или «Обновить», чтобы применить их в App.'))
            def probe(value,mode,user,password):
                self.session_log.protect({'directory':value,'directory_test_password':password})
                return probe_directory(c['hosts']['app'],v.data.get('ssh',{}).get('app',{}),value,mode,user,password,self.log)
            DirectoryDialog(self.root,v.data.get('directory',{}),save,probe,lambda value:setattr(self,'busy',value))
        except Exception as e:messagebox.showerror(tr('LDAP / Active Directory'),str(e))

    def pick_key(self,r):
        if self.busy:return
        path=filedialog.askopenfilename(title='SSH private key')
        if path:self.hostvars[r]['key_path'].set(path)


    def pick_directory(self):
        if self.busy:return
        self.directory=data_directory(self.home)/'sites'/'new'
        fresh=example();fresh['install_ai']=False;fresh['hosts']['ai']['address']=''
        self.populate(fresh);self.auth={};self.master.set('')
        self.pfx_password.set('');self.pfx_path.set('');self.pfx_url.set('');self.pfx_ca.set('')
        self.show_certificate(None);self.dirlabel.config(text=tr('Новая площадка — будет сохранена в prodcast-data/sites рядом с Manager'))
        self.log(tr('Новая площадка: введите адреса и SSH-доступ. Данные прежней площадки сохранены.'))

    def load(self):
        if self.busy:return
        path=filedialog.askopenfilename(filetypes=[('Site JSON','*.json')])
        if path:
            try:
                self.directory,c=import_site(path,self.home)
                self.populate(c);self.dirlabel.config(text=str(self.directory));self.auth={};self.master.set('')
                self.show_certificate(None);remember(self.home,self.directory)
                self.log(tr('Открыта portable-площадка: ')+str(self.directory)+tr('. Исходная папка не изменена.'))
            except Exception as e:messagebox.showerror(tr('Конфигурация'),str(e))

    def prepare_profile(self):
        from .workers import require_no_expansion
        require_no_expansion(self.directory)
        c=self.config();old_path=self.directory/'site.json'
        if c['admin_ip']==example()['admin_ip']:
            # Resolve the interface towards App without sending a UDP packet.
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
                probe.connect((c['hosts']['app']['address'],22))
                c['admin_ip']=probe.getsockname()[0]
            self.vars['admin_ip'].set(c['admin_ip'])
        old=runtime_config(old_path) if old_path.exists() else None
        if old and worker_roles(old)!=worker_roles(c) and (self.directory/'secrets.json').exists():
            saved=open_credentials(self.directory)
            if saved.data.get('topology'):raise ValueError(tr('Для расширения установленной площадки используйте «Добавить Workers…».'))
        changed=old and (old['site_id']!=c['site_id'] or
                        set(core_roles(old))!=set(core_roles(c)) or any(old['hosts'].get(r,{}).get('address')!=c['hosts'][r]['address'] for r in core_roles(c)))
        if not old or changed:
            if c['public_url']==example()['public_url'] or (changed and c['public_url'].rstrip('/')=='https://'+old['hosts']['app']['address']):
                c['public_url']='https://'+c['hosts']['app']['address'];self.vars['public_url'].set(c['public_url'])
            destination=profile_directory(self.home,c)
            if destination!=self.directory:
                self.directory=destination;self.master.set('');self.show_certificate(None)
                self.log(tr('Площадка: ')+str(destination))
        if old and not changed:c['_ai_topology_address']=old['_ai_topology_address']
        c=save_config(self.directory,c)
        self.original_config=dict(c)
        for r in roles(c):self.hostvars[r]['key_path'].set(c['hosts'][r]['key_path'])
        self.dirlabel.config(text=str(self.directory));remember(self.home,self.directory)
        return c

    def save(self):
        if self.busy:return
        try:self.prepare_profile();self.log(tr('Конфигурация сохранена рядом с Manager.'))
        except Exception as e:messagebox.showerror(tr('Конфигурация'),str(e))

    def vault(self,prepare=True):
        if prepare:self.prepare_profile()
        try:v=open_credentials(self.directory,self.master.get())
        except LegacyPasswordRequired:
            password=simpledialog.askstring(tr('Импорт прежней площадки'),tr('Введите прежний пароль vault один раз. После импорта он больше не понадобится.'),show='*',parent=self.root)
            if password is None:raise ValueError(tr('Импорт отменён; существующий vault не изменён.'))
            v=open_credentials(self.directory,password)
        self.master.set('');self.session_log.protect(v.data)
        stored=v.data.get('ssh',{});stored.update(self.auth);v.data['ssh']=stored;self.auth=stored
        return v

    def save_vault(self):
        if self.busy:return
        try:
            v=self.vault();v.save();self.save();self.show_certificate(v.data.get('app_tls'))
            self.log(tr('Данные доступа сохранены в portable-площадке; пароль хранилища больше не требуется.'))
        except Exception as e:self.credentials_error(e)

    def credentials_error(self,e):
        if isinstance(e,InvalidToken):text=tr('Прежний пароль vault не подошёл либо файл повреждён. Исходный vault не изменён.')
        elif isinstance(e,PermissionError):text=tr('Нет прав записи в папку Manager. Распакуйте приложение в доступную для записи папку.')
        elif isinstance(e,ValueError):text=str(e)
        else:text=tr('Не удалось прочитать или сохранить данные доступа: ')+type(e).__name__+tr('. Сохраните всю папку площадки.')
        self.log(text);messagebox.showerror(tr('Данные доступа'),text)

    def certificate_form(self,frame):
        ttk.Label(frame,text=tr('Сертификат организации для сайта ProdCast'),font=('Segoe UI',14,'bold')).grid(row=0,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Label(frame,text=tr('Выберите PFX/P12, содержащий серверный сертификат и приватный ключ. Пароль PFX нужен только для открытия файла.\nДля существующей площадки внутренний HTTPS адрес на вкладке «Площадка» менять не нужно.'),wraplength=680).grid(row=1,column=0,columnspan=3,sticky='w',pady=8)
        self.pfx_path=tk.StringVar();self.pfx_password=tk.StringVar();self.pfx_url=tk.StringVar();self.pfx_ca=tk.StringVar()
        for row,label,var,hidden in [(2,tr('Адрес сайта клиента (https://имя)'),self.pfx_url,False),(3,tr('PFX / P12 файл'),self.pfx_path,False),(4,tr('Пароль PFX'),self.pfx_password,True),(5,tr('Цепочка CA в PEM (если нет в PFX)'),self.pfx_ca,False)]:
            ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',pady=6)
            ttk.Entry(frame,textvariable=var,show='•' if hidden else '',width=30).grid(row=row,column=1,sticky='ew',padx=8)
        def choose(var,types):
            if self.busy:return
            value=filedialog.askopenfilename(filetypes=types)
            if value:var.set(value)
        ttk.Button(frame,text=tr('Выбрать…'),command=lambda:choose(self.pfx_path,[('PKCS#12','*.pfx *.p12')])).grid(row=3,column=2)
        ttk.Button(frame,text=tr('Выбрать…'),command=lambda:choose(self.pfx_ca,[('PEM certificates','*.pem *.crt'),(tr('Все файлы'),'*.*')])).grid(row=5,column=2)
        ttk.Label(frame,text=tr('Ключ из PFX сохраняется в данных площадки автоматически. Пароль старого vault нужен только при первом импорте.\nПереносите всю папку Manager; она содержит доступ к серверам. Приватный ключ CA организации не требуется.'),wraplength=680).grid(row=6,column=0,columnspan=3,sticky='w',pady=12)
        bar=ttk.Frame(frame);bar.grid(row=7,column=0,columnspan=3,sticky='w')
        ttk.Button(bar,text=tr('Проверить PFX'),command=lambda:self.import_pfx(False)).pack(side='left',padx=3)
        ttk.Button(bar,text=tr('Сохранить сертификат'),command=lambda:self.import_pfx(True)).pack(side='left',padx=3)
        ttk.Button(bar,text=tr('Показать сохранённый сертификат'),command=self.load_certificate_info).pack(side='left',padx=3)
        ttk.Button(frame,text=tr('Применить сертификат на App'),command=lambda:self.start('app-tls')).grid(row=8,column=0,columnspan=3,sticky='w',pady=12)
        self.certificate_info=tk.StringVar(value=tr('Сертификат организации ещё не загружен из vault.'))
        ttk.Label(frame,textvariable=self.certificate_info,wraplength=680).grid(row=9,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Label(frame,text=tr('Импорт сохраняет сертификат локально. «Применить» подключается только к App и перезапускает веб-сервисы — нужно окно обслуживания.\nПри первой установке и последующих обновлениях сохранённый сертификат применяется автоматически.\nDNS клиента должен указывать имя сайта на IP App. ПК пользователей должны доверять CA организации. Автопродление PFX не выполняется.'),wraplength=680).grid(row=10,column=0,columnspan=3,sticky='w',pady=10)
        frame.columnconfigure(1,weight=1)

    def show_certificate(self,material,prefix=None):
        if prefix is None:prefix=tr('Сохранено в vault')
        if not material:
            self.certificate_info.set(tr('В vault нет сертификата организации. Используется внутренний сертификат Manager.'));return
        info=material['info'];self.pfx_url.set(material['url'])
        self.certificate_info.set(tr('{v0}\nАдрес: {v1}\nВыдан: {v2}\nДействует до: {v3}\nSHA256 сертификата: {v4}\nSHA256 корневого CA: {v5}').format(v0=prefix,v1=material['url'],v2=info['issuer'],v3=info['expires'],v4=info['sha256'],v5=info['ca_sha256']))

    def load_certificate_info(self):
        if self.busy:return
        try:self.show_certificate(self.vault().data.get('app_tls'))
        except Exception as e:self.credentials_error(e)

    def import_pfx(self,save):
        if self.busy:return
        try:
            if not self.pfx_path.get():raise ValueError(tr('Выберите PFX / P12 файл.'))
            material=read_pfx(Path(self.pfx_path.get()).read_bytes(),self.pfx_password.get(),self.pfx_url.get().strip(),
                              Path(self.pfx_ca.get()).read_bytes() if self.pfx_ca.get() else b'')
            c=self.config();validate_for_site(material,c)
            if save:
                v=self.vault();store_app_certificate(v,c,material);self.session_log.protect(v.data)
                self.save();self.pfx_password.set('')
                self.log(tr('Сертификат организации сохранён в зашифрованном vault для ')+material['url']+tr('. Сервер пока не изменён.'))
            self.show_certificate(material,tr('Сохранено в vault; для установленной площадки нажмите «Применить»') if save else tr('Проверка пройдена; ещё не сохранено в vault'))
        except Exception as e:
            messagebox.showerror(tr('Импорт PFX'),str(e) if isinstance(e,ValueError) else tr('Не удалось прочитать файлы или сохранить vault. Проверьте доступ и пароль vault.'))

    def show_admin(self):
        try:
            # Viewing credentials must not save/revalidate an in-flight profile.
            directory=self.directory
            secret_file=directory/'secrets.json';key_file=directory/'secrets.key'
            if not secret_file.is_file() or not key_file.is_file():
                raise KeyError('ADMIN_PASSWORD')
            v=Vault(secret_file,key_file.read_text('ascii').strip())
            c=runtime_config(directory/'site.json')
            AdminAccessDialog(self.root,v.data.get('app_tls',{}).get('url') or c['public_url'],
                              c['admin_username'],v.data['secrets']['ADMIN_PASSWORD'])
        except KeyError:messagebox.showerror(tr('Доступ App'),tr('Начальный пароль появится после начала установки этой площадки.'))
        except Exception as e:self.credentials_error(e)

    def log(self,text):
        text=rendered(text)
        try:clean=self.session_log.append(text)
        except OSError as e:
            clean=self.session_log.redact(text)+tr('\nНе удалось записать локальный лог: ')+str(e)
        self.events.put(('log',clean))

    def copy_log(self,all_text=False):
        try:text=self.output.get('1.0','end-1c') if all_text else self.output.get('sel.first','sel.last')
        except tk.TclError:return 'break'
        self.root.clipboard_clear();self.root.clipboard_append(text)
        return 'break'

    def select_log(self):
        self.output.tag_add('sel','1.0','end-1c');self.output.focus_set();return 'break'

    def log_shortcut(self,event):
        if event.keycode==67 or event.keysym.lower()=='c':return self.copy_log()
        if event.keycode==65 or event.keysym.lower()=='a':return self.select_log()

    def open_logs(self):
        try:os.startfile(str(self.session_log.directory))
        except OSError as e:messagebox.showerror(tr('Логи'),str(e))

    def start(self,mode):
        if self.busy:return
        try:
            c=self.config()
            if mode=='plan':
                self.log(tr('План установки (AI — отдельный необязательный этап):\n')+'\n'.join(f'{r} ({c["hosts"][r]["address"]}): {a}' for r,a in plan('install',c.get('install_ai',True),worker_roles(c))));return
            c=self.prepare_profile();validate(c,True);v=self.vault();v.save()
            release_path=self.release.get(); trusted=self.trusted.get().strip();model_path=self.ai_model.get();ollama_path=self.ai_ollama.get()
            if mode in ('install','update','repair','ai') and not release_path:raise ValueError(tr('Выберите полный релиз ZIP или каталог'))
            if mode in ('install','update','repair','backup','app-tls'):
                detail='\n'.join(r+': '+h['address'] for r,h in c['hosts'].items() if (mode!='app-tls' or r=='app') and (r!='ai' or c.get('install_ai',True)))
                notice=tr('Перезапуск веб-сервисов App. Адрес: ')+v.data.get('app_tls',{}).get('url',tr('сначала импортируйте PFX')) if mode=='app-tls' else (tr('Обновление остановит доступ к App и планировщик до завершения.') if mode=='update' else tr('Будет выполнена выбранная операция на этих VM.'))
                if mode=='repair':notice=tr('Восстановление контейнеров и Workers из установленного релиза. Данные сохраняются. Используйте исходную площадку; возможен перезапуск неисправных сервисов.')
                if not messagebox.askyesno(tr('Запуск операции'),f'{mode}\n\n{detail}\n\n'+notice+tr('\n\nПродолжить?')):return
            self.busy=True
            directory=self.directory
            def task():
                try:
                    release=Release(release_path,directory/'cache',trusted,validate_ai=False,model_path=model_path,ollama_path=ollama_path) if mode in ('install','update','repair','ai') else None
                    Engine(c,v,release,directory,self.log,diagnostics=self.session_log).run(mode)
                    self.record_applied(mode,c,v,release,directory)
                except Exception as e:self.log(tr('ОШИБКА: ')+str(e))
                finally:self.events.put(('done',''))
            self.launch_operation(mode,task)
        except Exception as e:messagebox.showerror(tr('Проверка'),str(e))

    def worker_action(self,mode):
        if self.busy:return
        try:
            from .worker_lifecycle import run_worker_action,removal_target
            c=runtime_config(self.directory/'site.json');validate(c,True)
            role=self.selected_worker.get()
            if role not in worker_roles(c):raise ValueError(tr('Выбранный Worker отсутствует в установленном профиле.'))
            if mode=='worker-remove':removal_target(c,role)
            release_path=self.release.get();trusted=self.trusted.get().strip()
            if mode=='worker-repair' and not release_path:raise ValueError(tr('Выберите полный релиз ZIP или каталог'))
            title=tr('Переустановить Worker…') if mode=='worker-repair' else tr('Удалить Worker…')
            notice=tr('Будет переустановлен только выбранный Worker из точного установленного релиза. Его ID и настройки сохраняются.') if mode=='worker-repair' else tr('Будет удалена служба выбранного Worker и отозван его доступ. Остальные Workers сохранят свои ID. Файлы и журналы Worker сохраняются.')
            notice+=tr('\nНазначение новых задач App временно приостанавливается. Операция продолжится после завершения задач.')
            if not messagebox.askyesno(title,role+': '+c['hosts'][role]['address']+'\n\n'+notice+tr('\n\nПродолжить?')):return
            v=self.vault(prepare=False);v.save();directory=self.directory
            self.busy=True
            def task():
                try:
                    release=Release(release_path,directory/'cache',trusted,validate_ai=False) if mode=='worker-repair' else None
                    saved=run_worker_action(directory,role,mode,v,release,self.log,diagnostics=self.session_log)
                    self.events.put(('profile',saved))
                except Exception as e:self.log(tr('ОШИБКА: ')+str(e))
                finally:self.events.put(('done',''))
            self.launch_operation(mode,task)
        except Exception as e:messagebox.showerror(tr('Выбранный Worker'),str(e))

    def add_workers(self):
        if self.busy:return
        try:
            from .workers import add_workers,candidate,read_json,JOURNAL
            if not (self.directory/'site.json').is_file():raise ValueError(tr('Откройте профиль установленной площадки.'))
            pending=read_json(self.directory/JOURNAL)
            c=self.config()
            if pending.get('status') in ('running','failed') and worker_roles(c)==worker_roles(pending['original']):
                self.populate(pending['target']);c=self.config()
            validate(c,True)
            if pending.get('status') not in ('running','failed'):
                _,new=candidate(runtime_config(self.directory/'site.json'),c)
            else:new=pending['new_workers']
            release_path=self.release.get();trusted=self.trusted.get().strip()
            if not release_path:raise ValueError(tr('Выберите полный релиз ZIP или каталог'))
            detail='\n'.join(r+': '+c['hosts'][r]['address'] for r in new)
            if not messagebox.askyesno(tr('Добавить Workers…'),tr('Будут установлены только новые Workers. Выберите тот же релиз, который уже установлен. App и прежние Workers не перезапускаются.')+'\n\n'+detail+tr('\n\nПродолжить?')):return
            v=self.vault(prepare=False);v.save();directory=self.directory
            self.busy=True
            def task():
                try:
                    release=Release(release_path,directory/'cache',trusted,validate_ai=False)
                    saved=add_workers(directory,c,v,release,self.log,diagnostics=self.session_log)
                    self.events.put(('profile',saved))
                except Exception as e:self.log(tr('ОШИБКА: ')+str(e))
                finally:self.events.put(('done',''))
            self.launch_operation('add-workers',task)
        except Exception as e:messagebox.showerror(tr('Добавить Workers…'),str(e))

    def stop_previous(self):
        if self.busy:return
        if not messagebox.askyesno(tr('Остановить предыдущую операцию'),tr('Будет освобождён только владелец завершившейся или зависшей операции на VM. Vault, резервные копии и журналы не удаляются. Продолжить?')):return
        try:
            c=self.prepare_profile();validate(c,True);v=self.vault();v.save();directory=self.directory
            self.busy=True
            def task():
                try:
                    result=Engine(c,v,None,directory,self.log,diagnostics=self.session_log).stop_previous_operation()
                    self.log(tr('Готово: ')+str(result.get('status','stopped')))
                except Exception as e:self.log(tr('ОШИБКА: ')+str(e))
                finally:self.events.put(('done',''))
            self.launch_operation('stop',task)
        except Exception as e:messagebox.showerror(tr('Проверка'),str(e))

    def reset_dialog(self):
        if self.busy:return
        top=tk.Toplevel(self.root);top.title(tr('Очистить VM…'));top.transient(self.root);top.grab_set()
        frame=ttk.Frame(top,padding=20);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=tr('Удаляются только компоненты выбранной установки ProdCast.\nОС, SSH, сеть, системные зависимости и резервные копии на VM сохраняются.'),wraplength=650).pack(anchor='w',pady=8)
        for label,mode in [('Пересоздать компоненты, сохранив данные','reset-keep'),('Полный сброс: удалить данные ProdCast','reset-full')]:
            def choose(m=mode):top.destroy();self.maintenance(m)
            ttk.Button(frame,text=tr(label),command=choose).pack(fill='x',pady=6)

    def maintenance(self,mode):
        if self.busy:return
        try:
            from .maintenance import Maintenance,import_backup_profile
            path='';password=''
            if mode in ('export','restore','import'):
                options=dict(filetypes=[('ProdCast backup','*.pcbackup')])
                path=filedialog.asksaveasfilename(defaultextension='.pcbackup',**options) if mode=='export' else filedialog.askopenfilename(**options)
                if not path:return
                password=simpledialog.askstring(tr('Пароль резервной копии'),tr('Введите пароль бэкапа (минимум 12 символов). Сохраните его отдельно: без пароля восстановление невозможно.'),show='*',parent=self.root)
                if password is None:return
                if len(password)<12:raise ValueError(tr('Пароль должен содержать минимум 12 символов.'))
                if mode=='export':
                    journal_path=self.directory/'maintenance-journal.json'
                    pending=json.loads(journal_path.read_text('utf-8')) if journal_path.exists() else {}
                    resuming=pending.get('status') in ('failed','running') and pending.get('mode')=='export' and pending.get('backup_destination')==str(Path(path).resolve())
                    if Path(path).exists() and not resuming:raise ValueError(tr('Выберите новое имя файла резервной копии.'))
                    again=simpledialog.askstring(tr('Пароль резервной копии'),tr('Повторите пароль'),show='*',parent=self.root)
                    if again!=password:raise ValueError(tr('Пароли не совпадают.'))
            if mode=='import':
                import uuid
                root=data_directory(self.home)
                destination=root/'sites'/('recovered-'+uuid.uuid4().hex[:12])
                c=import_backup_profile(path,password,root/'temporary',destination)
                self.directory=destination;self.populate(c);self.auth={};self.master.set('');self.dirlabel.config(text=str(destination));remember(self.home,destination)
                self.show_certificate(None)
                self.log(tr('Профиль восстановлен. Укажите SSH-доступ. Для чистых VM сначала установите исходный релиз, затем восстановите данные из бэкапа.'));return
            c=self.prepare_profile();validate(c,True);v=self.vault();v.save()
            hosts='\n'.join(r+': '+h['address'] for r,h in c['hosts'].items() if r!='ai' or (mode.startswith('reset-') and c.get('install_ai',True)))
            notice=tr('App и Workers будут временно остановлены. Восстановление заменит текущие данные данными из копии.') if mode=='restore' else tr('Операция временно остановит выбранную установку.')
            if mode=='reset-full':
                notice=tr('Все рабочие данные ProdCast на этих VM будут удалены. Сначала выгрузите бэкап, если данные нужны.')
                answer=simpledialog.askstring(tr('Полный сброс'),hosts+'\n\n'+notice+'\n\n'+tr('Для подтверждения введите имя площадки: ')+c['site_id'],parent=self.root)
                if answer!=c['site_id']:return
            elif not messagebox.askyesno(tr('Запуск операции'),hosts+'\n\n'+notice):return
            self.busy=True;directory=self.directory
            def task():
                try:Maintenance(Engine(c,v,None,directory,self.log,diagnostics=self.session_log)).run(mode,path,password)
                except Exception as e:self.log(tr('ОШИБКА: ')+str(e))
                finally:self.events.put(('done',''))
            self.launch_operation(mode,task)
        except Exception as e:messagebox.showerror(tr('Проверка'),str(e))

    def poll(self):
        while not self.events.empty():
            kind,value=self.events.get()
            if kind=='done':self.busy=False
            elif kind=='profile':self.populate(value)
            else:self.output.config(state='normal');self.output.insert('end',value+'\n');self.output.see('end');self.output.config(state='disabled')
        self.root.after(100,self.poll)

    def close(self):
        if self.busy:messagebox.showinfo(tr('Операция выполняется'),tr('Дождитесь завершения текущего шага. Отключение SSH может оставить операцию незавершённой.'));return
        self.root.destroy()

from .workspace import Workspace


class App(Workspace, Actions):
    def __init__(self, root, log_dir=None, app_home=None):
        self.build_workspace(root, log_dir, app_home, application_directory)


def main():
    root=tk.Tk()
    try:App(root)
    except OSError as e:
        root.withdraw();messagebox.showerror(tr('Папка логов недоступна'),tr('Не удалось создать prodcast-data/logs рядом с EXE. Переместите приложение в папку с правом записи.\n')+str(e));root.destroy();return
    root.mainloop()
