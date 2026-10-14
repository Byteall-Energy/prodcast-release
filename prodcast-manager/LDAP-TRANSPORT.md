# LDAP transport in Manager

LDAPS remains the default: `ldaps://dc.example.com:636`. Certificate chain and
hostname verification remain mandatory. TLS failures never fall back to plaintext.

For a deployment that explicitly requires unencrypted LDAP, enter
`ldap://dc.example.com:389` and check the separate warning acknowledging that
passwords are sent without encryption. This is plain LDAP, not StartTLS.
The acknowledgement is required for saving, deploying and connection/user probes.
CA certificates do not protect plain LDAP and are omitted from its deployment settings.
Connection checks clearly report unencrypted transport, not verified TLS.
Existing group restrictions, disabled/locked-user checks, password redaction and
no-referral policy remain unchanged.

Manager sends `mode=ldap` and the LDAP URL to the App directory configuration.
The target App must support that mode and URL in its actual DirectoryConfiguration
model and authentication backend. A successful standalone diagnostic is NOT proof
that an older App accepts or applies the configuration. App frontend changes are
not part of this Manager change. No live deployment or domain changes are performed.

## Русский

По умолчанию используется LDAPS с обязательной проверкой сертификата и имени.
Для `ldap://` требуется отдельная отметка о передаче паролей без шифрования.
Это не StartTLS. Автоматического перехода с LDAPS на LDAP при ошибке нет.
Проверки группы/пользователя сохраняются; импорт CA не защищает обычный LDAP.
App должен отдельно поддерживать `mode=ldap` и URL в модели конфигурации и
backend аутентификации. Успешная диагностика Manager не подтверждает такую
поддержку установленным App. Работающие VM и домен этим изменением не затронуты.
