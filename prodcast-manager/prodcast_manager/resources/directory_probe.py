"""Read-only AD probe, runs on App VM using staged wheels. No App changes."""
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


class ProbeFailure(Exception):
    pass


def tls_connection(config):
    endpoint = urlsplit(config['url'])
    if endpoint.scheme=='ldap':
        if config.get('allow_plain_ldap') is not True:raise ProbeFailure('plaintext_not_allowed')
        with socket.create_connection((endpoint.hostname,endpoint.port or 389),timeout=8):
            return {'transport':'ldap','encrypted':False,'certificate_verified':False}
    if endpoint.scheme!='ldaps':raise ProbeFailure('directory_error')
    context = ssl.create_default_context(cadata=config['ca_pem'] or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((endpoint.hostname, endpoint.port or 636), timeout=8) as sock:
        with context.wrap_socket(sock, server_hostname=endpoint.hostname) as secured:
            return {'tls': secured.version(), 'certificate_verified': True}


def directory_check(config, username='', password='', test_user=False):
    from ldap3 import Server, Connection, Tls, SIMPLE, NONE, BASE, SUBTREE
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import parse_dn
    from ldap3.core.exceptions import LDAPException
    # No referral following, automatic downgrade or second-server credential forwarding.
    endpoint = urlsplit(config['url'])
    secure=endpoint.scheme=='ldaps'
    if not secure and (endpoint.scheme!='ldap' or config.get('allow_plain_ldap') is not True):raise ProbeFailure('plaintext_not_allowed')
    tls = Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLSv1_2,
              ca_certs_data=config['ca_pem'] or None, valid_names=[endpoint.hostname], sni=endpoint.hostname) if secure else None
    server = Server(endpoint.hostname, port=endpoint.port or (636 if secure else 389), use_ssl=secure, tls=tls, get_info=NONE, connect_timeout=8)
    for key in ('base_dn', 'group_dn'):
        try: parse_dn(config[key])
        except LDAPException: raise ProbeFailure('invalid_dn') from None
    def connect(user, secret):
        conn = Connection(server, user=user, password=secret, authentication=SIMPLE,
                          auto_referrals=False, read_only=True, receive_timeout=8, raise_exceptions=False)
        if not conn.bind():
            conn.unbind()
            raise ProbeFailure('bind_failed')
        return conn
    service = bool(config.get('bind_dn'))
    if not service:
        if not username.strip() or not password:
            raise ProbeFailure('test_credentials_required')
        user = username.strip()
        if '@' in user:
            if user.rsplit('@', 1)[1].lower() != config['domain'].lower():
                raise ProbeFailure('wrong_domain')
        elif '\\' in user:
            raise ProbeFailure('use_upn')
        identity = user if '@' in user else user+'@'+config['domain']
    conn = connect(config['bind_dn'] if service else identity,
                   config['bind_password'] if service else password)
    try:
        if not conn.search(config['base_dn'], '(objectClass=*)', search_scope=BASE, attributes=['distinguishedName'], time_limit=8) or len(conn.entries) != 1:
            raise ProbeFailure('base_not_found')
        if not conn.search(config['group_dn'], '(objectClass=group)', search_scope=BASE, attributes=['distinguishedName'], time_limit=8) or len(conn.entries) != 1:
            raise ProbeFailure('group_not_found')
        result = {'service_bind': service, 'user_bind': not service, 'base_found': True, 'group_found': True}
        if not test_user:
            return result
        user = username.strip()
        if '@' in user:
            if user.rsplit('@', 1)[1].lower() != config['domain'].lower():
                raise ProbeFailure('wrong_domain')
        else:
            # Avoid assuming NetBIOS name equals the DNS domain.
            if '\\' in user: raise ProbeFailure('use_upn')
        attribute=config.get('user_search_attribute','userPrincipalName')
        if attribute not in ('userPrincipalName','sAMAccountName'):raise ProbeFailure('directory_error')
        identity=(user if '@' in user else user+'@'+config['domain']) if attribute=='userPrincipalName' else user.split('@',1)[0]
        match='('+attribute+'='+escape_filter_chars(identity)+')'
        if not conn.search(config['base_dn'], '(&(objectCategory=person)(objectClass=user)' + match + ')',
                           search_scope=SUBTREE, attributes=['userAccountControl', 'msDS-User-Account-Control-Computed'], size_limit=2, time_limit=8) or len(conn.entries) != 1:
            raise ProbeFailure('user_not_found')
        entry = conn.entries[0]
        attrs = entry.entry_attributes_as_dict
        def number(key):
            value = next((v for k, v in attrs.items() if k.lower() == key.lower()), [])
            return int(value[0]) if value else 0
        if number('userAccountControl') & 2 or number('msDS-User-Account-Control-Computed') & 16:
            raise ProbeFailure('user_disabled_or_locked')
        dn = entry.entry_dn
        member = '(memberOf:1.2.840.113556.1.4.1941:=' + escape_filter_chars(config['group_dn']) + ')'
        if not conn.search(dn, member, search_scope=BASE, attributes=['distinguishedName'], time_limit=8) or len(conn.entries) != 1:
            raise ProbeFailure('not_in_group')
        user_conn = connect(dn, password)
        user_conn.unbind()
        result.update(user_bind=True, allowed_group=True)
        return result
    finally:
        conn.unbind()


def run_probe(request):
    config = request['config']
    try:
        result = tls_connection(config)
        if request['mode'] != 'connection':
            result.update(directory_check(config, request.get('username', ''), request.get('password', ''), request['mode'] == 'user'))
        return dict(ok=True, **result)
    except ProbeFailure as e:
        return {'ok': False, 'code': str(e)}
    except ssl.SSLCertVerificationError:
        return {'ok': False, 'code': 'certificate'}
    except socket.gaierror:
        return {'ok': False, 'code': 'dns'}
    except (TimeoutError, socket.timeout):
        return {'ok': False, 'code': 'timeout'}
    except (ssl.SSLError, ConnectionResetError):
        return {'ok': False, 'code': 'tls'}
    except ConnectionRefusedError:
        return {'ok': False, 'code': 'refused'}
    except Exception:
        # Never return AD diagnostics or exception text: they can contain identities/secrets.
        return {'ok': False, 'code': 'directory_error'}


if __name__ == '__main__':
    folder = Path(__file__).resolve().parent
    sys.path[:0] = [str(p) for p in sorted(folder.glob('*.whl'))]
    print('DIRECTORY_RESULT:' + json.dumps(run_probe(json.load(sys.stdin))))
