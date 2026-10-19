"""Saved AD settings, deployment mapping and read-only connection probes."""
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from .i18n import tr
from .ssh import Remote

RESOURCES = Path(__file__).parent / 'resources'


def read_ca(data):
    if len(data) > 262144:
        raise ValueError(tr('CA: файл слишком большой.'))
    if b'PRIVATE KEY' in data:
        raise ValueError(tr('Нужен публичный CA-сертификат, без приватного ключа.'))
    try:
        certs = x509.load_pem_x509_certificates(data) if b'-----BEGIN' in data else [x509.load_der_x509_certificate(data)]
        if not certs:
            raise ValueError()
        now = datetime.now(timezone.utc)
        for cert in certs:
            if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError()
            if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
                raise ValueError()
        return ''.join(c.public_bytes(serialization.Encoding.PEM).decode('ascii') for c in certs)
    except (ValueError, x509.ExtensionNotFound):
        raise ValueError(tr('Выберите действующий сертификат CA, а не сертификат сайта или контроллера.')) from None


def validate_directory(value, connection_only=False):
    """Return only known fields, so test passwords cannot enter the saved profile."""
    if not isinstance(value, dict):
        raise ValueError(tr('Некорректные настройки LDAP.'))
    out = {k: value.get(k, '') for k in ('url', 'domain', 'base_dn', 'bind_dn', 'bind_password', 'group_dn', 'ca_pem')}
    for key, text in out.items():
        limit = 262144 if key == 'ca_pem' else 2048
        if not isinstance(text, str) or len(text) > limit or ('\x00' in text):
            raise ValueError(tr('Некорректные настройки LDAP.'))
        if key != 'ca_pem' and any(c in text for c in '\r\n'):
            raise ValueError(tr('Поля LDAP должны занимать одну строку.'))
        if key != 'bind_password':
            out[key] = text.strip()
    try:
        u = urlsplit(out['url'])
        port = u.port if u.port is not None else (389 if u.scheme=='ldap' else 636)
        if (u.scheme not in ('ldap','ldaps') or not u.hostname or u.username or u.password
                or u.path not in ('', '/') or u.query or u.fragment or not 1 <= port <= 65535
                or not re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9.-]+', u.hostname)
                or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label) for label in u.hostname.split('.'))):
            raise ValueError()
        import ipaddress
        try: ipaddress.ip_address(u.hostname)
        except ValueError: pass
        else: raise ValueError()
        out['url'] = u.scheme+'://' + u.hostname.lower() + ':' + str(port)
    except ValueError:
        raise ValueError(tr('Укажите ldaps://имя-контроллера:636 или ldap://имя-контроллера:389. Требуется корректное DNS-имя.')) from None
    if u.scheme=='ldap':
        if value.get('allow_plain_ldap') is not True:
            raise ValueError(tr('LDAP без TLS передаёт пароли без шифрования. Подтвердите небезопасный режим отдельно.'))
        out['allow_plain_ldap']=True
        out['ca_pem']=''
    if out['ca_pem']:
        out['ca_pem'] = read_ca(out['ca_pem'].encode('utf-8'))
    if not connection_only:
        if (len(out['domain'])>253 or '.' not in out['domain'] or
                any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label) for label in out['domain'].split('.'))):
            raise ValueError(tr('Укажите домен пользователей, например byteallenergy.com.'))
        for key in ('base_dn', 'group_dn'):
            if not out[key] or '=' not in out[key]:
                raise ValueError(tr('Укажите Base DN и полный DN группы допуска.'))
        if bool(out['bind_dn']) != bool(out['bind_password']):
            raise ValueError('Provide both search account and password, or leave both empty.')
    lookup=value.get('user_search_attribute','userPrincipalName')
    if lookup not in ('userPrincipalName','sAMAccountName'):raise ValueError('Unsupported AD user lookup attribute')
    out.update(schema=1, provider='active_directory', user_search_attribute=lookup)
    return out


def save_directory(vault, value):
    config = validate_directory(value) if value.get('enabled',False) or value.get('url') else {}
    config['enabled'] = bool(value.get('enabled',False))
    vault.data['directory'] = config
    vault.save()
    return config


def app_directory(value):
    """Map to the v0.5.0 App singleton. Probe credentials never reach App."""
    if value is None:return None
    if not value.get('enabled',False):return {'mode':'disabled'}
    c=validate_directory(value)
    return dict(mode=urlsplit(c['url']).scheme,server_uri=c['url'],user_dn_template='%(user)s@'+c['domain'],
                ca_certificate=c['ca_pem'],connect_timeout=5,receive_timeout=10,
                group_filter_enabled=True,user_search_base=c['base_dn'],
                user_search_attribute=c['user_search_attribute'],allowed_group_dns=c['group_dn'],
                group_match='any',include_nested_groups=True)


def probe_directory(host, credentials, value, mode='connection', username='', password='', log=lambda message: None, remote_factory=Remote):
    """SSH to App VM; stage pure-Python probe, no sudo/install/container changes."""
    if mode not in ('connection', 'directory', 'user'):
        raise ValueError('Unknown directory probe')
    config = validate_directory(value, connection_only=mode == 'connection')
    if mode == 'user' and (not username.strip() or not password):
        raise ValueError(tr('Введите логин и пароль для пробного входа.'))
    if mode != 'connection' and not config['bind_dn'] and (not username.strip() or not password):
        raise ValueError('Enter a test login and password to check LDAP without a search account; these credentials are not saved.')
    if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', host.get('fingerprint', '')):
        raise ValueError(tr('Сначала подтвердите SSH-отпечаток App на вкладке серверов.'))
    r = remote_factory('app', host, credentials, log)
    try:
        r.prepare_stage()
        for file in sorted((RESOURCES / 'ldap_deps').glob('*.whl')):
            r.put_bytes(file.name, file.read_bytes())
        r.put_bytes('directory_probe.py', (RESOURCES / 'directory_probe.py').read_bytes())
        request = json.dumps({'config': config, 'mode': mode, 'username': username, 'password': password})
        output = r.command('python3 ' + shlex.quote(r.stage + '/directory_probe.py'), stdin=request, timeout=90)
        lines = [line[len('DIRECTORY_RESULT:'):] for line in output.splitlines() if line.startswith('DIRECTORY_RESULT:')]
        if len(lines) != 1:
            raise ValueError(tr('App VM не вернула результат проверки LDAP.'))
        result = json.loads(lines[0])
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError(tr('App VM не вернула результат проверки LDAP.'))
        return result
    finally:
        try: r.cleanup()
        finally: r.close()
