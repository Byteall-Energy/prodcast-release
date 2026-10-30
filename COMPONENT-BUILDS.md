# Выборочная сборка ProdCast

Workflow **Build selected ProdCast components** (`build-components.yml`) собирает
выбранные компоненты и сохраняет отдельные пакеты в GitHub Actions Artifacts.
Он не создаёт Git-теги, GitHub Release, Complete-архив, не публикует Docker-образы
в registry и не выполняет деплой. Предыдущий Complete-релиз не требуется.

## Запуск

После добавления workflow в `main` откройте **Actions → Build selected ProdCast
components → Run workflow** в `Byteall-Energy/prodcast-release`.

1. Укажите `version`, например `v0.6.7`. Это внутренняя версия пакетов и EXE;
   номер можно использовать повторно для тестовых сборок. Каждая часть версии
   должна быть от 0 до 65535.
2. Отметьте один или несколько компонентов: **App, AI, Worker, Agent, Manager**.
   Все флажки по умолчанию выключены; пустой выбор завершится понятной ошибкой.
3. По умолчанию исходники каждого выбранного компонента берутся из `main`.
   При необходимости задайте `source_refs`, например:

   ```json
   {"app":"feature/pi-auth","agent":"main"}
   ```

   Ключи: `app`, `ai`, `worker`, `agent`, `manager`. Разрешены только выбранные
   компоненты. Значения — существующие ветки, теги или commit SHA. Manager
   собирается из `prodcast-release`. Неуказанные компоненты используют `main`.
   Поле **Use workflow from** выбирает версию самого workflow, а исходники
   Manager, как и остальных компонентов, выбираются через `source_refs`.
4. Выберите срок хранения Artifacts: 1, 3 или 7 дней (по умолчанию 1 день,
   чтобы ограничить расход квоты).
5. Запустите сборку. Внизу страницы запуска скачайте нужные Artifacts.

Для проверки изменения только в PI External Drivers достаточно выбрать **App**,
если изменение действительно ограничено App. Локальные незакоммиченные правки
не попадут в GitHub Actions: сначала опубликуйте их в выбранной ветке.

## Что получится

| Компонент | Репозиторий | Содержимое |
|---|---|---|
| App | `prodcast-app` | Linux amd64 образы Docker в архивах и deployment payload |
| AI | `prodcast-ai` | Linux amd64 образ Docker в архиве и deployment payload |
| Worker | `prodcast-worker` | Windows amd64 ZIP |
| Agent | `prodcast-agent` | Portable Windows EXE, инструкция и SBOM |
| Manager | `prodcast-release` | Windows EXE с проверенной внутренней версией |

Имена пакетов: `ProdCast-<component>-<version>-<run_id>-<run_attempt>`.
Каждый пакет содержит `component-build.json` с версией, SHA исходников,
SHA workflow и ссылкой на запуск, а также `BUILD-SHA256SUMS` для всех вложенных
файлов. Исходные `release-manifest.json` и `SHA256SUMS` компонентов сохраняются.
Отдельный `component-sources-<run_id>` содержит общий снимок выбранных исходников.

Workflow один раз разрешает выбранные refs в полные SHA, затем все jobs делают
checkout этих SHA. При **Re-run failed jobs** используется сохранённый снимок;
при **Re-run all jobs** refs разрешаются заново. После истечения срока хранения
снимка нужен новый полный запуск workflow.
Сбой одного компонента не отменяет остальные jobs: успешные пакеты остаются
доступны даже при красном общем статусе запуска.

## Проверки и ограничения

Используются существующие скрипты сборки компонентов с их smoke-проверками,
тесты упаковки App и тесты Manager. Перед загрузкой проверяются версия,
repository, source SHA, размеры и SHA-256 файлов из манифеста каждого компонента
(у Manager версию проверяет инструмент чтения EXE, а SHA задаёт checkout).

Выборочная сборка не запускает общие App/Worker functional tests полной сборки
и не требует зелёного CI выбранной ветки: она подходит для проверки изменений
до полного релиза. Успешный статус не подтверждает совместимость с конкретной
клиентской установкой. Обновление, миграции и откат проверяются в целевой среде
отдельно; эти Artifacts не являются Complete-архивом для полной установки.

## Доступ к приватным исходникам

Используются существующие secrets из полного release workflow:
`PRODCAST_RELEASE_APP_ID` и `PRODCAST_RELEASE_APP_PRIVATE_KEY`, либо fallback
`PRODCAST_RELEASE_TOKEN`. GitHub App должен быть установлен на выбранные
репозитории; workflow запрашивает только **Contents: read** и ограничивает
каждый build-token одним репозиторием. Для PAT достаточно Contents: read на
выбранных репозиториях. Обычный `GITHUB_TOKEN` может собрать публичный Manager,
но не даёт доступ к приватным соседним репозиториям.

`persist-credentials: false` используется во всех checkout. Выбирайте refs
доверенных исходников: build-скрипты выбранной ветки выполняются на runner.

Проверка orchestration-логики локально:

```sh
python -m unittest discover -s prodcast-manager/tests -p test_component_builds.py -v
```
