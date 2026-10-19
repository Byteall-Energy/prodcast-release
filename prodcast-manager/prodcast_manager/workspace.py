"""Task-oriented desktop workspace. Deployment methods remain in gui.App."""
import json
import queue
import threading
import ipaddress
import hashlib
import hmac
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from cryptography.fernet import InvalidToken

from . import __version__
from .config import example, roles, worker_roles, MAX_WORKERS, atomic_json
from .i18n import tr, EN, language, set_language, localize
from .portable import data_directory, last_directory, runtime_config, remember
from .session_logs import SessionLog
from .operation_history import OperationHistory, pending_operations, filtered_events, target_of


def label(en, ru):
    EN[ru] = en
    return tr(ru)


MODE_NAMES = {
    'install': ('Install', 'Установка'), 'update': ('Update', 'Обновление'),
    'repair': ('Repair components', 'Восстановление компонентов'),
    'ai': ('AI setup / retry', 'Настройка / повтор AI'),
    'worker-repair': ('Reinstall worker', 'Переустановка Worker'),
    'worker-remove': ('Remove worker', 'Удаление Worker'),
    'add-workers': ('Add workers', 'Добавление Workers'),
    'backup': ('Server backup', 'Бэкап на серверах'),
    'export': ('Export backup', 'Выгрузка бэкапа'),
    'restore': ('Restore data', 'Восстановление данных'),
    'reset-keep': ('Recreate; keep data', 'Пересоздание с сохранением данных'),
    'reset-full': ('Delete installation data', 'Удаление данных установки'),
    'app-tls': ('Deploy certificate', 'Применение сертификата'),
    'stop': ('Stop previous operation', 'Остановка предыдущей операции'),
    'check': ('Check access', 'Проверка доступа'), 'status': ('Refresh status', 'Проверка состояния'),
    'verify-package': ('Verify local release', 'Проверка локального релиза'),
    'check-server': ('Check selected server', 'Проверка выбранного сервера'),
}


class Workspace:
    def build_workspace(self, root, log_dir, app_home, default_home):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.auth = {}; self.vars = {}; self.hostvars = {}
        self.home = Path(app_home) if app_home else default_home()
        self.session_log = SessionLog(log_dir or data_directory(self.home) / 'logs')
        self.history = OperationHistory(self.session_log.directory)
        self.active_operation = None
        self.operation_thread = threading.local()
        self.session_log.remote_listener = lambda role, action, text: self.log(role + ': ' + action + ' — ' + label('server diagnostics', 'диагностика сервера') + '\n' + text)
        self.session_messages = []
        self.operation_records = {}
        self.pages = {}; self.nav_buttons = {}; self.editors = []
        self.directory = last_directory(self.home)
        self.editor_role = None
        self.secret_vars = {}; self.secret_loaded = {}
        self._editor_loading = False
        self._draft_dirty = False
        self._loading_profile = True
        self.checked_release = None
        self.wizard_index = 0
        try:
            chosen = json.loads((data_directory(self.home) / 'ui.json').read_text('utf-8')).get('language', 'ru')
        except (OSError, ValueError):
            chosen = 'ru'
        set_language(chosen if chosen in ('ru', 'en') else 'ru')
        self.root.title('ProdCast Manager ' + __version__)
        root.geometry('1180x850'); root.minsize(920, 650)
        icon = Path(__file__).parent / 'resources/prodcast-manager.ico'
        if icon.exists():
            try: root.iconbitmap(str(icon))
            except tk.TclError: pass
        style = ttk.Style(); style.theme_use('clam')
        style.configure('TButton', padding=7)
        style.configure('Heading.TLabel', font=('Segoe UI', 18, 'bold'))
        shell = ttk.Frame(root, padding=12); shell.pack(fill='both', expand=True)
        chrome = ttk.Frame(shell); chrome.pack(fill='x')
        ttk.Label(chrome, text='ProdCast Manager', style='Heading.TLabel').pack(side='left')
        ttk.Button(chrome, text=tr('Доступ администратора App'), command=self.show_admin).pack(side='right')
        self.language_choice = tk.StringVar(value='English' if language() == 'en' else 'Русский')
        selector = ttk.Combobox(chrome, textvariable=self.language_choice, values=('Русский', 'English'), width=9, state='readonly')
        selector.pack(side='right', padx=8); selector.bind('<<ComboboxSelected>>', self.change_language)
        context = ttk.Frame(shell); context.pack(fill='x', pady=10)
        self.site_choice = tk.StringVar()
        self.site_selector = ttk.Combobox(context, textvariable=self.site_choice, state='readonly', width=36)
        self.site_selector.pack(side='left'); self.site_selector.bind('<<ComboboxSelected>>', self.choose_site)
        for text, ru, cmd in [('New site', 'Новая площадка', self.pick_directory), ('Open profile…', 'Открыть профиль…', self.load), ('Save draft', 'Сохранить черновик', self.save_draft)]:
            ttk.Button(context, text=label(text, ru), command=cmd).pack(side='left', padx=4)
        self.workspace_status = tk.StringVar()
        ttk.Label(shell, textvariable=self.workspace_status, wraplength=1100).pack(anchor='w')
        body = ttk.Frame(shell); body.pack(fill='both', expand=True, pady=8)
        side = ttk.Frame(body, padding=(0, 0, 12, 0)); side.pack(side='left', fill='y')
        self.page_host = ttk.Frame(body); self.page_host.pack(side='left', fill='both', expand=True)
        titles = [('overview', 'Overview', 'Обзор'), ('servers', 'Servers & workers', 'Серверы и Workers'),
                  ('deploy', 'Install / update', 'Установка / обновление'), ('ai', 'AI', 'AI'),
                  ('settings', 'App settings', 'Настройки App'), ('backups', 'Backups & recovery', 'Бэкапы и восстановление'),
                  ('operations', 'Operations', 'Операции')]
        for key, en, ru in titles:
            self.nav_buttons[key] = ttk.Button(side, text=label(en, ru), command=lambda k=key: self.show_page(k))
            self.nav_buttons[key].pack(fill='x', pady=3)
            outer = ttk.Frame(self.page_host); self.pages[key] = outer
            if key == 'operations':
                inner = outer
            else:
                canvas = tk.Canvas(outer, highlightthickness=0)
                scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
                canvas.configure(yscrollcommand=scrollbar.set)
                scrollbar.pack(side='right', fill='y'); canvas.pack(side='left', fill='both', expand=True)
                inner = ttk.Frame(canvas, padding=10)
                window = canvas.create_window((0, 0), window=inner, anchor='nw')
                inner.bind('<Configure>', lambda e, c=canvas: c.configure(scrollregion=c.bbox('all')))
                def fit(event, c=canvas, w=window, frame=inner):
                    c.itemconfigure(w, width=event.width)
                    def wrap(parent):
                        for child in parent.winfo_children():
                            if isinstance(child, ttk.Label) and int(child.cget('wraplength') or 0) > 0:
                                child.configure(wraplength=max(260, event.width - 55))
                            wrap(child)
                    wrap(frame)
                canvas.bind('<Configure>', fit)
                canvas.bind('<MouseWheel>', lambda e, c=canvas: c.yview_scroll(int(-e.delta / 120), 'units'))
            setattr(self, key + '_page', inner)
        for name in ('site_id', 'public_url', 'admin_ip', 'admin_username', 'admin_email', 'app_subnet'):
            self.vars[name] = tk.StringVar()
        for name in ('release', 'trusted', 'master', 'ai_model', 'ai_ollama'):
            setattr(self, name, tk.StringVar())
        self.worker_count = tk.StringVar(value='2')
        self.install_ai = tk.BooleanVar(value=False)
        self.ai_widgets = []
        self.selected_worker = tk.StringVar()
        self.worker_selector = ttk.Combobox(self.servers_page, textvariable=self.selected_worker, state='readonly')
        self.hostframe = ttk.Frame(self.servers_page)  # Compatibility with host-variable helpers.
        self.worker_count.trace_add('write', lambda *_: self.render_servers())
        self.install_ai.trace_add('write', lambda *_: self.toggle_ai())
        self.build_overview(); self.build_servers(); self.build_deploy()
        self.build_ai(); self.build_settings(); self.build_backups(); self.build_operations()
        self.dirlabel = ttk.Label(shell, wraplength=1100); self.dirlabel.pack(anchor='w')
        fresh = example(); fresh['install_ai'] = False; fresh['hosts']['ai']['address'] = ''
        self.populate(fresh)
        saved = self.directory / 'site.json'
        if saved.exists():
            try: self.populate(runtime_config(saved))
            except Exception as exc: self.log(tr('ОШИБКА: ') + str(exc))
        self.read_saved_access()
        self.edit_server(self.editor_role or 'app')
        self.restore_draft()
        self._loading_profile = False
        for variable in [*self.vars.values(), self.release, self.trusted, self.ai_model, self.ai_ollama, self.install_ai, self.worker_count]:
            variable.trace_add('write', self.mark_dirty)
        self.refresh_sites(); self.refresh_overview(); self.refresh_operations(); self.show_page('overview')
        self.log(tr('Журнал этого запуска: ') + str(self.session_log.path))
        root.after(100, self.poll); root.protocol('WM_DELETE_WINDOW', self.close)

    def heading(self, parent, en, ru, detail=''):
        ttk.Label(parent, text=label(en, ru), style='Heading.TLabel').pack(anchor='w', pady=(0, 8))
        if detail: ttk.Label(parent, text=detail, wraplength=820).pack(anchor='w', pady=(0, 10))

    def field(self, parent, en, ru, variable, secret=False, browse=None):
        group = ttk.Frame(parent); group.pack(fill='x', pady=5)
        ttk.Label(group, text=label(en, ru)).pack(anchor='w')
        line = ttk.Frame(group); line.pack(fill='x')
        entry = ttk.Entry(line, textvariable=variable, show='•' if secret else '')
        entry.pack(side='left', fill='x', expand=True); self.editors.append(entry)
        if secret:
            show = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(line, text=label('Show', 'Показать'), variable=show,
                                   command=lambda: entry.configure(show='' if show.get() else '•'))
            check.pack(side='left', padx=5); self.editors.append(check)
        if browse:
            button = ttk.Button(line, text=label('Choose…', 'Выбрать…'), command=browse)
            button.pack(side='left', padx=5); self.editors.append(button)
        return entry

    def button(self, parent, en, ru, command):
        button = ttk.Button(parent, text=label(en, ru), command=command)
        button.pack(side='left', padx=(0, 6), pady=5)
        def arrange(event=None):
            children = parent.winfo_children()
            if not children or not all(isinstance(child, ttk.Button) for child in children): return
            width = max(1, parent.winfo_width())
            row = col = used = 0
            for child in children: child.pack_forget(); child.grid_forget()
            for child in children:
                size = child.winfo_reqwidth() + 8
                if col and used + size > width: row += 1; col = used = 0
                child.grid(row=row, column=col, padx=(0, 6), pady=5, sticky='w')
                col += 1; used += size
        parent.bind('<Configure>', arrange)
        return button

    def show_page(self, name):
        for page in self.pages.values(): page.pack_forget()
        self.pages[name].pack(fill='both', expand=True)
        self.current_page = name
        if name == 'operations': self.refresh_operations()
        if name == 'overview': self.refresh_overview()

    def build_overview(self):
        p = self.overview_page
        self.heading(p, 'Site overview', 'Обзор площадки')
        self.overview_text = tk.StringVar()
        ttk.Label(p, textvariable=self.overview_text, wraplength=820, justify='left').pack(anchor='w', pady=12)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Guided install / update', 'Мастер установки / обновления', lambda: self.show_page('deploy'))
        self.button(bar, 'Refresh remote status', 'Проверить состояние VM', lambda: self.start('status'))
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Manage workers', 'Управление Workers', lambda: self.show_page('servers'))
        self.button(bar, 'Inspect pending operations', 'Незавершённые операции', lambda: self.show_page('operations'))
        ttk.Label(p, text=label('Copy the whole prodcast-data folder when moving Manager. It includes server credentials and their local encryption key.',
                               'При переносе Manager копируйте всю папку prodcast-data. Она содержит доступ к серверам и локальный ключ шифрования.'), wraplength=820).pack(anchor='w', pady=20)

    def refresh_overview(self):
        pending = pending_operations(self.directory)
        journal = self.directory / 'journal.json'
        version = label('Not verified', 'Не проверена')
        try:
            record = json.loads(journal.read_text('utf-8'))
            version = record.get('version') or version
        except (OSError, ValueError, AttributeError): pass
        applied_state = label('Applied settings: unknown (no deployment receipt).', 'Применённые настройки: неизвестны (нет квитанции развёртывания).')
        try:
            receipt = json.loads((self.directory / 'ui-deployed.json').read_text('utf-8'))
            version = receipt['version']
            from .vault import Vault
            key = (self.directory / 'secrets.key').read_text('ascii').strip()
            vault = Vault(self.directory / 'secrets.json', key)
            config = runtime_config(self.directory / 'site.json')
            same = receipt['settings_signature'] == self.settings_signature(config, vault.data, key)
            applied_state = label('Saved settings match the last successful deployment.', 'Сохранённые настройки совпадают с последним успешным развёртыванием.') if same else label('Saved App settings differ from the last deployment. Review and apply by update.', 'Сохранённые настройки App отличаются от последнего развёртывания. Проверьте и примените обновлением.')
        except (OSError, ValueError, KeyError, InvalidToken): pass
        text = [self.vars['site_id'].get(), label('Release recorded locally: ', 'Релиз в локальном журнале: ') + version,
                label('Server health: use Refresh remote status; a completed journal is not a health check.',
                      'Работоспособность VM: нажмите проверку состояния; завершённый журнал не заменяет диагностику.'),
                label('Pending operations: ', 'Незавершённых операций: ') + str(len(pending)),
                applied_state]
        self.overview_text.set('\n\n'.join(text))
        self.dirlabel.config(text=str(self.directory))
        self.workspace_status.set(label('Operation running — conflicting changes are locked.', 'Операция выполняется — изменения заблокированы.') if self.busy else
                                  label('Local draft has unsaved edits.', 'В черновике есть несохранённые изменения.') if self._draft_dirty else
                                  label('Local profile · remote changes require a reviewed action.', 'Локальный профиль · изменения VM запускаются отдельной операцией.'))

    def build_servers(self):
        p = self.servers_page
        self.heading(p, 'Servers & workers', 'Серверы и Workers', label('Select a server to edit access. Password and key fields follow your sign-in choice.', 'Выберите сервер. Поля пароля и ключа зависят от способа входа.'))
        ttk.Label(p, text=label('Initial worker count (new sites only). For installed sites use Add / Remove.', 'Начальное число Workers (новая площадка). Для установленной используйте добавление / удаление.')).pack(anchor='w')
        self.initial_count = ttk.Combobox(p, textvariable=self.worker_count, values=[str(n) for n in range(1, MAX_WORKERS + 1)], width=6, state='readonly')
        self.initial_count.pack(anchor='w', pady=6)
        self.server_tree = ttk.Treeview(p, columns=('address', 'auth'), show='tree headings', height=7, selectmode='browse')
        self.server_tree.heading('#0', text='VM'); self.server_tree.column('#0', width=110, stretch=False)
        self.server_tree.heading('address', text='IPv4'); self.server_tree.column('address', width=180)
        self.server_tree.heading('auth', text=label('Sign-in', 'Вход')); self.server_tree.column('auth', width=120)
        self.server_tree.pack(fill='x'); self.server_tree.bind('<<TreeviewSelect>>', self.select_server)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Add worker…', 'Добавить Worker…', self.add_worker_flow)
        self.button(bar, 'Reinstall selected worker…', 'Переустановить выбранный Worker…', lambda: self.worker_flow('worker-repair'))
        self.button(bar, 'Remove selected worker…', 'Удалить выбранный Worker…', lambda: self.worker_flow('worker-remove'))
        self.connection_editor = ttk.LabelFrame(p, text=label('Selected server', 'Выбранный сервер'), padding=12)
        self.connection_editor.pack(fill='x', pady=10)

    def render_servers(self):
        for role in ('app', 'db', 'ai') + self.visible_workers():
            if role not in self.hostvars:
                defaults = dict(address='', port='22', username='Administrator' if role.startswith('worker') else 'ubuntu', auth='key', key_path='', fingerprint='')
                self.hostvars[role] = {k: tk.StringVar(value=v) for k, v in defaults.items()}
                for v in self.hostvars[role].values(): v.trace_add('write', self.mark_dirty)
        if hasattr(self, 'server_tree'):
            for item in self.server_tree.get_children(): self.server_tree.delete(item)
            for role in ('app', 'db', 'ai') + self.visible_workers():
                v = self.hostvars[role]
                self.server_tree.insert('', 'end', iid=role, text=role.upper(), values=(v['address'].get(), v['auth'].get()))
            if self.editor_role not in self.server_tree.get_children(): self.editor_role = 'app'
            self.server_tree.selection_set(self.editor_role)

    def populate(self, config):
        self._editor_loading = True
        try:
            super().populate(config)
            self.render_servers()
            if hasattr(self, 'connection_editor'): self.edit_server(self.editor_role or 'app')
        finally: self._editor_loading = False

    def read_saved_access(self):
        from .vault import Vault
        try:
            if (self.directory / 'secrets.json').exists():
                v = Vault(self.directory / 'secrets.json', (self.directory / 'secrets.key').read_text('ascii').strip())
                self.auth = v.data.get('ssh', {})
                self.session_log.protect(v.data)
                self.show_certificate(v.data.get('app_tls'))
        except (OSError, ValueError, InvalidToken): pass  # Existing migration/error handling stays authoritative.

    def select_server(self, event=None):
        selected = self.server_tree.selection()
        if self.busy:
            if selected and selected[0] != self.editor_role: self.server_tree.selection_set(self.editor_role)
            return
        if selected and selected[0] != self.editor_role:
            self.capture_access(); self.edit_server(selected[0])

    def edit_server(self, role):
        self.editor_role = role
        for child in self.connection_editor.winfo_children(): child.destroy()
        p = self.connection_editor; v = self.hostvars[role]
        self._editor_loading = True
        ttk.Label(p, text=role.upper()).pack(anchor='w')
        self.field(p, 'Server IPv4 (numeric address)', 'IPv4 сервера (числовой адрес)', v['address'])
        self.field(p, 'SSH port', 'Порт SSH', v['port'])
        self.field(p, 'SSH username', 'Пользователь SSH', v['username'])
        ttk.Label(p, text=label('Sign-in method', 'Способ входа')).pack(anchor='w')
        self.auth_choice = tk.StringVar(value=label('Password', 'Пароль') if v['auth'].get() == 'password' else label('SSH key', 'Ключ SSH'))
        box = ttk.Combobox(p, textvariable=self.auth_choice, values=(label('SSH key', 'Ключ SSH'), label('Password', 'Пароль')), state='readonly')
        box.pack(fill='x', pady=5); self.editors.append(box)
        box.bind('<<ComboboxSelected>>', self.auth_changed)
        self.auth_fields = ttk.Frame(p); self.auth_fields.pack(fill='x')
        self.secret_vars = {name: tk.StringVar(value=self.auth.get(role, {}).get(name, '')) for name in ('password', 'passphrase', 'sudo_password')}
        self.secret_loaded = {name: var.get() for name, var in self.secret_vars.items()}
        for var in self.secret_vars.values(): var.trace_add('write', self.mark_dirty)
        self.draw_auth_fields()
        if not role.startswith('worker'):
            sudo = ttk.LabelFrame(p, text=label('Linux privileges', 'Права Linux'), padding=8); sudo.pack(fill='x', pady=8)
            self.sudo_mode = tk.StringVar(value='root' if v['username'].get() == 'root' else 'password' if self.secret_vars['sudo_password'].get() else 'nopass')
            for value, en, ru in [('root', 'Root account', 'Пользователь root'), ('nopass', 'Passwordless sudo', 'Sudo без пароля'), ('password', 'Sudo with a separate password', 'Sudo с отдельным паролем')]:
                radio = ttk.Radiobutton(sudo, text=label(en, ru), variable=self.sudo_mode, value=value, command=self.draw_sudo)
                radio.pack(anchor='w'); self.editors.append(radio)
            self.sudo_frame = ttk.Frame(sudo); self.sudo_frame.pack(fill='x'); self.draw_sudo()
        identity = ttk.LabelFrame(p, text=label('Server identity', 'Подлинность сервера'), padding=8); identity.pack(fill='x', pady=8)
        fp = v['fingerprint'].get()
        ttk.Label(identity, text=fp or label('Not confirmed. Retrieve and compare the fingerprint before sending credentials.', 'Не подтверждена. Получите и сверьте отпечаток до отправки пароля.'), wraplength=740).pack(anchor='w')
        ttk.Button(identity, text=label('Verify fingerprint…', 'Сверить отпечаток…'), command=lambda: self.credentials(role)).pack(anchor='w', pady=5)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Save server access', 'Сохранить доступ к серверу', self.save_vault)
        self.button(bar, 'Test selected server', 'Проверить выбранный сервер', self.check_server)
        self._editor_loading = False

    def draw_auth_fields(self):
        for child in self.auth_fields.winfo_children(): child.destroy()
        v = self.hostvars[self.editor_role]
        if v['auth'].get() == 'password':
            self.field(self.auth_fields, 'SSH password', 'Пароль SSH', self.secret_vars['password'], secret=True)
        else:
            self.field(self.auth_fields, 'SSH private key file', 'Файл приватного ключа SSH', v['key_path'], browse=lambda: self.pick_key(self.editor_role))
            self.field(self.auth_fields, 'Key passphrase (optional)', 'Пароль ключа (если есть)', self.secret_vars['passphrase'], secret=True)

    def auth_changed(self, event=None):
        if self.busy: return
        self.hostvars[self.editor_role]['auth'].set('password' if self.auth_choice.get() == label('Password', 'Пароль') else 'key')
        self.draw_auth_fields(); self.mark_dirty()

    def draw_sudo(self):
        for child in self.sudo_frame.winfo_children(): child.destroy()
        if self.sudo_mode.get() == 'password':
            self.field(self.sudo_frame, 'Sudo password (may differ from SSH)', 'Пароль sudo (может отличаться от SSH)', self.secret_vars['sudo_password'], secret=True)

    def capture_access(self):
        if not self.editor_role or not self.secret_vars: return
        role = self.editor_role; values = self.auth.setdefault(role, {})
        # Blank explicitly clears a previously loaded value; initial blanks remain blanks.
        name = 'password' if self.hostvars[role]['auth'].get() == 'password' else 'passphrase'
        values[name] = self.secret_vars[name].get()
        if not role.startswith('worker'):
            if self.sudo_mode.get() == 'root' and self.hostvars[role]['username'].get() != 'root':
                # Do not silently convert a non-root account to a privileged login.
                values['sudo_password'] = ''
            else: values['sudo_password'] = self.secret_vars['sudo_password'].get() if self.sudo_mode.get() == 'password' else ''
        self.session_log.protect({'ssh': self.auth})

    def config(self):
        self.capture_access()
        if self.editor_role and not self.editor_role.startswith('worker') and self.sudo_mode.get() == 'root' and self.hostvars[self.editor_role]['username'].get() != 'root':
            raise ValueError(label('Root privileges require SSH username root. Otherwise choose a sudo mode.', 'Для root укажите SSH-пользователя root. Иначе выберите режим sudo.'))
        return super().config()

    def prepare_profile(self):
        saved = self.directory / 'site.json'
        if saved.exists():
            old = runtime_config(saved)
            current = self.config()
            # Once a vault is bound, editing a core endpoint must never silently select a new site.
            if self.profile_is_installed() and (old['site_id'] != current['site_id'] or
                    any(old['hosts'][role]['address'] != current['hosts'][role]['address'] for role in ('app', 'db'))):
                raise ValueError(label('This is an installed profile. Use New site for a different site ID or core VM addresses.', 'Это установленный профиль. Для другого ID или адресов основных VM создайте новую площадку.'))
        result = super().prepare_profile()
        self._draft_dirty = False
        # A successful validated save supersedes the incomplete local draft.
        (self.directory / 'ui-draft.json').unlink(missing_ok=True)
        return result

    def credentials(self, role):
        """Identity verification is separate from the inline login credentials."""
        if self.busy: return
        from .ssh import scan
        try:
            host = dict(address=str(ipaddress.IPv4Address(self.hostvars[role]['address'].get().strip())), port=int(self.hostvars[role]['port'].get()))
            if not 1 <= host['port'] <= 65535: raise ValueError('Invalid SSH port')
        except ValueError as exc:
            messagebox.showerror('SSH', str(exc)); return
        top = tk.Toplevel(self.root); top.title(role + ' — ' + label('Server identity', 'Подлинность сервера')); top.transient(self.root)
        frame = ttk.Frame(top, padding=16); frame.pack(fill='both', expand=True)
        status = tk.StringVar(value=label('Retrieving fingerprint without sending credentials…', 'Получение отпечатка без отправки пароля…'))
        ttk.Label(frame, textvariable=status, wraplength=680).pack(anchor='w')
        pending = queue.Queue(); cancel = threading.Event()
        def task():
            try: pending.put((True, scan(host, progress=lambda text: None, cancel=cancel)))
            except Exception as exc: pending.put((False, str(exc)))
        def poll():
            if not top.winfo_exists(): return
            try: ok, value = pending.get_nowait()
            except queue.Empty: top.after(100, poll); return
            status.set(value)
            if ok:
                ttk.Label(frame, text=label('Compare this SHA256 with the VM console or administrator. Retrieving a fingerprint does not establish trust.', 'Сверьте SHA256 с консолью VM или администратором. Получение отпечатка само по себе не подтверждает доверие.'), wraplength=680).pack(anchor='w', pady=10)
                def trust():
                    if self.busy:
                        status.set(label('Wait for the active operation before changing trust.', 'Дождитесь завершения операции перед изменением доверия.')); return
                    # Refuse a result fetched for an endpoint that has since changed.
                    if (self.hostvars[role]['address'].get().strip(), self.hostvars[role]['port'].get().strip()) != (host['address'], str(host['port'])):
                        messagebox.showerror('SSH', label('Endpoint changed. Retrieve its fingerprint again.', 'Адрес изменён. Получите отпечаток заново.')); return
                    self.hostvars[role]['fingerprint'].set(value); top.destroy(); self.edit_server(role)
                ttk.Button(frame, text=label('I compared it — trust this fingerprint', 'Сверено — доверять этому отпечатку'), command=trust).pack(anchor='w')
        def close(): cancel.set(); top.destroy()
        ttk.Button(frame, text=label('Close', 'Закрыть'), command=close).pack(anchor='e', pady=8)
        top.protocol('WM_DELETE_WINDOW', close)
        threading.Thread(target=task, daemon=True).start(); top.after(100, poll)

    def check_server(self):
        if self.busy: return
        from .ssh import Remote
        import re
        role = self.editor_role
        self.capture_access()
        host = {k: v.get().strip() for k,v in self.hostvars[role].items()}
        try:
            host['address'] = str(ipaddress.IPv4Address(host['address']))
            host['port'] = int(host['port'])
            if not 1 <= host['port'] <= 65535: raise ValueError('Invalid SSH port')
            if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', host['fingerprint']):
                raise ValueError(label('Confirm the server fingerprint first.', 'Сначала подтвердите отпечаток сервера.'))
        except ValueError as exc: messagebox.showerror('SSH', str(exc)); return
        access = dict(self.auth.get(role, {}))
        self.busy = True
        def task():
            remote = None
            try:
                remote = Remote(role, host, access, self.log)
                result = remote.probe()
                self.log(role + ': ' + label('Sign-in and required privileges verified. Host: ', 'Вход и требуемые права проверены. Сервер: ') + str(result.get('hostname', '')))
            except Exception as exc: self.log(tr('ОШИБКА: ') + role + ': ' + str(exc))
            finally:
                if remote is not None: remote.close()
        self.launch_operation('check-server', task)

    def save_vault(self):
        if self.busy: return
        self.capture_access(); super().save_vault()
        self.refresh_sites(); self.render_servers()

    def build_deploy(self):
        p = self.deploy_page
        self.heading(p, 'Install / update', 'Установка / обновление')
        self.deploy_mode = tk.StringVar(value='install')
        bar = ttk.Frame(p); bar.pack(fill='x')
        for mode in ('install', 'update'):
            ttk.Radiobutton(bar, text=label(*MODE_NAMES[mode]), variable=self.deploy_mode, value=mode, command=self.refresh_review).pack(side='left', padx=8)
        self.wizard_label = tk.StringVar()
        ttk.Label(p, textvariable=self.wizard_label).pack(anchor='w', pady=10)
        self.wizard_frames = []
        for _ in range(4): self.wizard_frames.append(ttk.Frame(p))
        site, package, options, review = self.wizard_frames
        ttk.Label(site, text=label('1. Configure servers and site settings, then return here.', '1. Настройте серверы и площадку, затем вернитесь сюда.'), wraplength=800).pack(anchor='w')
        bar = ttk.Frame(site); bar.pack(fill='x')
        self.button(bar, 'Servers & access', 'Серверы и доступ', lambda: self.show_page('servers'))
        self.button(bar, 'App settings', 'Настройки App', lambda: self.show_page('settings'))
        self.field(site, 'Site ID', 'ID площадки', self.vars['site_id'])
        ttk.Label(site, text=label('Installed identity and core addresses must remain consistent with the original profile.', 'Для установленной площадки сохраняйте исходный ID и адреса основных VM.'), wraplength=800).pack(anchor='w', pady=8)
        self.package_fields(package)
        self.ai_update = tk.StringVar(value='keep')
        ttk.Label(options, text=label('AI handling for this update', 'AI при этом обновлении')).pack(anchor='w')
        for val, en, ru in [('keep', 'Keep existing AI unchanged (update only)', 'Сохранить существующий AI (только обновление)'), ('update', 'Install / update AI packages', 'Установить / обновить пакеты AI')]:
            ttk.Radiobutton(options, text=label(en, ru), variable=self.ai_update, value=val, command=self.refresh_review).pack(anchor='w', pady=4)
        ttk.Label(options, text=label('For a new site, AI inclusion is selected on the AI page. Keep existing requires AI already installed; packages are omitted intentionally.', 'Для новой площадки AI включается на странице AI. Сохранение AI означает намеренный пропуск пакетов при обновлении.'), wraplength=800).pack(anchor='w', pady=8)
        self.ai_files(options)
        self.review_text = tk.StringVar()
        ttk.Label(review, textvariable=self.review_text, wraplength=800, justify='left').pack(anchor='w')
        self.button(review, 'Start reviewed action', 'Запустить выбранную операцию', self.run_reviewed)
        bar = ttk.Frame(p); bar.pack(fill='x', pady=12)
        self.button(bar, 'Back', 'Назад', lambda: self.wizard_move(-1))
        self.button(bar, 'Next / review', 'Далее / обзор', lambda: self.wizard_move(1))
        self.show_wizard()

    def package_fields(self, p):
        self.field(p, 'Complete release ZIP / extracted directory', 'Полный релиз ZIP / распакованный каталог', self.release,
                   browse=lambda: self.release.set(filedialog.askopenfilename(filetypes=[('Complete ZIP', '*.zip')]) or self.release.get()))
        ttk.Button(p, text=label('Choose extracted directory…', 'Выбрать распакованный каталог…'), command=lambda: self.release.set(filedialog.askdirectory() or self.release.get())).pack(anchor='w')
        self.field(p, 'Expected SHA256 of release-manifest.json (not ZIP)', 'Ожидаемый SHA256 release-manifest.json (не ZIP)', self.trusted)
        ttk.Label(p, text=label('Get the expected manifest hash from a trusted source. File checks do not establish trust in a hash calculated from the same package.', 'Получите ожидаемый хэш manifest из доверенного источника. Вычисление хэша из пакета не подтверждает доверие к нему.'), wraplength=800).pack(anchor='w', pady=8)
        ttk.Button(p, text=label('Verify package locally', 'Проверить пакет локально'), command=self.verify_package).pack(anchor='w')
        self.package_status = tk.StringVar(value=label('Package not checked.', 'Пакет не проверен.'))
        ttk.Label(p, textvariable=self.package_status, wraplength=800).pack(anchor='w', pady=8)

    def ai_files(self, p):
        self.field(p, 'Ollama runtime components ZIP', 'ZIP компонентов Ollama', self.ai_ollama,
                   browse=lambda: self.ai_ollama.set(filedialog.askopenfilename(filetypes=[('Ollama ZIP', '*.zip')]) or self.ai_ollama.get()))
        self.field(p, 'Combined AI models ZIP (embedding + chat)', 'Общий ZIP моделей AI (embedding + chat)', self.ai_model,
                   browse=lambda: self.ai_model.set(filedialog.askopenfilename(filetypes=[('AI models ZIP', '*.zip'), ('Legacy chat GGUF', '*.gguf')]) or self.ai_model.get()))

    def verify_package(self):
        if self.busy: return
        if not self.release.get():
            messagebox.showerror(tr('Проверка'), tr('Выберите полный релиз ZIP или каталог')); return
        from .release import Release
        path, digest = self.release.get(), self.trusted.get().strip()
        self.busy = True
        self.package_status.set(label('Verifying local files…', 'Проверка локальных файлов…'))
        def task():
            try:
                release = Release(path, self.directory / 'cache', digest, validate_ai=False)
                self.events.put(('package', (path, digest, release.version, release.digest)))
            except Exception as exc:
                self.log(tr('ОШИБКА: ') + str(exc)); self.events.put(('package-error', str(exc)))
            finally: self.events.put(('done', ''))
        self.launch_operation('verify-package', task)

    def wizard_move(self, delta):
        self.wizard_index = max(0, min(3, self.wizard_index + delta)); self.show_wizard()

    def show_wizard(self):
        for frame in self.wizard_frames: frame.pack_forget()
        self.wizard_frames[self.wizard_index].pack(fill='x', pady=8)
        self.wizard_label.set(label('Site → Package → AI options → Review', 'Площадка → Пакет → AI → Обзор') + f'  ({self.wizard_index + 1}/4)')
        self.refresh_review()

    def refresh_review(self):
        if not hasattr(self, 'review_text'): return
        mode = self.deploy_mode.get()
        details = [label(*MODE_NAMES[mode]), label('Package: ', 'Пакет: ') + self.release.get()]
        details += [role + ': ' + self.hostvars[role]['address'].get() for role in ('app', 'db') + self.visible_workers() if role in self.hostvars]
        if mode == 'update':
            details.append(label('App access and scheduling stop during the update. Update backups are created. No automatic DB rollback.', 'На время обновления App и планировщик остановятся. Будут созданы бэкапы. Автоотката БД нет.'))
        details.append(label('AI: keep unchanged', 'AI: сохранить без изменений') if mode == 'update' and self.ai_update.get() == 'keep' else label('AI follows the inclusion setting and selected packages.', 'AI: согласно настройке включения и выбранным пакетам.'))
        details.append(label('Saved LDAP, activation and certificate settings are included by the existing deployment contract.', 'Сохранённые LDAP, активация и сертификат применяются согласно контракту релиза.'))
        pending = pending_operations(self.directory)
        if pending: details.append(label('Unresolved journals: ', 'Незавершённые журналы: ') + ', '.join(x['journal'] for x in pending))
        details.append(label('Final access, release compatibility and ownership checks run before deployment steps. A local review does not prove VM readiness.', 'Перед развёртыванием выполняются проверки доступа, совместимости и владельца. Локальный обзор не подтверждает готовность VM.'))
        self.review_text.set('\n\n'.join(details))

    def run_reviewed(self):
        if self.busy: return
        mode = self.deploy_mode.get()
        if mode == 'update' and self.ai_update.get() == 'keep':
            # Capture omission at start(): Engine's explicit skip path preserves existing AI.
            model, runtime = self.ai_model.get(), self.ai_ollama.get()
            self.ai_model.set(''); self.ai_ollama.set('')
            try: self.start(mode)
            finally: self.ai_model.set(model); self.ai_ollama.set(runtime)
        else: self.start(mode)

    def build_ai(self):
        p = self.ai_page
        self.heading(p, 'AI setup and recovery', 'Настройка и восстановление AI')
        check = ttk.Checkbutton(p, text=label('Include AI in this site', 'Включить AI в этой площадке'), variable=self.install_ai)
        check.pack(anchor='w'); self.editors.append(check)
        ttk.Label(p, text=label('This setting describes the site. During a core update, choose separately whether to keep existing AI. GPU drivers must already be installed; Manager can use CPU.', 'Эта настройка относится к площадке. При обновлении отдельно выберите сохранение AI. Драйвер GPU должен быть установлен заранее; доступен CPU.'), wraplength=800).pack(anchor='w', pady=10)
        self.ai_files(p)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Install / retry AI', 'Установить / повторить AI', lambda: self.start('ai'))
        self.button(bar, 'AI operation logs', 'Журналы AI', lambda: self.show_page('operations'))
        ttk.Label(p, text=label('Uses the complete release selected in Install / update. Retry requires the runtime/model packages required by that release.', 'Используется полный релиз, выбранный в мастере установки. Повтор требует пакеты компонентов и моделей, указанные в контракте релиза.'), wraplength=800).pack(anchor='w', pady=10)

    def toggle_ai(self):
        # Keep access editors readable. Engine decides whether the optional endpoint participates.
        if hasattr(self, 'overview_text'): self.refresh_overview()

    def build_settings(self):
        p = self.settings_page
        self.heading(p, 'App settings', 'Настройки App')
        ttk.Label(p, text=label('Save locally first. LDAP and activation take effect on installation/update. Certificate deployment is a separate reviewed action.', 'Сначала сохраните настройки локально. LDAP и активация применяются при установке/обновлении. Применение сертификата — отдельная операция.'), wraplength=800).pack(anchor='w', pady=8)
        notebook = ttk.Notebook(p); notebook.pack(fill='both', expand=True)
        general = ttk.Frame(notebook, padding=12); https = ttk.Frame(notebook, padding=12)
        integrations = ttk.Frame(notebook, padding=12)
        for frame, en, ru in [(general, 'General & network', 'Основные и сеть'), (https, 'HTTPS certificate', 'Сертификат HTTPS'), (integrations, 'Domain login & activation', 'Доменный вход и активация')]: notebook.add(frame, text=label(en, ru))
        for key, en, ru in [('public_url', 'Internal App HTTPS URL (browser URL is in certificate settings)', 'Внутренний HTTPS App (адрес сайта — в сертификате)'),
                            ('admin_username', 'Initial App administrator username', 'Начальный логин администратора App'), ('admin_email', 'Administrator email', 'Email администратора'),
                            ('admin_ip', 'Management PC IPv4 used for access rules', 'IPv4 ПК управления для правил доступа'), ('app_subnet', 'Docker network /24 (must not overlap VMs)', 'Подсеть Docker /24 (без пересечения с VM)')]: self.field(general, en, ru, self.vars[key])
        ttk.Label(general, text=label('Initial administrator settings do not edit an existing App account. Core VM addresses and site identity must not be casually changed.', 'Начальные параметры администратора не изменяют существующий аккаунт App. Сохраняйте исходные адреса основных VM и ID площадки.'), wraplength=760).pack(anchor='w', pady=8)
        ttk.Button(general, text=label('Save local configuration', 'Сохранить локальную конфигурацию'), command=self.save).pack(anchor='w', pady=5)
        self.certificate_form(https)
        reveal = tk.BooleanVar(value=False)
        # Add reveal beside the PFX password without changing the established importer.
        def reveal_pfx():
            for widget in https.grid_slaves(row=4, column=1):
                if isinstance(widget, ttk.Entry): widget.configure(show='' if reveal.get() else '•')
        ttk.Checkbutton(https, text=label('Show', 'Показать'), variable=reveal, command=reveal_pfx).grid(row=4,column=2)
        ttk.Label(https, text=label('Browser trust troubleshooting: DNS target → presented certificate → hostname / validity → CA chain → client trusted-root store. Importing a PFX does not install client trust.', 'Если браузер не доверяет сайту: DNS → сертификат сервера → имя / срок → цепочка CA → доверенный корневой CA на клиенте. Импорт PFX не устанавливает доверие на ПК.'), wraplength=680).grid(row=11,column=0,columnspan=3,sticky='w',pady=10)
        integration_bar = ttk.Frame(integrations); integration_bar.pack(fill='x')
        self.button(integration_bar, 'Domain login (LDAP / AD)…', 'Доменный вход (LDAP / AD)…', self.directory_dialog)
        self.button(integration_bar, 'App activation…', 'Активация App…', self.activation_dialog)
        ttk.Label(integrations, text=label('Domain login controls authentication in App. It does not join workers to a domain or change their service account.', 'Доменный вход относится к App. Он не вводит Workers в домен и не меняет пользователя службы.'), wraplength=760).pack(anchor='w', pady=12)

    def build_backups(self):
        p = self.backups_page
        self.heading(p, 'Backups & recovery', 'Бэкапы и восстановление')
        for en, ru, detail_en, detail_ru, command in [
            ('Create server backup', 'Создать бэкап на серверах', 'Create backups on the VMs. Review the affected services before proceeding.', 'Бэкапы остаются на VM. Перед запуском проверьте влияние на сервисы.', lambda: self.start('backup')),
            ('Export encrypted .pcbackup…', 'Выгрузить зашифрованный .pcbackup…', 'A portable archive with its own password (12+ characters). Keep that password separately.', 'Переносимый архив с отдельным паролем (от 12 символов). Сохраните пароль отдельно.', lambda: self.maintenance('export')),
            ('Restore data from archive…', 'Восстановить данные из архива…', 'Replaces destination data. App and workers stop temporarily; not the same as component repair.', 'Заменяет текущие данные. App и Workers временно остановятся; это не ремонт компонентов.', lambda: self.maintenance('restore')),
            ('Recover Manager profile…', 'Восстановить профиль Manager…', 'Recovers local configuration. On clean VMs, install the original release first, then restore data.', 'Восстанавливает локальную конфигурацию. На чистых VM сначала установите исходный релиз, затем восстановите данные.', lambda: self.maintenance('import')),
            ('Repair installed components…', 'Восстановить установленные компоненты…', 'Requires the exact installed release selected in Install / update. Retains data; may restart services.', 'Выберите точный установленный релиз в мастере. Данные сохранятся; возможен перезапуск сервисов.', lambda: self.start('repair')),
            ('Advanced: reset managed VMs…', 'Дополнительно: сброс компонентов VM…', 'Choose keep-data recreation or full ProdCast data deletion. OS, SSH, networking and server backups remain.', 'Пересоздание с данными либо удаление данных ProdCast. ОС, SSH, сеть и бэкапы на VM остаются.', self.reset_dialog),
        ]:
            frame = ttk.LabelFrame(p, text=label(en, ru), padding=10); frame.pack(fill='x', pady=6)
            ttk.Label(frame, text=label(detail_en, detail_ru), wraplength=780).pack(anchor='w')
            ttk.Button(frame, text=label('Review…', 'Обзор…'), command=command).pack(anchor='w', pady=5)

    def build_operations(self):
        p = self.operations_page
        self.heading(p, 'Operations and logs', 'Операции и журналы')
        self.operation_tree = ttk.Treeview(p, columns=('mode', 'status'), show='tree headings', height=5, selectmode='browse')
        self.operation_tree.heading('#0', text=label('Started / session', 'Начало / сеанс')); self.operation_tree.column('#0', width=190)
        self.operation_tree.heading('mode', text=label('Operation', 'Операция')); self.operation_tree.column('mode', width=200)
        self.operation_tree.heading('status', text=label('Local result', 'Локальный результат')); self.operation_tree.column('status', width=130)
        self.operation_tree.pack(fill='x'); self.operation_tree.bind('<<TreeviewSelect>>', lambda e: self.refresh_log())
        self.pending_text = tk.StringVar()
        ttk.Label(p, textvariable=self.pending_text, wraplength=820).pack(anchor='w', pady=8)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Refresh history', 'Обновить историю', self.refresh_operations)
        self.button(bar, 'Recovery options…', 'Варианты восстановления…', self.recovery_options)
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.log_target = tk.StringVar(value='')
        self.target_selector = ttk.Combobox(bar, textvariable=self.log_target, state='readonly', width=16)
        self.target_selector.pack(side='left'); self.target_selector.bind('<<ComboboxSelected>>', lambda e: self.refresh_log())
        self.summary_only = tk.BooleanVar(value=False); self.follow_log = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=label('Summary (first lines)', 'Кратко (первые строки)'), variable=self.summary_only, command=self.refresh_log).pack(side='left', padx=6)
        ttk.Checkbutton(bar, text=label('Follow output', 'Следовать за выводом'), variable=self.follow_log).pack(side='left', padx=6)
        logframe = ttk.Frame(p); logframe.pack(fill='both', expand=True, pady=5)
        scroll = ttk.Scrollbar(logframe); scroll.pack(side='right', fill='y')
        self.output = tk.Text(logframe, wrap='word', height=12, state='disabled', exportselection=False, yscrollcommand=scroll.set, font=('Consolas', 10))
        self.output.pack(side='left', fill='both', expand=True); scroll.configure(command=self.output.yview)
        self.output.bind('<Control-KeyPress>', self.log_shortcut); self.output.bind('<<Copy>>', lambda e: self.copy_log())
        menu = tk.Menu(self.output, tearoff=False)
        for text, command in [('Копировать выделенное', self.copy_log), ('Копировать всё', lambda: self.copy_log(all_text=True)), ('Выделить всё', self.select_log)]: menu.add_command(label=tr(text), command=command)
        self.output.bind('<Button-3>', lambda e: menu.tk_popup(e.x_root, e.y_root))
        bar = ttk.Frame(p); bar.pack(fill='x')
        self.button(bar, 'Copy selected', 'Копировать выделенное', self.copy_log)
        self.button(bar, 'Copy visible log', 'Копировать видимый журнал', lambda: self.copy_log(all_text=True))
        self.button(bar, 'Open logs folder', 'Открыть папку журналов', self.open_logs)
        self.operation_detail = tk.StringVar()
        ttk.Label(p, textvariable=self.operation_detail, wraplength=820).pack(anchor='w')

    def maintenance(self, mode):
        if self.busy: return
        if mode not in ('export', 'restore', 'import'): return super().maintenance(mode)
        from .maintenance import Maintenance, import_backup_profile
        from .engine import Engine
        from .config import validate
        import uuid
        top = tk.Toplevel(self.root)
        title = label('Recover Manager profile', 'Восстановить профиль Manager') if mode == 'import' else label(*MODE_NAMES[mode])
        top.title(title); top.transient(self.root)
        frame = ttk.Frame(top, padding=18); frame.pack(fill='both', expand=True)
        path = tk.StringVar(); password = tk.StringVar(); confirmation = tk.StringVar()
        def browse():
            options = dict(parent=top, filetypes=[('ProdCast backup', '*.pcbackup')])
            value = filedialog.asksaveasfilename(defaultextension='.pcbackup', **options) if mode == 'export' else filedialog.askopenfilename(**options)
            if value: path.set(value)
        self.field(frame, 'Encrypted .pcbackup archive', 'Зашифрованный архив .pcbackup', path, browse=browse)
        self.field(frame, 'Archive password (12+ characters)', 'Пароль архива (от 12 символов)', password, secret=True)
        if mode == 'export': self.field(frame, 'Confirm archive password', 'Повторите пароль архива', confirmation, secret=True)
        notice = label('Export creates a new encrypted portable archive. Keep its password separately. It cannot be recovered.', 'Выгрузка создаёт новый зашифрованный переносимый архив. Сохраните пароль отдельно; восстановить его нельзя.') if mode == 'export' else label('Restore replaces current data and stops App / workers temporarily. The archive must match this installation and installed release. Existing data is not automatically rolled back.', 'Восстановление заменит текущие данные и временно остановит App / Workers. Архив должен соответствовать установке и установленному релизу. Автоматического отката нет.') if mode == 'restore' else label('This imports the Manager profile into a new local folder. It does not install software on clean VMs. Install the original release before restoring data.', 'Профиль Manager импортируется в новую локальную папку. ПО на чистые VM не устанавливается. Перед восстановлением данных установите исходный релиз.')
        ttk.Label(frame, text=notice, wraplength=700).pack(anchor='w', pady=12)
        ttk.Label(frame, text=label('Destination site: ', 'Целевая площадка: ') + str(self.directory), wraplength=700).pack(anchor='w')
        status = tk.StringVar()
        ttk.Label(frame, textvariable=status, wraplength=700).pack(anchor='w', pady=8)
        def execute():
            if self.busy: return
            archive, phrase = path.get(), password.get()
            try:
                if not archive: raise ValueError(label('Choose an archive path.', 'Выберите путь архива.'))
                if len(phrase) < 12: raise ValueError(tr('Пароль должен содержать минимум 12 символов.'))
                if mode == 'export' and phrase != confirmation.get(): raise ValueError(tr('Пароли не совпадают.'))
                if mode == 'export' and Path(archive).exists():
                    pending_path = self.directory / 'maintenance-journal.json'
                    pending = json.loads(pending_path.read_text('utf-8')) if pending_path.exists() else {}
                    if not (pending.get('status') in ('running','failed') and pending.get('mode') == 'export' and pending.get('backup_destination') == str(Path(archive).resolve())):
                        raise ValueError(tr('Выберите новое имя файла резервной копии.'))
                if mode == 'import':
                    destination = data_directory(self.home) / 'sites' / ('recovered-' + uuid.uuid4().hex[:12])
                    config = import_backup_profile(archive, phrase, data_directory(self.home) / 'temporary', destination)
                    self.directory = destination; self.auth = {}; self.secret_vars = {}; self.editor_role = None
                    self.populate(config); self.edit_server('app'); remember(self.home, destination)
                    self.refresh_sites(); self.refresh_overview(); top.destroy()
                    self.log(label('Profile recovered. Supply SSH access before restoring data.', 'Профиль восстановлен. Укажите SSH-доступ перед восстановлением данных.')); return
                config = self.prepare_profile(); validate(config, True); vault = self.vault(); vault.save()
                targets = '\n'.join(r + ': ' + h['address'] for r,h in config['hosts'].items() if r != 'ai')
                if not messagebox.askyesno(title, targets + '\n\n' + notice, parent=top): return
                directory = self.directory
                password.set(''); confirmation.set(''); top.destroy(); self.busy = True
                def task():
                    try: Maintenance(Engine(config, vault, None, directory, self.log, diagnostics=self.session_log)).run(mode, archive, phrase)
                    except Exception as exc: self.log(tr('ОШИБКА: ') + str(exc))
                self.launch_operation(mode, task)
            except Exception as exc: status.set(str(exc))
        ttk.Button(frame, text=label('Review and continue', 'Проверить и продолжить'), command=execute).pack(anchor='e')
        top.grab_set()

    def refresh_operations(self, select=None):
        old = self.operation_tree.selection()
        items = self.history.list(self.directory)
        self.operation_records = {item['id']: item for item in items}
        for identity in self.operation_tree.get_children(): self.operation_tree.delete(identity)
        self.operation_tree.insert('', 'end', iid='session', text=label('Current Manager session', 'Текущий сеанс Manager'), values=(label('All messages', 'Все сообщения'), ''))
        for item in items:
            status = item['status']
            if status == 'running' and (not self.active_operation or self.active_operation['id'] != item['id']): status = 'interrupted / unknown'
            name = label(*MODE_NAMES.get(item['mode'], (item['mode'], item['mode'])))
            self.operation_tree.insert('', 'end', iid=item['id'], text=item['started'], values=(name, status))
        selected = select or (old[0] if old and self.operation_tree.exists(old[0]) else 'session')
        self.operation_tree.selection_set(selected)
        pending = pending_operations(self.directory)
        self.pending_text.set(label('Unresolved deployment journals: ', 'Незавершённые журналы развёртывания: ') + ('; '.join(x['journal'] + ': ' + x['status'] for x in pending) or label('none', 'нет')))
        self.refresh_log()

    def refresh_log(self):
        selected = self.operation_tree.selection()
        identity = selected[0] if selected else 'session'
        item = self.operation_records.get(identity)
        events = self.history.events(identity) if item else self.session_messages
        all_targets = label('All targets', 'Все серверы')
        values = [all_targets] + sorted({e['target'] for e in events if e.get('target')})
        target = self.log_target.get()
        if target not in values: target = all_targets; self.log_target.set(target)
        self.target_selector.configure(values=values)
        text = '\n'.join(filtered_events(events, '' if target == all_targets else target, self.summary_only.get()))
        position = self.output.yview()
        self.output.config(state='normal'); self.output.delete('1.0', 'end'); self.output.insert('end', text); self.output.config(state='disabled')
        if self.follow_log.get(): self.output.see('end')
        else: self.output.yview_moveto(position[0])
        self.operation_detail.set((item['mode'] + ' · ' + item['status'] + '\n' + item.get('release', '') + '\n' + str(self.history.directory / (identity + '.jsonl'))) if item else str(self.session_log.path))

    def recovery_options(self):
        pending = pending_operations(self.directory)
        top = tk.Toplevel(self.root); top.title(label('Pending operations', 'Незавершённые операции')); top.transient(self.root)
        frame = ttk.Frame(top, padding=18); frame.pack(fill='both', expand=True)
        advice = {'journal.json': label('Repeat the same install/update with its original release.', 'Повторите исходную установку/обновление с исходным релизом.'),
                  'ai-journal.json': label('Retry AI with its required packages, or use stop below.', 'Повторите AI с нужными пакетами либо используйте остановку ниже.'),
                  'workers-journal.json': label('Resume Add workers with the original release and addresses.', 'Продолжите добавление Workers с исходным релизом и адресами.'),
                  'worker-action-journal.json': label('Repeat the same worker repair/remove action and target.', 'Повторите ту же переустановку/удаление того же Worker.'),
                  'app-tls-journal.json': label('Repeat Apply certificate.', 'Повторите применение сертификата.'),
                  'maintenance-journal.json': label('Repeat the original backup/restore/reset action.', 'Повторите исходную операцию бэкапа/восстановления/сброса.')}
        for item in pending:
            ttk.Label(frame, text=item['journal'] + ' · ' + item['status'] + '\n' + ' · '.join(str(item[k]) for k in ('mode','role','phase','operation') if item[k]) + '\n' + advice[item['journal']], wraplength=720).pack(anchor='w', pady=8)
        if not pending: ttk.Label(frame, text=label('No pending local journals. This does not verify remote ownership.', 'Незавершённых локальных журналов нет. Владельцы на VM не проверялись.')).pack(anchor='w')
        ttk.Label(frame, text=label('Stop releases core/AI ownership only where supported. It is not rollback and does not clear worker, certificate or maintenance journals. Unresolved journals remain visible.', 'Остановка освобождает владельцев основной/AI операции, если поддерживается. Это не откат; журналы Workers, сертификата и обслуживания не очищаются. Незавершённые журналы останутся видимыми.'), wraplength=720).pack(anchor='w', pady=12)
        ttk.Button(frame, text=tr('Остановить предыдущую операцию'), command=lambda: (top.destroy(), self.stop_previous())).pack(anchor='w')
        ttk.Button(frame, text=label('Close', 'Закрыть'), command=top.destroy).pack(anchor='e', pady=8)

    def launch_operation(self, mode, task):
        try:
            targets = ('app','db') + self.visible_workers() + (('ai',) if self.install_ai.get() else ())
            item = self.history.begin(mode, self.directory, targets, self.release.get())
        except OSError as exc:
            self.busy = False; messagebox.showerror(tr('Логи'), str(exc)); return
        self.active_operation = item
        self.log_target.set('')
        self.refresh_operations(select=item['id']); self.show_page('operations')
        self.lock_editors(True); self.refresh_overview()
        def run():
            self.operation_thread.item = item
            try: task()
            except Exception as exc: self.log(tr('ОШИБКА: ') + str(exc))
            finally:
                try: self.history.finish(item)
                except OSError as exc: self.log(tr('ОШИБКА: ') + str(exc))
                self.operation_thread.item = None
                self.events.put(('operation-finished', item['id']))
        threading.Thread(target=run, daemon=False).start()

    def lock_editors(self, locked):
        def descendants(parent):
            for child in parent.winfo_children():
                yield child
                yield from descendants(child)
        widgets = [widget for key, page in self.pages.items() if key != 'operations' for widget in descendants(page)
                   if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Checkbutton, ttk.Radiobutton, ttk.Button))]
        for widget in widgets:
            try:
                if locked:
                    if not hasattr(widget, '_workspace_state'): widget._workspace_state = widget.cget('state')
                    widget.configure(state='disabled')
                elif hasattr(widget, '_workspace_state'):
                    widget.configure(state=widget._workspace_state); del widget._workspace_state
            except tk.TclError: pass

    def log(self, text):
        super().log(text)
        item = getattr(self.operation_thread, 'item', None)
        if item:
            try: self.history.append(item, self.session_log.redact(text))
            except OSError: pass  # Session log remains the fallback diagnostic source.

    def poll(self):
        from datetime import datetime
        changed = False
        while not self.events.empty():
            kind, value = self.events.get()
            if kind == 'done':
                # Worker task's legacy completion arrives before the history finalizer.
                if self.active_operation is None: self.busy = False
            elif kind == 'operation-finished':
                self.busy = False; self.active_operation = None; self.lock_editors(False)
                self.refresh_operations(); self.refresh_overview()
            elif kind == 'profile':
                self.populate(value); self.refresh_sites()
                if self.busy: self.lock_editors(True)
            elif kind == 'package':
                self.checked_release = value
                self.package_status.set(label('Verified package version: ', 'Проверена версия пакета: ') + value[2] + '\nSHA256 manifest: ' + value[3])
            elif kind == 'package-error': self.package_status.set(value); self.checked_release = None
            else:
                self.session_messages.append(dict(time=datetime.now().astimezone().isoformat(timespec='seconds'), target=target_of(value), text=value))
                changed = True
        if changed: self.refresh_log()
        self.root.after(100, self.poll)

    def mark_dirty(self, *_):
        if self._loading_profile or self._editor_loading: return
        self._draft_dirty = True
        if hasattr(self, 'workspace_status'): self.refresh_overview()
        if hasattr(self, 'checked_release'): self.checked_release = None

    @staticmethod
    def settings_signature(config, secrets, key):
        # HMAC prevents exposing low-entropy credential hashes in the public UI receipt.
        value = dict(config={k: v for k,v in config.items() if k not in ('hosts', '_worker_topology_hosts')},
                     directory=secrets.get('directory'), activation=secrets.get('activation'), app_tls=secrets.get('app_tls'))
        return hmac.new(key.encode(), json.dumps(value, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def record_applied(self, mode, config, vault, release, directory):
        if mode not in ('install', 'update'): return
        try:
            journal = json.loads((directory / 'journal.json').read_text('utf-8'))
            if journal.get('status') != 'complete': return
            key = (directory / 'secrets.key').read_text('ascii').strip()
            atomic_json(directory / 'ui-deployed.json', dict(version=release.version, manifest=release.digest,
                        operation=journal.get('operation'), settings_signature=self.settings_signature(config, vault.data, key)))
        except (OSError, ValueError) as exc:
            self.log(label('Deployment finished; could not save the UI receipt: ', 'Развёртывание завершено; не удалось сохранить квитанцию UI: ') + str(exc))

    def save_draft(self):
        if self.busy: return
        self.capture_access()
        # Draft never contains passwords, passphrases or key material.
        payload = dict(values={k: v.get() for k, v in self.vars.items()},
                       hosts={r: {k: v.get() for k, v in values.items()} for r, values in self.hostvars.items()},
                       worker_count=self.worker_count.get(), install_ai=self.install_ai.get(),
                       release=self.release.get(), trusted=self.trusted.get(), ai_model=self.ai_model.get(), ai_ollama=self.ai_ollama.get())
        atomic_json(self.directory / 'ui-draft.json', payload)
        self._draft_dirty = False; self.refresh_overview()
        messagebox.showinfo(label('Local draft', 'Локальный черновик'), label('Draft saved. Passwords are saved separately using Save server access. No server changed.', 'Черновик сохранён. Пароли сохраняются отдельно кнопкой сохранения доступа. VM не изменялись.'))

    def restore_draft(self):
        path = self.directory / 'ui-draft.json'
        if not path.exists(): return
        try:
            value = json.loads(path.read_text('utf-8'))
            count = int(value['worker_count'])
            if not 1 <= count <= MAX_WORKERS: raise ValueError('Invalid draft count')
            self.worker_count.set(str(count))
            for k, var in self.vars.items(): var.set(value.get('values', {}).get(k, var.get()))
            for r, fields in self.hostvars.items():
                for k, var in fields.items(): var.set(value.get('hosts', {}).get(r, {}).get(k, var.get()))
            self.install_ai.set(value.get('install_ai', False))
            for key in ('release', 'trusted', 'ai_model', 'ai_ollama'): getattr(self, key).set(value.get(key, ''))
            self.render_servers(); self.edit_server(self.editor_role or 'app')
        except (OSError, ValueError, KeyError, TypeError) as exc: self.log(label('Could not load local draft: ', 'Не удалось прочитать черновик: ') + str(exc))

    def refresh_sites(self):
        root = data_directory(self.home) / 'sites'
        self.site_paths = {p.parent.name: p.parent for p in root.glob('*/site.json')}
        self.site_paths.setdefault(self.directory.name, self.directory)
        self.site_selector.configure(values=sorted(self.site_paths))
        self.site_choice.set(self.directory.name)
        self.initial_count.configure(state='disabled' if self.profile_is_installed() else 'readonly')

    def profile_is_installed(self):
        from .vault import Vault
        if not (self.directory / 'secrets.json').exists(): return False
        try:
            vault = Vault(self.directory / 'secrets.json', (self.directory / 'secrets.key').read_text('ascii').strip())
            return bool(vault.data.get('topology'))
        except (OSError, ValueError, InvalidToken): return True  # Unknown ownership must not silently migrate.

    def discard_or_save(self):
        if self.busy: return False
        if not self._draft_dirty: return True
        result = messagebox.askyesnocancel(label('Unsaved draft', 'Несохранённый черновик'), label('Save draft before leaving? Passwords need Save server access.', 'Сохранить черновик перед выходом? Для паролей используйте сохранение доступа.'))
        if result is None: return False
        if result: self.save_draft()
        return True

    def choose_site(self, event=None):
        selected = self.site_paths[self.site_choice.get()]
        if selected == self.directory: return
        if not self.discard_or_save(): self.site_choice.set(self.directory.name); return
        self._loading_profile = True
        try:
            self.directory = selected; self.auth = {}; self.secret_vars = {}; self.editor_role = None
            self.populate(runtime_config(selected / 'site.json')); self.read_saved_access(); self.restore_draft()
            self.edit_server('app'); remember(self.home, selected); self._draft_dirty = False
            self.refresh_overview(); self.refresh_operations()
        finally: self._loading_profile = False

    def pick_directory(self):
        if not self.discard_or_save(): return
        self._loading_profile = True
        try:
            super().pick_directory(); self.secret_vars = {}; self.editor_role = 'app'; self.edit_server('app')
            self._draft_dirty = False; self.refresh_sites(); self.refresh_overview(); self.deploy_mode.set('install')
        finally: self._loading_profile = False

    def load(self):
        if not self.discard_or_save(): return
        self._loading_profile = True
        try:
            super().load(); self.read_saved_access(); self.edit_server('app'); self.restore_draft()
            self._draft_dirty = False; self.refresh_sites(); self.refresh_overview()
        finally: self._loading_profile = False

    def save(self):
        if self.busy: return
        super().save(); self.refresh_sites(); self.refresh_overview()

    def add_worker_flow(self):
        if self.busy: return
        if not (self.directory / 'site.json').exists():
            messagebox.showerror(tr('Проверка'), tr('Откройте профиль установленной площадки.')); return
        current = len(worker_roles(runtime_config(self.directory / 'site.json')))
        if current >= MAX_WORKERS:
            messagebox.showinfo('Workers', str(MAX_WORKERS)); return
        self.worker_count.set(str(current + 1)); self.render_servers()
        new = next(r for r in self.visible_workers() if r not in worker_roles(runtime_config(self.directory / 'site.json')))
        self.server_tree.selection_set(new); self.edit_server(new)
        self.worker_prerequisites('add-workers')

    def worker_flow(self, mode):
        if self.busy: return
        if not self.editor_role or not self.editor_role.startswith('worker'):
            messagebox.showinfo('Workers', label('Select a worker row first.', 'Сначала выберите строку Worker.')); return
        self.selected_worker.set(self.editor_role)
        if mode == 'worker-remove': self.worker_action(mode)
        else: self.worker_prerequisites(mode)

    def worker_prerequisites(self, mode):
        top = tk.Toplevel(self.root); top.title(label('Worker action prerequisites', 'Подготовка операции Worker')); top.transient(self.root)
        frame = ttk.Frame(top, padding=16); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=label('Configure the selected server access on the page behind this window. Select the exact installed release here. Only the reviewed worker action runs; do not press Install again.', 'Настройте доступ выбранного сервера на странице. Здесь выберите точный установленный релиз. Запускается только операция Worker; повторная установка стека не нужна.'), wraplength=680).pack(anchor='w')
        self.field(frame, 'Exact installed complete release ZIP', 'ZIP точного установленного релиза', self.release, browse=lambda: self.release.set(filedialog.askopenfilename(parent=top, filetypes=[('Complete ZIP', '*.zip')]) or self.release.get()))
        self.field(frame, 'Expected manifest SHA256', 'Ожидаемый SHA256 manifest', self.trusted)
        def run():
            top.destroy()
            if mode == 'add-workers': self.add_workers()
            else: self.worker_action(mode)
        ttk.Button(frame, text=label('Review and continue', 'Проверить и продолжить'), command=run).pack(anchor='e', pady=8)

    def change_language(self, event=None):
        super().change_language(event)
        if not self.busy:
            # Translated display values must not change the persisted auth choice.
            self.capture_access()
            self.edit_server(self.editor_role or 'app')
            self.refresh_operations(); self.refresh_overview(); self.refresh_review()

    def close(self):
        if self.busy: return super().close()
        if self.discard_or_save(): self.root.destroy()
