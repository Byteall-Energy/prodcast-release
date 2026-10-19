'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const E = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let bridge, state, config, packages, page='overview', role='app', setting='general', step=0, deployMode='install';
  let selectedOperation='', target='', summary=false, follow=true, localDirty=false, polling=false, lastResult='';
  const access = {}, directoryDraft = {}, activationDraft = {}, certificateDraft = {}, maintenanceDraft = {};
  const t = (en,ru) => state?.language === 'ru' ? ru : en;
  const modes = {
    install:['Install','Установка'],update:['Update','Обновление'],repair:['Repair components','Восстановление компонентов'],
    ai:['AI setup / retry','Настройка / повтор AI'],'add-workers':['Add workers','Добавление Workers'],
    'worker-repair':['Reinstall worker','Переустановка Worker'],'worker-remove':['Remove worker','Удаление Worker'],
    backup:['Server backup','Бэкап на серверах'],export:['Export backup','Выгрузка бэкапа'],restore:['Restore data','Восстановление данных'],
    import:['Recover profile','Восстановление профиля'],'reset-keep':['Recreate; keep data','Пересоздать с данными'],
    'reset-full':['Delete installation data','Удалить данные установки'],'app-tls':['Deploy certificate','Применить сертификат'],
    stop:['Stop previous operation','Остановить предыдущую операцию'],'check-server':['Check server','Проверка сервера'],
    check:['Check site access','Проверка доступа площадки'],status:['Refresh status','Проверка состояния'],
    'verify-package':['Verify package','Проверка пакета'],fingerprint:['Retrieve fingerprint','Получение отпечатка']
  };
  const modeName = m => modes[m] ? t(...modes[m]) : m;
  const navigation = [
    ['overview','Overview','Обзор'],['servers','Servers & workers','Серверы и Workers'],['deploy','Install / update','Установка / обновление'],
    ['ai','AI','AI'],['settings','App settings','Настройки App'],['backups','Backups & recovery','Бэкапы и восстановление'],['operations','Operations','Операции']
  ];
  function rpc(action,payload={}) {
    return new Promise((resolve,reject) => bridge.request(JSON.stringify({action,payload}), raw => {
      try { const result=JSON.parse(raw); if(result.ok) resolve(result.value); else {const error=new Error(result.error);error.code=result.code;reject(error);} }
      catch(error){reject(error);}
    }));
  }
  function notice(text){$('#notification').textContent=text;$('#notification').hidden=false;setTimeout(()=>$('#notification').hidden=true,7000);}
  function closeModal(){ $('#modal').close(); $('#modal-content').replaceChildren(); }
  function modal(title,body,buttons='') {
    $('#modal-content').innerHTML=`<h1>${E(title)}</h1>${body}<div class="dialog-actions">${buttons}<button data-action="close-modal">${E(t('Close','Закрыть'))}</button></div>`;
    if(!$('#modal').open) $('#modal').showModal();
  }
  const button=(text,action,primary=false,extra='')=>`<button data-action="${action}" ${extra} ${primary?'class="primary"':''}>${E(text)}</button>`;
  const note=text=>`<div class="note">${E(text)}</div>`;
  const row=(a,b)=>`<div class="row"><span>${E(a)}</span><span>${E(b)}</span></div>`;
  const heading=(title,detail='')=>`<h1>${E(title)}</h1>${detail?`<p class="muted">${E(detail)}</p>`:''}`;
  const field=(title,bind,value='',placeholder='',secret=false,hint='')=>`<label class="field"><span>${E(title)}</span><span class="inline ${secret?'secret':''}"><input ${secret?'type="password" autocomplete="off"':'type="text"'} data-bind="${E(bind)}" value="${E(value)}" placeholder="${E(placeholder)}" ${state.busy?'disabled':''}>${secret?button(t('Show','Показать'),'reveal'):''}</span>${hint?`<small>${E(hint)}</small>`:''}</label>`;
  const file=(title,bind,value='',kind='zip')=>`<label class="field"><span>${E(title)}</span><span class="inline"><input data-bind="${E(bind)}" value="${E(value)}" placeholder="${E(t('No file selected','Файл не выбран'))}" ${state.busy?'disabled':''}>${button(t('Choose…','Выбрать…'),'pick',false,`data-bind-target="${E(bind)}" data-kind="${kind}" ${state.busy?'disabled':''}`)}</span></label>`;
  const check=(title,bind,checked)=>`<label class="check"><input type="checkbox" data-bind="${E(bind)}" ${checked?'checked':''} ${state.busy?'disabled':''}><span>${E(title)}</span></label>`;
  const select=(title,bind,values,value)=>`<label class="field"><span>${E(title)}</span><select data-bind="${E(bind)}" ${state.busy?'disabled':''}>${values.map(([id,text])=>`<option value="${E(id)}" ${id===value?'selected':''}>${E(text)}</option>`).join('')}</select></label>`;
  function getBound(path){
    let [group,...keys]=path.split('.');let obj=group==='c'?config:group==='p'?packages:group==='access'?(access[role]??={}):group==='directory'?directoryDraft:group==='activation'?activationDraft:group==='cert'?certificateDraft:maintenanceDraft;
    return keys.reduce((o,k)=>o?.[k],obj);
  }
  function setBound(path,value){
    if(path==='initial-count')return;
    let [group,...keys]=path.split('.');let obj=group==='c'?config:group==='p'?packages:group==='access'?(access[role]??={}):group==='directory'?directoryDraft:group==='activation'?activationDraft:group==='cert'?certificateDraft:maintenanceDraft;
    for(const k of keys.slice(0,-1)){ if(!Object.hasOwn(obj,k))obj[k]={}; obj=obj[k]; }
    obj[keys.at(-1)]=value;
    if(group==='c'||group==='p')localDirty=true;
  }
  async function sync(){if(localDirty){await rpc('update',{config,packages});localDirty=false;}}
  async function reload(){
    state=await rpc('state');config=structuredClone(state.config);packages={...state.packages};
    if(!config.hosts[role])role='app';
    for(const [key,value] of Object.entries(state.directory||{}))if(!Object.hasOwn(directoryDraft,key))directoryDraft[key]=value;
    for(const [key,value] of Object.entries(state.activation||{}))if(!Object.hasOwn(activationDraft,key))activationDraft[key]=value;
    chrome();render();
  }
  function chrome(){
    document.documentElement.lang=state.language;
    $('#version').textContent=t('Manager version ','Версия Manager ')+state.version;
    $('#language').value=state.language;
    $('#admin-access').textContent=t('App administrator access','Доступ администратора App');
    $('#site-selector').innerHTML=state.sites.map(s=>`<option value="${E(s.id)}" ${s.id===state.site?'selected':''}>${E(s.id)}${s.installed?'':' · '+E(t('draft','черновик'))}</option>`).join('');
    $('#site-selector').disabled=state.busy;$('#language').disabled=state.busy;
    const labels={'new-site':t('New site','Новая площадка'),'import-profile':t('Open profile…','Открыть профиль…'),'save-draft':t('Save draft','Сохранить черновик')};
    for(const [action,text]of Object.entries(labels)){const b=document.querySelector(`.context [data-action="${action}"]`);b.textContent=text;b.disabled=state.busy;}
    $('#global-status').textContent=state.busy?t('Operation running — conflicting edits locked','Операция выполняется — изменения заблокированы'):(localDirty||state.dirty)?t('Local changes not saved / applied','Локальные изменения не сохранены / не применены'):t('Local profile · reviewed actions change servers','Локальный профиль · изменения VM запускаются отдельной операцией');
    $('#profile-path').textContent=state.profile_path;$('#footer-status').textContent=t('Offline desktop interface','Офлайн-интерфейс');
    $('#navigation').innerHTML=navigation.map(([id,en,ru])=>`<button data-page="${id}" ${page===id?'aria-current="page"':''}>${E(t(en,ru))}</button>`).join('');
  }
  function overview(){
    const applied=state.applied==='match'?t('Saved settings match last deployment','Настройки совпадают с последним развёртыванием'):state.applied==='pending'?t('Saved settings differ — review an update','Настройки изменены — проверьте обновление'):t('Unknown until a successful deployment receipt exists','Неизвестно до успешного развёртывания');
    return heading(config.site_id||t('New site','Новая площадка'),state.installed?t('Installed site workspace','Управление установленной площадкой'):t('Set up a new site. Nothing is deployed until you review and start.','Новая площадка. Развёртывание начнётся только после проверки и запуска.'))+
      (state.pending.length?`<div class="warning"><strong>${E(t('Unresolved operation requires attention','Есть незавершённая операция'))}</strong><p>${E(state.pending.map(x=>x.journal).join(', '))}</p>${button(t('Inspect recovery','Варианты восстановления'),'go-operations')}</div>`:'')+
      `<div class="panel">${row(t('Recorded release','Релиз в локальной квитанции'),state.receipt?.version||t('Not verified','Не проверен'))}${row(t('App settings','Настройки App'),applied)}${row(t('Remote service health','Работоспособность VM'),t('Use Refresh status; local completion is not a health check','Проверьте состояние VM; локальный журнал не заменяет диагностику'))}${row(t('Workers configured','Workers в конфигурации'),workerRoles().length)}${row('AI',config.install_ai?t('Included in this site','Включён в площадку'):t('Not included; existing services are not removed','Не включён; существующие службы не удаляются'))}</div>`+
      `<div class="actions">${button(state.installed?t('Review update','Проверить обновление'):t('Start guided setup','Мастер установки'),'go-deploy',true)}${button(t('Manage workers','Управление Workers'),'go-servers')}${button(t('Refresh status','Проверить состояние'),'run-status',false,state.busy?'disabled':'')}</div>`+
      note(t('Move the entire prodcast-data folder with Manager. It contains server access and the portable encryption key.','Переносите всю папку prodcast-data вместе с Manager. Она содержит доступ к серверам и переносимый ключ шифрования.'));
  }
  const workerRoles=()=>Object.keys(config.hosts).filter(r=>/^worker\d+$/.test(r)).sort((a,b)=>Number(a.slice(6))-Number(b.slice(6)));
  function serverForm(){
    const h=config.hosts[role], draft=access[role]??={}, stored=state.credentials[role]||{}, windows=role.startsWith('worker');
    const prefix='c.hosts.'+role+'.', authPrefix='access.';
    const examples={app:'192.0.2.10',db:'192.0.2.11',ai:'192.0.2.12'};
    const example=examples[role]||'192.0.2.'+(20+Number(role.slice(6)));
    let html=`<div class="panel" id="server-editor"><h2>${E(role.toUpperCase())} · ${E(windows?'Windows Server':'Linux')}</h2><div class="grid">${field(t('Server IPv4','IPv4 сервера'),prefix+'address',h.address,example)}${field(t('SSH port','Порт SSH'),prefix+'port',h.port,'22')}${field(t('SSH username','Пользователь SSH'),prefix+'username',h.username,windows?'Administrator':'prodcast')}${select(t('Sign-in method','Способ входа'),prefix+'auth',[['password',t('Password','Пароль')],['key',t('SSH key','Ключ SSH')]],h.auth)}</div>`;
    const secretHint=key=>stored[key]?t('Stored. Leave empty to keep; use Clear saved to remove.','Сохранён. Пустое поле оставляет пароль; для удаления нажмите «Очистить сохранённый».'):t('Not saved yet','Ещё не сохранён');
    const secretField=(key,title)=>field(title,authPrefix+key,draft[key]||'','',true,secretHint(key))+(stored[key]?button(t('Clear saved','Очистить сохранённый'),'clear-secret',false,`data-key="${key}" ${state.busy?'disabled':''}`):'');
    html+=h.auth==='password'?secretField('password',t('SSH password','Пароль SSH')):file(t('SSH private key','Приватный ключ SSH'),prefix+'key_path',h.key_path,'key')+secretField('passphrase',t('Key passphrase (optional)','Пароль ключа (если есть)'));
    if(!windows){
      const mode=draft.sudoMode||(h.username==='root'?'root':stored.sudo_password?'password':'nopass');
      html+=`<details open><summary>${E(t('Linux administrator privileges','Административные права Linux'))}</summary>${select(t('Privilege mode','Режим повышения прав'),'access.sudoMode',[['root',t('Root account','Пользователь root')],['nopass',t('Passwordless sudo','Sudo без пароля')],['password',t('Sudo with a separate password','Sudo с отдельным паролем')]],mode)}${mode==='password'?secretField('sudo_password',t('Sudo password (may differ from SSH)','Пароль sudo (может отличаться от SSH)')):''}</details>`;
    }
    html+=`<details><summary>${E(t('Server identity','Подлинность сервера'))} · ${E(h.fingerprint?t('confirmed','подтверждена'):t('not confirmed','не подтверждена'))}</summary><pre>${E(h.fingerprint||t('Retrieve a fingerprint without sending a password. Compare it with the VM console before trusting.','Получите отпечаток без отправки пароля. Сверьте с консолью VM перед подтверждением.'))}</pre>${button(t('Retrieve fingerprint','Получить отпечаток'),'scan',false,state.busy?'disabled':'')}</details><div class="actions">${button(t('Save server access','Сохранить доступ к серверу'),'save-access',true,state.busy?'disabled':'')}${button(t('Test selected server','Проверить выбранный сервер'),'check-server',false,state.busy?'disabled':'')}</div></div>`;
    return html;
  }
  function servers(){
    const roles=['app','db','ai',...workerRoles()];
    return heading(t('Servers & workers','Серверы и Workers'),t('Select a server to edit access. Grey examples are not field values.','Выберите сервер для настройки доступа. Серые примеры не являются значениями полей.'))+
      (!state.installed?select(t('Initial worker count','Начальное число Workers'),'initial-count',Array.from({length:16},(_,i)=>[String(i+1),String(i+1)]),String(workerRoles().length)):'')+
      `<div class="table-wrap"><table><thead><tr><th>VM</th><th>IPv4</th><th>${E(t('Sign-in','Вход'))}</th><th>${E(t('Access','Доступ'))}</th></tr></thead><tbody>${roles.map(r=>`<tr class="${r===role?'selected':''}"><td>${E(r)}</td><td>${E(config.hosts[r].address||'—')}</td><td>${E(config.hosts[r].auth==='password'?t('Password','Пароль'):t('SSH key','Ключ SSH'))}</td><td>${button(t('Edit access','Настроить доступ'),'select-server',false,`data-role="${r}" ${state.busy?'disabled':''}`)}</td></tr>`).join('')}</tbody></table></div><div class="actions">${button(t('Add worker','Добавить Worker'),'add-worker',false,state.busy?'disabled':'')}${state.installed&&workerRoles().some(r=>!state.installed_workers.includes(r))?button(t('Review added workers','Проверить добавленных Workers'),'add-workers-review',true,state.busy?'disabled':''):''}${role.startsWith('worker')?button(t('Reinstall selected worker','Переустановить выбранный Worker'),'worker-repair',false,state.busy?'disabled':'')+button(t('Remove selected worker','Удалить выбранный Worker'),'worker-remove',false,state.busy?'disabled':''):''}</div>${serverForm()}`;
  }
  function packageFields(){return file(t('Complete release ZIP','ZIP полного релиза'),'p.release',packages.release,'zip')+button(t('Choose extracted directory…','Выбрать распакованный каталог…'),'pick',false,'data-kind="directory" data-bind-target="p.release"')+field(t('Expected SHA256 of release-manifest.json (not ZIP)','Ожидаемый SHA256 release-manifest.json (не ZIP)'),'p.trusted',packages.trusted,'')+note(t('Use the manifest hash supplied through a trusted channel. The Manager checks package assets and the adapter contract.','Используйте хэш manifest из доверенного источника. Manager проверит файлы пакета и контракт адаптера.'))+button(t('Verify package locally','Проверить пакет локально'),'verify-release',false,state.busy?'disabled':'');}
  function aiFiles(){return `<div class="grid">${file(t('Ollama runtime components ZIP','ZIP компонентов Ollama'),'p.ai_ollama',packages.ai_ollama,'zip')}${file(t('Combined AI models ZIP (embedding + chat)','Общий ZIP моделей AI (embedding + chat)'),'p.ai_model',packages.ai_model,'models')}</div>`;}
  function deploy(){
    const stages=[t('Site','Площадка'),t('Package','Пакет'),t('AI & settings','AI и настройки'),t('Review','Обзор')];
    const body=step===0?`<div class="grid">${field(t('Site ID','ID площадки'),'c.site_id',config.site_id,'prodcast-production')}${field(t('Initial administrator username','Начальный логин администратора'),'c.admin_username',config.admin_username,'admin')}${field(t('Administrator email','Email администратора'),'c.admin_email',config.admin_email,'admin@example.com')}</div><div class="actions">${button(t('Configure servers & access','Настроить серверы и доступ'),'go-servers')}${button(t('App settings','Настройки App'),'go-settings')}</div>${note(t('Keep the original identity and core addresses for an installed site. Configure server access and App settings before deployment.','Для установленной площадки сохраняйте исходный ID и адреса основных VM. Перед развёртыванием настройте доступ и параметры App.'))}`:
      step===1?packageFields():step===2?select(t('AI handling during update','AI при обновлении'),'p.ai_action',[['keep',t('Keep existing AI unchanged (update only)','Сохранить существующий AI (только обновление)')],['update',t('Install / update AI packages','Установить / обновить пакеты AI')]],packages.ai_action)+(packages.ai_action==='update'||deployMode==='install'?aiFiles():note(t('AI package omission is intentional. This core update will not start a new AI deployment.','Пропуск пакетов AI намеренный. Обновление основного стека не начнёт новое развёртывание AI.')))+note(t('For first installation, include AI on the AI page. Saved HTTPS, domain-login and activation settings are applied by the release contract.','Для первой установки включите AI на странице AI. Сохранённые HTTPS, доменный вход и активация применяются согласно контракту релиза.')):
      `<div class="panel">${row(t('Action','Действие'),modeName(deployMode))}${row(t('Complete package','Полный релиз'),packages.release||'—')}${['app','db',...workerRoles()].map(r=>row(r,config.hosts[r].address||t('Required','Обязательно'))).join('')}${row('AI',deployMode==='update'&&packages.ai_action==='keep'?t('Intentionally keep unchanged','Намеренно сохранить'):config.install_ai?t('Included','Включён'):t('Not included','Не включён'))}</div>${deployMode==='update'?`<div class="warning">${E(t('App and scheduling stop during the update. Update backups are created. Database rollback is not automatic.','На время обновления App и планировщик остановятся. Будут созданы бэкапы. Автоматического отката БД нет.'))}</div>`:''}${state.pending.length?note(t('Pending journals exist. Inspect Operations before changing the deployment mode.','Есть незавершённые журналы. Проверьте страницу «Операции» перед сменой режима.')):''}<p>${E(t('Final access, ownership, package and compatibility checks run before deployment steps. A local review is not a remote readiness check.','Перед развёртыванием выполняются проверки доступа, владельца, пакета и совместимости. Локальный обзор не заменяет проверку VM.'))}</p>${button(t('Review and start','Проверить и запустить'),'deploy-start',true,state.busy?'disabled':'')}`;
    return heading(t('Install / update','Установка / обновление'),t('One guided action with an explicit package and scope.','Мастер операции с явным пакетом и областью изменений.'))+`<div class="tabs">${['install','update'].map(m=>`<button data-action="deploy-mode" data-mode="${m}" aria-pressed="${deployMode===m}" ${state.busy?'disabled':''}>${E(modeName(m))}</button>`).join('')}</div><div class="steps">${stages.map((s,i)=>`<button data-action="step" data-step="${i}" ${i===step?'aria-current="step"':''}>${i+1}. ${E(s)}</button>`).join('')}</div>${body}<div class="actions">${step>0?button(t('Back','Назад'),'previous-step'):''}${step<3?button(t('Next','Далее'),'next-step',true):''}</div>`;
  }
  function aiPage(){return heading(t('AI setup and recovery','Настройка и восстановление AI'))+check(t('Include AI in this site','Включить AI в этой площадке'),'c.install_ai',config.install_ai)+note(t('Including AI in the site and updating it during a core update are separate choices. GPU drivers must already be installed; CPU can also be used.','Включение AI в площадку и обновление AI вместе с основным стеком — разные решения. Драйвер GPU устанавливается заранее; можно использовать CPU.'))+file(t('Complete release ZIP','ZIP полного релиза'),'p.release',packages.release)+field(t('Expected manifest SHA256','Ожидаемый SHA256 manifest'),'p.trusted',packages.trusted)+aiFiles()+`<div class="actions">${button(t('Review AI setup / retry','Проверить настройку / повтор AI'),'review-ai',true,state.busy?'disabled':'')}${button(t('Inspect AI operations','Журналы AI'),'go-operations')}</div>`;}
  function generalSettings(){return `<div class="grid">${field(t('Internal App HTTPS URL','Внутренний HTTPS App'),'c.public_url',config.public_url,'https://prodcast.example.com')}${field(t('Management PC IPv4 for access rules','IPv4 ПК управления для правил доступа'),'c.admin_ip',config.admin_ip,'192.0.2.100')}${field(t('Initial administrator username','Начальный логин администратора'),'c.admin_username',config.admin_username,'admin')}${field(t('Administrator email','Email администратора'),'c.admin_email',config.admin_email,'admin@example.com')}${field(t('Separate Docker /24 subnet','Отдельная подсеть Docker /24'),'c.app_subnet',config.app_subnet,'172.30.23.0/24')}</div>${note(t('Initial provisioning settings do not edit an existing App account. Set the browser-facing URL with the HTTPS certificate.','Начальные параметры не изменяют существующий аккаунт App. Внешний адрес сайта задаётся с HTTPS-сертификатом.'))}${button(t('Save local configuration','Сохранить локальную конфигурацию'),'save-profile',true,state.busy?'disabled':'')}`;}
  function httpsSettings(){return (state.certificate?`<div class="panel"><h2>${E(t('Saved certificate','Сохранённый сертификат'))}</h2>${row('URL',state.certificate.url)}${row(t('Issuer','Выдан'),state.certificate.info.issuer)}${row(t('Valid until','Действует до'),state.certificate.info.expires)}<details><summary>SHA256</summary><pre>${E(state.certificate.info.sha256)}</pre></details></div>`:note(t('No organization certificate saved. Deployment uses Manager’s internal certificate.','Сертификат организации не сохранён. При развёртывании используется внутренний сертификат Manager.')))+field(t('Browser-facing HTTPS URL','HTTPS-адрес сайта'),'cert.url',certificateDraft.url||state.certificate?.url||'','https://prodcast.example.com')+file(t('PFX / P12 certificate with private key','Сертификат PFX / P12 с приватным ключом'),'cert.path',certificateDraft.path,'pfx')+field(t('PFX password','Пароль PFX'),'cert.password',certificateDraft.password,'',true)+file(t('Additional CA chain (optional)','Дополнительная цепочка CA (если нужна)'),'cert.ca_path',certificateDraft.ca_path,'ca')+`<div class="actions">${button(t('Validate PFX','Проверить PFX'),'check-certificate',true,state.busy?'disabled':'')}${button(t('Apply saved certificate','Применить сохранённый сертификат'),'review-certificate',false,state.busy?'disabled':'')}</div>`+note(t('Validation/import saves locally; deployment restarts App web services. Browser trust also depends on DNS, certificate name, issuer chain and the trusted-root store on the client PC. No automatic certificate renewal.','Проверка/импорт сохраняет локально; применение перезапускает веб-сервисы App. Доверие браузера также зависит от DNS, имени сертификата, цепочки и корневого CA на клиенте. Автопродления нет.'));}
  function domainSettings(){
    const d=directoryDraft, plain=(d.url||'').startsWith('ldap://');
    return check(t('Enable domain login in App','Включить доменный вход в App'),'directory.enabled',d.enabled)+select(t('Connection security','Защита соединения'),'directory.transport',[['ldaps',t('LDAPS / TLS','LDAPS / TLS')],['ldap',t('LDAP / unencrypted','LDAP / без шифрования')]],plain?'ldap':'ldaps')+field(t('LDAP / LDAPS URL','Адрес LDAP / LDAPS'),'directory.url',d.url,'ldaps://dc01.example.com:636')+(plain?`<div class="warning">${check(t('I allow unencrypted LDAP. Passwords are sent without encryption.','Разрешаю LDAP без шифрования. Пароли передаются открытым текстом.'),'directory.allow_plain_ldap',d.allow_plain_ldap)}</div>`:`<div class="actions">${button(t('Load CA certificate…','Загрузить сертификат CA…'),'load-directory-ca',false,state.busy?'disabled':'')}${button(t('Use system trust','Использовать системные CA'),'system-directory-ca',false,state.busy?'disabled':'')}</div><p class="small muted">${E(d.ca_pem?t('CA loaded. Certificate and hostname checks remain required.','CA загружен. Проверки сертификата и имени обязательны.'):t('App VM diagnostics and App container login can use different trust stores.','Диагностика VM App и вход в контейнере могут использовать разные хранилища доверия.'))}</p>`)+
      `<details open><summary>${E(t('Directory lookup and access','Поиск и допуск пользователей'))}</summary><div class="grid">${field(t('User domain','Домен пользователей'),'directory.domain',d.domain,'example.com')}${field(t('Search Base DN','Base DN поиска'),'directory.base_dn',d.base_dn,'DC=example,DC=com')}${field(t('Allowed group DN','DN группы допуска'),'directory.group_dn',d.group_dn,'CN=ProdCast-Users,OU=Groups,DC=example,DC=com')}${field(t('Search account UPN / DN (optional)','Учётная запись поиска UPN / DN (необязательно)'),'directory.bind_dn',d.bind_dn,'svc-prodcast@example.com')}</div>${field(t('Search account password (optional)','Пароль учётной записи поиска (необязательно)'),'directory.bind_password',d.bind_password,'',true,state.directory.password_stored?t('Stored; leave empty to keep','Сохранён; пустое поле оставляет пароль'):'')}${note(t('Leave both search-account fields empty to use the signing-in user. To test lookup without a search account, enter a test login and password below; these are never saved. Clear the account name to remove its stored password.','Оставьте оба поля учётной записи поиска пустыми, чтобы использовать входящего пользователя. Для проверки поиска без служебной учётной записи введите ниже пробный логин и пароль; они не сохраняются. Очистка имени удаляет сохранённый пароль.'))}${select(t('User login format','Формат логина пользователя'),'directory.user_search_attribute',[['userPrincipalName',t('Email-style (userPrincipalName)','user@domain (userPrincipalName)')],['sAMAccountName',t('Short username (sAMAccountName)','Короткий логин (sAMAccountName)')]],d.user_search_attribute||'userPrincipalName')}</details><div class="actions">${button(t('Test connection','Проверить соединение'),'probe-connection',false,state.busy?'disabled':'')}${button(t('Test lookup & group','Проверить поиск и группу'),'probe-directory',false,state.busy?'disabled':'')}</div><details><summary>${E(t('Test a user’s group access','Проверить допуск пользователя'))}</summary>${field(t('Test login / UPN','Пробный логин / UPN'),'directory.test_user',d.test_user,'')}${field(t('Test password (not saved)','Пробный пароль (не сохраняется)'),'directory.test_password',d.test_password,'',true)}${button(t('Test user','Проверить пользователя'),'probe-user',false,state.busy?'disabled':'')}</details>${button(t('Save for next deployment','Сохранить для следующего развёртывания'),'save-directory',true,state.busy?'disabled':'')}${note(t('Tests run from App VM and do not apply settings. Existing App sessions are not revoked by group changes. These settings do not join workers to a domain or change service accounts.','Проверки выполняются с VM App и не применяют настройки. Изменение групп не отзывает открытые сессии App. Эти параметры не вводят Workers в домен и не меняют пользователя служб.'))}`;
  }
  function activationSettings(){
    const a=activationDraft,offline=a.mode==='offline';
    return select(t('Activation method','Способ активации'),'activation.mode',[['online',t('Online licensing server','Онлайн-сервер лицензий')],['offline',t('Offline license file','Офлайн-файл лицензии')]],a.mode||'online')+(offline?file(t('Issued .license file','Выданный файл .license'),'activation.document_path',a.document_path,'license')+file(t('Signing public key PEM','Публичный ключ подписи PEM'),'activation.public_key_path',a.public_key_path,'public-key')+note(t('Company, validity and permissions come from the signed document. Never load the signing private key.','Компания, срок и права берутся из подписанного документа. Приватный ключ подписи загружать нельзя.')):field(t('Company ID','ID компании'),'activation.tenant_id',a.tenant_id||config.tenant_id,'')+field(t('HTTPS licensing server','HTTPS-сервер лицензий'),'activation.url',a.url,'https://licenses.example.com')+field(t('Company client token','Клиентский токен компании'),'activation.token',a.token,'',true,a.token_stored?t('Stored; leave empty to keep','Сохранён; пустое поле оставляет токен'):'')+file(t('Optional licensing-server CA','CA сервера лицензий (если нужен)'),'activation.ca_path',a.ca_path,'ca'))+`<div class="actions">${button(t('Save activation for next deployment','Сохранить активацию для следующего развёртывания'),'save-activation',true,state.busy?'disabled':'')}${button(t('Show / create installation ID','Показать / создать ID установки'),'installation-id',false,state.busy?'disabled':'')}</div>${a.offline_stored?note(t('An offline license is already stored. Choose its files only when replacing it.','Офлайн-лицензия уже сохранена. Выбирайте файлы только для её замены.')):''}`;
  }
  function settings(){return heading(t('App settings','Настройки App'),t('Saved settings and applied server settings are separate states.','Сохранённые настройки и параметры на серверах — разные состояния.'))+`<div class="tabs">${[['general','General & network','Основные и сеть'],['https','HTTPS','HTTPS'],['directory','Domain login','Доменный вход'],['activation','Activation','Активация']].map(([id,en,ru])=>`<button data-action="settings-tab" data-tab="${id}" aria-pressed="${setting===id}">${E(t(en,ru))}</button>`).join('')}</div>`+(setting==='general'?generalSettings():setting==='https'?httpsSettings():setting==='directory'?domainSettings():activationSettings());}
  const maintenanceItems=[
    ['backup','Create server backup','Создать бэкап на серверах','Create backups on managed VMs. Separate from a portable archive.','Бэкапы на управляемых VM. Отдельно от переносимого архива.'],
    ['export','Export encrypted .pcbackup','Выгрузить зашифрованный .pcbackup','Create a portable archive with its own password.','Создать переносимый архив с отдельным паролем.'],
    ['restore','Restore data from archive','Восстановить данные из архива','Replaces current data; services stop temporarily. No automatic rollback.','Заменяет текущие данные; сервисы временно остановятся. Автоотката нет.'],
    ['import','Recover Manager profile','Восстановить профиль Manager','Recovers local identity/settings. On clean VMs install the original release before restoring data.','Восстановить локальные ID и настройки. На чистых VM сначала установите исходный релиз, затем восстановите данные.'],
    ['repair','Repair installed components','Восстановить установленные компоненты','Exact installed complete release required. Retains data; may restart services.','Нужен точный установленный полный релиз. Данные сохраняются; возможен перезапуск.'],
    ['reset-keep','Recreate components; keep data','Пересоздать компоненты с данными','Recreates managed components. OS, SSH, network and server backups remain.','Пересоздаёт управляемые компоненты. ОС, SSH, сеть и бэкапы остаются.'],
    ['reset-full','Delete ProdCast installation data','Удалить данные установки ProdCast','Destructive. Requires the site name. OS, SSH, network and server backups remain.','Удаление данных. Нужен ввод ID площадки. ОС, SSH, сеть и бэкапы остаются.']
  ];
  function backups(){return heading(t('Backups & recovery','Бэкапы и восстановление'),t('Choose the outcome. Repair, restore and reset affect different things.','Выберите результат. Ремонт компонентов, восстановление данных и сброс — разные операции.'))+maintenanceItems.map(([mode,en,ru,den,dru])=>`<section class="panel"><h2 class="${mode==='reset-full'?'error':''}">${E(t(en,ru))}</h2><p>${E(t(den,dru))}</p>${button(t('Review…','Обзор…'),'maintenance-review',false,`data-mode="${mode}" ${state.busy?'disabled':''}`)}</section>`).join('');}
  function operationRows(){return state.history.length?`<div class="table-wrap" id="operations-list"><table><thead><tr><th>${E(t('Started','Начало'))}</th><th>${E(t('Operation','Операция'))}</th><th>${E(t('Local result','Локальный результат'))}</th><th></th></tr></thead><tbody>${state.history.map(item=>`<tr class="${item.id===selectedOperation?'selected':''}"><td>${E(item.started)}</td><td>${E(modeName(item.mode))}</td><td>${E(item.status==='running'&&item.id!==state.active?t('Interrupted / unknown','Прервана / неизвестно'):item.status)}</td><td>${button(t('View','Открыть'),'select-operation',false,`data-id="${item.id}"`)}</td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">${E(t('No recorded operations for this site yet. Previous Manager session files remain in the logs folder.','Для площадки пока нет записанных операций. Прежние журналы Manager доступны в папке логов.'))}</p>`;}
  function operations(){return heading(t('Operations and logs','Операции и журналы'),t('Core installation/update, workers, AI, certificates and maintenance.','Установка/обновление стека, Workers, AI, сертификаты и обслуживание.'))+`<div id="history-content">${operationRows()}</div><div id="pending-content">${pendingView()}</div><div class="actions">${button(t('Recovery options','Варианты восстановления'),'recovery')}${button(t('Open logs folder','Открыть папку логов'),'open-logs')}${button(t('Open legacy session log…','Открыть прежний журнал…'),'legacy-log')}</div><div class="inline"><label>${E(t('Target','Сервер'))} <select id="log-target"><option value="">${E(t('All targets','Все серверы'))}</option></select></label><label class="check"><input id="summary" type="checkbox" ${summary?'checked':''}>${E(t('Summary (first lines)','Кратко (первые строки)'))}</label><label class="check"><input id="follow" type="checkbox" ${follow?'checked':''}>${E(t('Follow output','Следовать за выводом'))}</label></div><pre id="log-output"></pre><div class="actions">${button(t('Copy visible log','Копировать видимый журнал'),'copy-log')}${button(t('Copy selection','Копировать выделенное'),'copy-selection')}</div><p class="small muted">${E(t('Full Manager events include remote diagnostic excerpts captured on failures. This is not a continuous stream of every server transcript.','Полный журнал Manager включает диагностику VM, собранную при ошибках. Это не непрерывная трансляция всех серверных журналов.'))}</p>`;}
  function pendingView(){return state.pending.length?`<div class="warning"><strong>${E(t('Unresolved journals','Незавершённые журналы'))}</strong><p>${E(state.pending.map(x=>x.journal+' · '+x.status).join('\n'))}</p></div>`:note(t('No pending local journals. Remote ownership has not been checked by this view.','Незавершённых локальных журналов нет. Эта страница не проверяет владельцев на VM.'));}
  function render(){
    chrome();$('#main').innerHTML=page==='overview'?overview():page==='servers'?servers():page==='deploy'?deploy():page==='ai'?aiPage():page==='settings'?settings():page==='backups'?backups():operations();
    if(page==='operations')refreshLogs();
  }
  async function refreshLogs(){
    if(page!=='operations')return;
    if(!selectedOperation)selectedOperation=state.active||state.history[0]?.id||'';
    const result=await rpc('logs',{id:selectedOperation,target,summary});
    if(page!=='operations'||!$('#log-output'))return;
    const pre=$('#log-output'),position=pre.scrollTop;
    if(pre.textContent!==result.text){pre.textContent=result.text;if(follow)pre.scrollTop=pre.scrollHeight;else pre.scrollTop=position;}
    const selector=$('#log-target');selector.innerHTML=`<option value="">${E(t('All targets','Все серверы'))}</option>`+result.targets.map(r=>`<option value="${E(r)}">${E(r)}</option>`).join('');selector.value=target;
  }
  async function start(action,payload){
    await sync();const result=await rpc(action,payload);selectedOperation=result.operation||'';target='';page='operations';await reload();return result;
  }
  function review(mode,action='run',extra={}){
    const targets=mode==='app-tls'?['app']:mode==='ai'?['ai']:['app','db',...workerRoles()];
    const impact=mode==='update'?t('App and task scheduling stop. Tasks drain, update backups are created, and services restart. No automatic DB rollback.','App и планировщик остановятся. Задачи завершатся, будут созданы бэкапы, сервисы перезапустятся. Автоотката БД нет.'):
      mode==='worker-remove'?t('Remove only the selected worker service and revoke access. Files/logs remain. Scheduling pauses temporarily.','Удалить только выбранную службу Worker и отозвать доступ. Файлы/логи остаются. Планирование временно приостанавливается.'):
      mode==='worker-repair'?t('Reinstall the selected worker from the exact installed release. Preserve its ID/settings. Scheduling pauses temporarily.','Переустановить выбранный Worker из точного установленного релиза. ID/настройки сохраняются. Планирование временно приостанавливается.'):
      mode==='add-workers'?t('Install only added workers using the exact installed release. Existing workers retain their IDs.','Установить только добавленных Workers из точного установленного релиза. ID существующих сохраняются.'):
      mode==='app-tls'?t('Restart App web services to apply the saved certificate.','Перезапустить веб-сервисы App для применения сохранённого сертификата.'):
      mode==='ai'?t('Install/retry AI using the selected runtime/model packages.','Установить/повторить AI с выбранными пакетами компонентов/моделей.'):
      t('Run this action against the selected site. Follow its logs and preserve the profile on failure.','Выполнить операцию для выбранной площадки. Следите за журналом и сохраните профиль при ошибке.');
    modal(t('Review: ','Обзор: ')+modeName(mode),`<div class="panel">${targets.map(r=>row(r,config.hosts[r]?.address||'—')).join('')}${row(t('Selected worker','Выбранный Worker'),extra.role||'—')}${row(t('Complete package','Полный релиз'),packages.release||'—')}</div><div class="warning">${E(impact)}</div><p>${E(t('Final validation and ownership checks remain mandatory.','Итоговые проверки и сверка владельца обязательны.'))}</p>`,button(t('Start this action','Запустить эту операцию'),'confirm-run',true,`data-mode="${E(mode)}" data-api="${action}" data-role="${E(extra.role||'')}"`));
  }
  function recovery(){
    const advice={'journal.json':t('Resume the same core install/update with its original release.','Продолжите исходную установку/обновление с исходным релизом.'),'ai-journal.json':t('Retry AI with its packages, or stop its owner.','Повторите AI с пакетами либо остановите владельца.'),'workers-journal.json':t('Resume Add workers with the original addresses/release.','Продолжите добавление Workers с исходными адресами/релизом.'),'worker-action-journal.json':t('Repeat the same worker repair/remove and target.','Повторите ту же переустановку/удаление того же Worker.'),'app-tls-journal.json':t('Repeat Apply certificate.','Повторите применение сертификата.'),'maintenance-journal.json':t('Repeat the original backup/restore/reset action.','Повторите исходную операцию бэкапа/восстановления/сброса.')};
    modal(t('Pending operations','Незавершённые операции'),state.pending.map(x=>`<div class="panel"><h2>${E(x.journal)}</h2><p>${E([x.status,x.mode,x.role,x.phase,x.operation].filter(Boolean).join(' · '))}</p><p>${E(advice[x.journal])}</p></div>`).join('')+note(t('Stop supports core/AI ownership release. It does not roll back data or clear worker/certificate/maintenance journals. Foreign owners and unreachable VMs remain unresolved.','Остановка поддерживает освобождение владельца основной/AI операции. Данные не откатываются; журналы Workers, сертификата и обслуживания не очищаются. Чужие владельцы и недоступные VM остаются неразрешёнными.')),button(t('Review stop / release ownership','Проверить остановку / освобождение владельца'),'review-stop',false,state.busy?'disabled':''));
  }
  function maintenanceForm(mode){
    const item=maintenanceItems.find(x=>x[0]===mode);maintenanceDraft.mode=mode;
    modal(t(item[1],item[2]),`<p>${E(t(item[3],item[4]))}</p>`+file(t('Encrypted .pcbackup archive','Зашифрованный архив .pcbackup'),'maintenance.path',maintenanceDraft.path,mode==='export'?'backup-save':'backup')+field(t('Archive password (12+ characters)','Пароль архива (от 12 символов)'),'maintenance.password',maintenanceDraft.password,'',true)+(mode==='export'?field(t('Confirm archive password','Повторите пароль архива'),'maintenance.again',maintenanceDraft.again,'',true):'')+note(t('Keep the archive password separately. It cannot be recovered. Restore validates the original site/release before replacing data.','Сохраните пароль архива отдельно. Восстановить его нельзя. Перед заменой данных проверяется исходная площадка/релиз.')),button(t('Review and continue','Проверить и продолжить'),'maintenance-start',true));
  }
  async function resultDialog(result){
    if(!result.ok){modal(t('Operation needs attention','Операция требует внимания'),`<p class="error">${E(result.error)}</p>${note(t('Keep the profile and logs. Inspect the saved operation before choosing a recovery action.','Сохраните профиль и журналы. Проверьте операцию перед выбором восстановления.'))}`);return;}
    const value=result.value||{};
    if(result.mode==='fingerprint')modal(t('Confirm server identity','Подтвердить подлинность сервера'),`<p>${E(value.role)} · ${E(value.address)}:${E(value.port)}</p><pre>${E(value.value)}</pre><p>${E(t('Compare this complete fingerprint with the VM console or administrator. Retrieval alone does not establish trust.','Сверьте полный отпечаток с консолью VM или администратором. Получение само по себе не подтверждает доверие.'))}</p>`,button(t('Compared — trust fingerprint','Сверено — доверять отпечатку'),'trust',true,`data-role="${E(value.role)}" data-fingerprint="${E(value.value)}"`));
    else if(result.mode==='verify-package')modal(t('Package verified','Пакет проверен'),`<div class="definition"><span>${E(t('Version','Версия'))}</span><span>${E(value.version)}</span><span>SHA256 manifest</span><span>${E(value.manifest)}</span><span>${E(t('Required adapter','Требуемый адаптер'))}</span><span>${E(value.minimum_manager)}</span><span>${E(t('Upgrade sources','Обновление с версий'))}</span><span>${E(value.upgrade_from?.join(', '))}</span></div>`);
    else if(result.mode.startsWith('directory-'))modal(t('Directory test result','Результат проверки LDAP'),`<p class="${value.ok?'':'error'}">${E(value.ok?t('Test successful','Проверка успешна'):t('Test failed: ','Проверка не пройдена: ')+(value.code||'unknown'))}</p>${value.encrypted===false?`<div class="warning">${E(t('Connection is unencrypted; no certificate is verified.','Соединение не зашифровано; сертификат не проверяется.'))}</div>`:''}<pre>${E(JSON.stringify(value,null,2))}</pre>`);
    else if(result.mode==='stop')modal(t('Stop result','Результат остановки'),`<pre>${E(JSON.stringify(value,null,2))}</pre>`+note(t('Any remaining journals still require recovery. This was not a rollback.','Оставшиеся журналы по-прежнему требуют восстановления. Откат не выполнялся.')));
    else notice(t('Local action finished. Inspect its logs and pending journals.','Локальная операция завершилась. Проверьте журнал и незавершённые операции.'));
  }
  async function poll(){
    if(polling||!state)return;polling=true;
    try{
      const next=await rpc('state'),wasBusy=state.busy;
      state={...next,config:state.config,packages:state.packages};chrome();
      if(next.active&&next.active!==selectedOperation&&wasBusy===false){selectedOperation=next.active;target='';}
      if(page==='operations'){
        $('#history-content').innerHTML=operationRows();$('#pending-content').innerHTML=pendingView();await refreshLogs();
      }
      if(wasBusy&&!next.busy){await reload();}
      const resultKey=next.result?JSON.stringify(next.result):'';
      if(resultKey&&resultKey!==lastResult){lastResult=resultKey;await resultDialog(next.result);}
    }catch(error){notice(error.message);}finally{polling=false;}
  }
  async function changeSite(action,payload){
    if(localDirty||state.dirty){modal(t('Unsaved local changes','Несохранённые изменения'),`<p>${E(t('Save the non-secret draft or discard it before leaving. Save server access separately for passwords.','Сохраните черновик без паролей либо отмените изменения перед выходом. Пароли сохраняются отдельно с доступом сервера.'))}</p>`,button(t('Save and continue','Сохранить и продолжить'),'leave-save',true,`data-next="${action}" data-payload="${E(JSON.stringify(payload))}"`)+button(t('Discard and continue','Не сохранять и продолжить'),'leave-discard',false,`data-next="${action}" data-payload="${E(JSON.stringify(payload))}"`));return;}
    await rpc(action,payload);clearForms();await reload();
  }
  function clearForms(){for(const object of [access,directoryDraft,activationDraft,certificateDraft,maintenanceDraft])for(const key of Object.keys(object))delete object[key];localDirty=false;selectedOperation='';lastResult='';}
  async function handle(action,b){
    if(action==='close-modal'){closeModal();return;}
    if(action==='reveal'){const input=b.previousElementSibling;input.type=input.type==='password'?'text':'password';b.textContent=input.type==='password'?t('Show','Показать'):t('Hide','Скрыть');return;}
    if(action.startsWith('go-')){await sync();page=action.slice(3);if(page==='deploy')deployMode=state.installed?'update':'install';render();return;}
    if(action==='new-site'){await changeSite('new_site',{});return;}
    if(action==='leave-save'||action==='leave-discard'){
      if(action==='leave-save'){await sync();await rpc('save_draft');}
      const payload=JSON.parse(b.dataset.payload);closeModal();await rpc(b.dataset.next,payload);clearForms();await reload();return;
    }
    if(action==='import-profile'){const path=await rpc('pick_file',{kind:'profile'});if(path)await changeSite('import_profile',{path});return;}
    if(action==='save-draft'){await sync();await rpc('save_draft');notice(t('Draft saved. Passwords require Save server access.','Черновик сохранён. Пароли сохраняются отдельно с доступом сервера.'));await reload();return;}
    if(action==='save-profile'){await sync();await rpc('save_profile');notice(t('Configuration saved locally. No server changed.','Конфигурация сохранена локально. VM не изменялись.'));await reload();return;}
    if(action==='pick'){
      const path=await rpc('pick_file',{kind:b.dataset.kind});if(path){setBound(b.dataset.bindTarget,path);const input=document.querySelector(`[data-bind="${b.dataset.bindTarget}"]`);if(input)input.value=path;}return;
    }
    if(action==='select-server'){await sync();role=b.dataset.role;render();$('#server-editor').scrollIntoView({block:'start',behavior:'smooth'});return;}
    if(action==='save-access'){
      await sync();const draft=access[role]||{},secrets={};
      for(const key of ['password','passphrase','sudo_password'])if(draft[key])secrets[key]=draft[key];
      for(const key of draft.clear||[])secrets[key]='';
      if(!role.startsWith('worker')){
        if(draft.sudoMode==='root'&&config.hosts[role].username!=='root')throw new Error(t('Root mode requires SSH username root.','Для режима root укажите SSH-пользователя root.'));
        if(draft.sudoMode==='nopass'||draft.sudoMode==='root')secrets.sudo_password='';
      }
      await rpc('save_access',{role,secrets});await rpc('save_draft');delete access[role];await reload();notice(t('Server access saved securely. No installation started.','Доступ сервера сохранён. Установка не запускалась.'));return;
    }
    if(action==='clear-secret'){const draft=access[role]??={};draft.clear=[...new Set([...(draft.clear||[]),b.dataset.key])];draft[b.dataset.key]='';notice(t('Saved secret will be cleared when you save server access.','Сохранённый пароль будет удалён при сохранении доступа.'));return;}
    if(action==='scan'){await start('scan_server',{role});return;}
    if(action==='trust'){await rpc('trust_server',{role:b.dataset.role,fingerprint:b.dataset.fingerprint});closeModal();await reload();return;}
    if(action==='check-server'){await start('check_server',{role});return;}
    if(action==='add-worker'){await sync();const result=await rpc('add_worker_draft');role=result.role;page='servers';await reload();$('#server-editor').scrollIntoView({block:'start',behavior:'smooth'});notice(t('Configure the added worker, save its access, then review Add workers.','Настройте добавленный Worker, сохраните доступ, затем запустите добавление.'));return;}
    if(action==='add-workers-review'){modal(t('Add workers','Добавление Workers'),packageFields()+note(t('Use the exact installed complete release. New workers are added without renumbering existing workers.','Выберите точный установленный полный релиз. Новые Workers добавляются без перенумерации существующих.')),button(t('Review targets','Проверить цели'),'worker-review',true,'data-mode="add-workers"'));return;}
    if(action==='worker-repair'||action==='worker-remove'){
      await sync();modal(modeName(action),`<p>${E(role)} · ${E(config.hosts[role].address)}</p>${action==='worker-repair'?packageFields():''}<p>${E(t('The worker action uses the saved installation identity. Scheduling pauses temporarily.','Операция использует сохранённый ID установки. Планирование временно приостанавливается.'))}</p>`,button(t('Review target and impact','Проверить цель и влияние'),'worker-review',true,`data-mode="${action}"`));return;
    }
    if(action==='worker-review'){await sync();closeModal();review(b.dataset.mode,'worker_action',{role});return;}
    if(action==='deploy-mode'){deployMode=b.dataset.mode;render();return;}
    if(action==='step'){step=Number(b.dataset.step);render();return;}
    if(action==='next-step'||action==='previous-step'){await sync();step=Math.max(0,Math.min(3,step+(action==='next-step'?1:-1)));render();return;}
    if(action==='verify-release'){await start('verify_release',{});return;}
    if(action==='deploy-start'){await sync();review(deployMode);return;}
    if(action==='review-ai'){await sync();review('ai');return;}
    if(action==='review-certificate'){await sync();review('app-tls');return;}
    if(action==='run-status'){await start('run',{mode:'status'});return;}
    if(action==='confirm-run'){closeModal();await start(b.dataset.api,{mode:b.dataset.mode,role:b.dataset.role});return;}
    if(action==='settings-tab'){await sync();setting=b.dataset.tab;render();return;}
    if(action==='load-directory-ca'){const path=await rpc('pick_file',{kind:'ca'});if(path){directoryDraft.ca_pem=await rpc('directory_ca',{path});render();}return;}
    if(action==='system-directory-ca'){directoryDraft.ca_pem='';render();return;}
    if(action==='save-directory'){
      const value=directoryValue();await sync();await rpc('directory_save',value);delete directoryDraft.bind_password;await reload();notice(t('Saved for next deployment. App login is not changed yet.','Сохранено для следующего развёртывания. Вход App ещё не изменён.'));return;
    }
    if(action.startsWith('probe-')){
      const mode=action.slice(6),password=directoryDraft.test_password||'',user=directoryDraft.test_user||'';
      if((mode==='user'||(mode==='directory'&&!directoryDraft.bind_dn?.trim()))&&(!user||!password))throw new Error(t('Enter a test username and password below. Without a search account, directory checks use these temporary credentials.','Введите ниже пробный логин и пароль. Без учётной записи поиска проверка использует эти временные данные.'));
      await start('directory_probe',{mode,directory:directoryValue(),user,password});directoryDraft.test_password='';return;
    }
    if(action==='save-activation'){
      await sync();const value={...activationDraft};delete value.token_stored;delete value.offline_stored;
      if(!value.token)delete value.token;
      await rpc('activation_save',value);delete activationDraft.token;await reload();notice(t('Activation saved for next deployment.','Активация сохранена для следующего развёртывания.'));return;
    }
    if(action==='installation-id'){await sync();const result=await rpc('initialize_identity');modal(t('Installation ID','ID установки'),`<pre>${E(result.installation_id)}</pre>`);await reload();return;}
    if(action==='check-certificate'){
      await sync();const result=await rpc('certificate_check',certificateDraft);delete certificateDraft.password;
      certificateDraft.validated=result.id;modal(t('Certificate validated — not saved yet','Сертификат проверен — ещё не сохранён'),`<pre>${E(JSON.stringify(result.info,null,2))}</pre>`,button(t('Save certificate locally','Сохранить сертификат локально'),'save-certificate',true));return;
    }
    if(action==='save-certificate'){await rpc('certificate_save',{id:certificateDraft.validated});closeModal();await reload();notice(t('Certificate saved locally; deploy it to change App.','Сертификат сохранён локально; для изменения App примените его.'));return;}
    if(action==='maintenance-review'){
      const mode=b.dataset.mode;
      if(mode==='backup'||mode==='repair'){await sync();review(mode);return;}
      if(mode==='reset-keep'||mode==='reset-full'){
        maintenanceDraft.mode=mode;modal(modeName(mode),`<div class="warning">${E(maintenanceItems.find(x=>x[0]===mode)[state.language==='ru'?4:3])}</div>${['app','db',...workerRoles(),...(config.install_ai?['ai']:[])].map(r=>row(r,config.hosts[r].address)).join('')}${mode==='reset-full'?field(t('Type site ID to confirm deletion','Введите ID площадки для удаления'),'maintenance.confirmation','','',false,config.site_id):''}`,button(t('Run reviewed reset','Запустить выбранный сброс'),'maintenance-start',true));return;
      }
      maintenanceForm(mode);return;
    }
    if(action==='maintenance-start'){
      const value={...maintenanceDraft};if(value.mode==='export'&&value.password!==value.again)throw new Error(t('Passwords do not match.','Пароли не совпадают.'));
      delete value.again;closeModal();await start('maintenance',value);delete maintenanceDraft.password;delete maintenanceDraft.again;return;
    }
    if(action==='select-operation'){selectedOperation=b.dataset.id;target='';render();return;}
    if(action==='recovery'){recovery();return;}
    if(action==='review-stop'){modal(t('Stop / release previous ownership','Остановить / освободить предыдущего владельца'),note(t('Core/AI owners will be checked. Unsupported journal types remain pending. This does not roll back the database.','Владельцы основной/AI операции будут проверены. Неподдерживаемые журналы останутся незавершёнными. Откат БД не выполняется.')),button(t('Stop supported operations','Остановить поддерживаемые операции'),'confirm-stop',true));return;}
    if(action==='confirm-stop'){closeModal();await start('stop',{});return;}
    if(action==='open-logs'){await rpc('open_logs');return;}
    if(action==='legacy-log'){const value=await rpc('legacy_log');if(value)modal(t('Previous Manager session (site not recorded)','Прежний сеанс Manager (площадка не указана)'),`<pre>${E(value)}</pre>`);return;}
    if(action==='copy-log'||action==='copy-selection'){const value=action==='copy-log'?$('#log-output').textContent:window.getSelection().toString();await rpc('copy',{text:value});notice(t('Copied','Скопировано'));return;}
    if(action==='copy-field'){await rpc('copy',{text:b.closest('.field').querySelector('input').value});notice(t('Copied','Скопировано'));return;}
    if(action==='close-save'||action==='close-discard'){if(action==='close-save'){await sync();await rpc('save_draft');}await rpc('close',{choice:'discard'});return;}
    if(action==='unlock-legacy'){const input=$('[data-bind="maintenance.legacy_password"]');await rpc('unlock_legacy',{password:input?.value||''});closeModal();await reload();return;}
  }
  function directoryValue(){const value={...directoryDraft};delete value.test_password;delete value.test_user;delete value.password_stored;delete value.transport;if(!value.bind_password)delete value.bind_password;return value;}
  document.addEventListener('input',event=>{const input=event.target;if(input.dataset.bind){setBound(input.dataset.bind,input.type==='checkbox'?input.checked:input.value);chrome();}});
  document.addEventListener('change',async event=>{
    const input=event.target;
    try{
      if(input.id==='language'){await sync();await rpc('language_set',{language:input.value});await reload();return;}
      if(input.id==='site-selector'){await changeSite('select_site',{id:input.value});return;}
      if(input.id==='log-target'){target=input.value;await refreshLogs();return;}
      if(input.id==='summary'){summary=input.checked;await refreshLogs();return;}
      if(input.id==='follow'){follow=input.checked;await refreshLogs();return;}
      if(input.dataset.bind==='initial-count'){await sync();await rpc('set_initial_workers',{count:input.value});await reload();return;}
      if(input.dataset.bind){
        setBound(input.dataset.bind,input.type==='checkbox'?input.checked:input.value);
        if(input.dataset.bind==='directory.transport'){
          const u=directoryDraft.url||'';const host=u.replace(/^ldaps?:\/\//,'').replace(/:\d+\/?$/,'');
          directoryDraft.url=input.value+'://'+host+':'+(input.value==='ldap'?'389':'636');
        }
        if(input.tagName==='SELECT'||input.type==='checkbox'||input.dataset.bind==='directory.url')render();
      }
    }catch(error){modal(t('Check settings','Проверьте настройки'),`<p class="error">${E(error.message)}</p>`);}
  });
  document.addEventListener('click',async event=>{
    const b=event.target.closest('button');if(!b)return;
    try{
      if(b.dataset.page){await sync();page=b.dataset.page;render();return;}
      if(b.id==='admin-access'){
        const admin=await rpc('administrator');modal(t('Initial App administrator access','Начальные данные администратора App'),field('URL','admin.url',admin.url)+field(t('Username','Логин'),'admin.username',admin.username)+field(t('Initial password','Начальный пароль'),'admin.password',admin.password,'',true)+note(t('If changed inside App, use the new password. These are initial provisioning credentials.','Если пароль изменён в App, используйте новый. Здесь начальные данные установки.')));
        for(const input of $('#modal-content').querySelectorAll('input')){input.readOnly=true;input.parentElement.insertAdjacentHTML('beforeend',button(t('Copy','Копировать'),'copy-field'));}return;
      }
      if(b.dataset.action)await handle(b.dataset.action,b);
    }catch(error){
      if(error.code==='LegacyPasswordRequired')modal(t('Import older encrypted profile','Импорт прежнего зашифрованного профиля'),field(t('Old vault password (one-time import)','Прежний пароль vault (однократный импорт)'),'maintenance.legacy_password','','',true),button(t('Unlock older profile','Открыть прежний профиль'),'unlock-legacy',true));
      else modal(t('Action needs attention','Операция требует внимания'),`<p class="error">${E(error.message)}</p>`);
    }
  });
  $('#modal').addEventListener('close',()=>$('#modal-content').replaceChildren());
  window.manager={
    ready:false,
    requestClose(){if(state.busy){modal(t('Operation running','Операция выполняется'),note(t('Wait for the current action before closing. Closing can disconnect SSH and leave an interrupted operation.','Перед закрытием дождитесь завершения операции. Закрытие может оборвать SSH и оставить незавершённое состояние.')));return;}
      modal(t('Close Manager','Закрыть Manager'),note(t('Save the non-secret draft before closing? Passwords must be saved separately with server access.','Сохранить черновик без паролей перед закрытием? Пароли сохраняются отдельно с доступом сервера.')),button(t('Save and close','Сохранить и закрыть'),'close-save',true)+button(t('Close without saving','Закрыть без сохранения'),'close-discard'));}
  };
  if(!window.qt||typeof QWebChannel==='undefined'){
    $('#main').innerHTML='<h1>Desktop connection unavailable</h1><p>Open this interface using ProdCast-Manager.exe.</p>';return;
  }
  new QWebChannel(qt.webChannelTransport,async channel=>{
    bridge=channel.objects.manager;
    try{await reload();deployMode=state.installed?'update':'install';window.manager.ready=true;setInterval(poll,1200);}
    catch(error){$('#main').textContent=error.message;}
  });
})();
