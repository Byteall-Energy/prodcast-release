"""Domain-login editor with transport-specific fields and separate test results."""
import queue
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlsplit
from .i18n import tr
from .workspace import label


def build(dialog, parent, saved, on_save, on_probe, on_busy):
    dialog.title(tr('LDAP / Active Directory')); dialog.transient(parent)
    dialog.resizable(True, True)
    dialog.on_save = on_save; dialog.on_probe = on_probe; dialog.on_busy = on_busy
    dialog.busy = False; dialog.events = queue.Queue(); dialog.ca = saved.get('ca_pem', '')
    dialog.protocol('WM_DELETE_WINDOW', dialog.close)
    outer = ttk.Frame(dialog, padding=16); outer.pack(fill='both', expand=True)
    ttk.Label(outer, text=label('App domain login · saved locally, applied on installation/update', 'Доменный вход App · сохраняется локально, применяется при установке/обновлении'), wraplength=720).pack(anchor='w')
    dialog.enabled = tk.BooleanVar(value=saved.get('enabled', False))
    ttk.Checkbutton(outer, text=tr('Включить доменный вход в App'), variable=dialog.enabled).pack(anchor='w', pady=8)
    tabs = ttk.Notebook(outer); tabs.pack(fill='both', expand=True)
    connection = ttk.Frame(tabs, padding=12); access = ttk.Frame(tabs, padding=12); tests = ttk.Frame(tabs, padding=12)
    for p, en, ru in [(connection, 'Connection & security', 'Соединение и защита'), (access, 'Search & allowed group', 'Поиск и группа допуска'), (tests, 'User test', 'Проверка пользователя')]: tabs.add(p, text=label(en, ru))
    dialog.inputs = []; dialog.values = {}
    def field(p, key, en, ru, secret=False):
        ttk.Label(p, text=label(en, ru)).pack(anchor='w', pady=(6, 0))
        row = ttk.Frame(p); row.pack(fill='x')
        var = dialog.values.setdefault(key, tk.StringVar(value=saved.get(key, '')))
        entry = ttk.Entry(row, textvariable=var, show='*' if secret else '', width=62)
        entry.pack(side='left', fill='x', expand=True); dialog.inputs.append(entry)
        if secret:
            show = tk.BooleanVar()
            check = ttk.Checkbutton(row, text=label('Show', 'Показать'), variable=show, command=lambda: entry.configure(show='' if show.get() else '*'))
            check.pack(side='left', padx=4); dialog.inputs.append(check)
        return var
    url = urlsplit(saved.get('url', '') or 'ldaps://:636')
    dialog.values['url'] = tk.StringVar(value=saved.get('url', ''))
    transport = tk.StringVar(value='ldap' if url.scheme == 'ldap' else 'ldaps')
    ttk.Label(connection, text=label('Transport', 'Протокол')).pack(anchor='w')
    for value, text in [('ldaps', 'LDAPS / TLS'), ('ldap', label('LDAP / unencrypted', 'LDAP / без шифрования'))]:
        button = ttk.Radiobutton(connection, text=text, variable=transport, value=value)
        button.pack(anchor='w'); dialog.inputs.append(button)
    host = tk.StringVar(value=url.hostname or '')
    try: port_value = str(url.port or (389 if url.scheme == 'ldap' else 636))
    except ValueError: port_value = ''
    port = tk.StringVar(value=port_value)
    for text, var in [(label('LDAP server DNS name', 'DNS-имя LDAP сервера'), host), (label('Port', 'Порт'), port)]:
        ttk.Label(connection, text=text).pack(anchor='w', pady=(6, 0))
        entry = ttk.Entry(connection, textvariable=var); entry.pack(fill='x'); dialog.inputs.append(entry)
    ca = ttk.Frame(connection)
    bar = ttk.Frame(ca); bar.pack(fill='x', pady=6)
    dialog.add_button(bar, 'Загрузить CA (.cer / .crt / .pem)…', dialog.pick_ca)
    dialog.add_button(bar, 'Использовать системные CA', dialog.clear_ca)
    dialog.ca_status = tk.StringVar()
    ttk.Label(ca, textvariable=dialog.ca_status, wraplength=700).pack(anchor='w')
    plain = ttk.Frame(connection)
    dialog.allow_plain_ldap = tk.BooleanVar(value=saved.get('allow_plain_ldap', False))
    check = ttk.Checkbutton(plain, text=tr('Разрешить ldap:// без TLS: пароли передаются без шифрования (не рекомендуется)'), variable=dialog.allow_plain_ldap)
    check.pack(anchor='w'); dialog.inputs.append(check)
    def update(*_, reset_port=False):
        if reset_port: port.set('389' if transport.get() == 'ldap' else '636')
        dialog.values['url'].set(transport.get() + '://' + host.get().strip() + ':' + port.get().strip())
        ca.pack_forget(); plain.pack_forget()
        (plain if transport.get() == 'ldap' else ca).pack(fill='x', pady=8)
        dialog.update_ca_status()
    host.trace_add('write', update); port.trace_add('write', update)
    for child in connection.winfo_children():
        if isinstance(child, ttk.Radiobutton): child.configure(command=lambda: update(reset_port=True))
    update()
    dialog.add_button(connection, 'Проверить LDAP / LDAPS с App VM', lambda: dialog.probe('connection'))
    field(access, 'domain', 'User domain', 'Домен пользователей')
    field(access, 'base_dn', 'Search Base DN', 'Base DN поиска')
    field(access, 'group_dn', 'Allowed group DN', 'DN группы допуска')
    field(access, 'bind_dn', 'Directory lookup account (UPN / DN)', 'Учётная запись поиска (UPN / DN)')
    field(access, 'bind_password', 'Lookup account password', 'Пароль учётной записи поиска', True)
    ttk.Label(access, text=label('User login format', 'Формат логина пользователя')).pack(anchor='w', pady=(8, 0))
    dialog.lookup = tk.StringVar(value=saved.get('user_search_attribute', 'userPrincipalName'))
    combo = ttk.Combobox(access, textvariable=dialog.lookup, values=('userPrincipalName', 'sAMAccountName'), state='readonly')
    combo.pack(fill='x'); dialog.inputs.append(combo)
    ttk.Label(access, text=label('userPrincipalName = user@domain; sAMAccountName = short username. The lookup account only needs read access.', 'userPrincipalName = user@domain; sAMAccountName = короткий логин. Учётной записи поиска достаточно прав чтения.'), wraplength=700).pack(anchor='w', pady=8)
    dialog.add_button(access, 'Проверить учётную запись и группу', lambda: dialog.probe('directory'))
    dialog.test_user = tk.StringVar(); dialog.test_password = tk.StringVar()
    for text, var, hidden in [(tr('Пробный пользователь (логин / UPN)'), dialog.test_user, False), (tr('Пароль пробного пользователя'), dialog.test_password, True)]:
        ttk.Label(tests, text=text).pack(anchor='w', pady=(8, 0))
        entry = ttk.Entry(tests, textvariable=var, show='*' if hidden else ''); entry.pack(fill='x'); dialog.inputs.append(entry)
    dialog.add_button(tests, 'Проверить пользователя и допуск', lambda: dialog.probe('user'))
    ttk.Label(tests, text=tr('Пробный пароль не сохраняется. Не повторяйте неверный пароль: AD может заблокировать учётную запись.'), wraplength=700).pack(anchor='w', pady=8)
    dialog.test_results = {mode: tk.StringVar(value=caption + ': ' + label('not checked', 'не проверено')) for mode, caption in [('connection', label('Connection', 'Соединение')), ('directory', label('Lookup and group', 'Поиск и группа')), ('user', label('User access', 'Допуск пользователя'))]}
    for var in dialog.test_results.values(): ttk.Label(outer, textvariable=var, wraplength=720).pack(anchor='w', pady=3)
    dialog.status = tk.StringVar(value=tr('Настройки сохраняются только в защищённых данных площадки Manager.'))
    ttk.Label(outer, textvariable=dialog.status, wraplength=720).pack(anchor='w', pady=10)
    ttk.Label(outer, text=label('Tests run from App VM. Passing them does not apply settings or prove App-container trust. Group changes do not revoke existing App sessions.', 'Проверки выполняются с VM App. Успех не применяет настройки и не подтверждает доверие контейнера App. Изменение группы не отзывает открытые сессии App.'), wraplength=720).pack(anchor='w')
    bar = ttk.Frame(outer); bar.pack(fill='x', pady=8)
    dialog.add_button(bar, 'Сохранить настройки LDAP', dialog.save)
    dialog.add_button(bar, 'Закрыть', dialog.close)
    def invalidate(*_):
        for mode, var in dialog.test_results.items(): var.set(mode + ': ' + label('settings changed — recheck', 'настройки изменены — повторите проверку'))
    for var in (*dialog.values.values(), dialog.lookup, dialog.allow_plain_ldap): var.trace_add('write', invalidate)
    dialog.timer = dialog.after(100, dialog.poll); dialog.grab_set()
