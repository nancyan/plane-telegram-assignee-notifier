# Plane Telegram Assignee Notifier

Небольшой self-hosted сервис, который отправляет пользователю личное сообщение
в Telegram, когда его назначают исполнителем задачи в Plane или упоминают в
комментарии.

Поддерживаются три случая:

- исполнитель указан при создании задачи (`workitem.created`);
- исполнитель добавлен позднее (`workitem.updated`);
- пользователя отметили в комментарии (`workitem.comment.created`).

Сервис поддерживает Plane Webhooks v2 и legacy-события `issue` и
`issue_comment`, которые Community Edition продолжает отправлять в некоторых
установках. Он проверяет HMAC-подпись и защищается от повторной отправки по
`event_id` или детерминированному идентификатору legacy-события.

## Как это работает

1. Администратор создаёт одноразовую ссылку для Plane User UUID.
2. Пользователь открывает ссылку и нажимает Start в Telegram.
3. Сервис сохраняет связь `Plane user UUID → Telegram private chat_id`.
4. Plane отправляет событие о задаче или новом комментарии.
5. Бот пишет новым исполнителям и упомянутым пользователям только в личный чат.

Бот Telegram не может первым начать разговор, поэтому каждый сотрудник должен
один раз открыть бота и нажать **Start**.

## Быстрый запуск

### 1. Создайте Telegram-бота

Создайте бота через `@BotFather`, получите токен и узнайте username бота без
символа `@`.

### 2. Настройте переменные

```bash
cp .env.example .env
```

Заполните `.env`. По умолчанию бот получает команды через long polling:

```dotenv
TELEGRAM_UPDATE_MODE=polling
```

В этом режиме Telegram не подключается к вашему серверу: сервис сам запрашивает
обновления у Telegram Bot API. Это рекомендуемый вариант для серверов, до
которых Telegram не может достучаться извне.

Если сервер не может подключаться к Telegram напрямую, задайте SOCKS5-прокси:

```dotenv
TELEGRAM_PROXY_URL=socks5://username:password@proxy.example.com:1080
```

Прокси применяется ко всем исходящим запросам сервиса в Telegram Bot API:
получению команд через polling, отправке сообщений и управлению Telegram
webhook. Специальные символы в имени пользователя и пароле прокси необходимо
URL-кодировать.

Если прямые входящие соединения Telegram доступны, можно использовать прежний
webhook-режим:

```dotenv
TELEGRAM_UPDATE_MODE=webhook
TELEGRAM_WEBHOOK_SECRET=replace-with-a-long-random-secret
PUBLIC_BASE_URL=https://plane-notifier.example.com
```

При запуске сервис автоматически регистрирует или удаляет Telegram webhook в
зависимости от выбранного режима. `PUBLIC_BASE_URL` не требуется для polling.
Webhook Plane на `/webhooks/plane` остаётся отдельным: его должна вызывать ваша
инсталляция Plane.

Сгенерировать секрет Plane (и отдельный секрет Telegram для webhook-режима)
можно, например, так:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Запустите сервис

```bash
docker compose up -d --build
docker compose logs -f notifier
```

Проверка:

```bash
curl https://plane-notifier.example.com/health
```

### 4. Добавьте webhook в Plane

Откройте **Workspace settings → Webhooks → Add webhook**:

- URL: `https://plane-notifier.example.com/webhooks/plane`;
- события: `Work items → Created`, `Work items → Updated` и
  `Work item comments → Created`;
- сохраните скачанный секрет в `PLANE_WEBHOOK_SECRET`.

После изменения `.env` перезапустите контейнер:

```bash
docker compose up -d --force-recreate
```

### Диагностика

Смотреть обработку webhook и отправку сообщений в реальном времени:

```bash
docker compose logs -f notifier
```

При `LOG_LEVEL=INFO` сервис пишет тип и ID события, найденных получателей,
причины пропуска (`нет привязки`, `уже отправлено`, `получатели не найдены`) и
результат Telegram. `LOG_LEVEL=DEBUG` дополнительно показывает вызовы методов
Telegram Bot API. Токен и webhook-секреты в логи не выводятся.

Успешный HTTP 200 теперь содержит подробный JSON, например:

```json
{
  "ok": true,
  "event": "workitem.created",
  "event_id": "...",
  "work_item_id": "...",
  "status": "processed_without_delivery",
  "recipients": ["plane-user-uuid"],
  "sent": 0,
  "delivered_to": [],
  "skipped_unlinked": ["plane-user-uuid"],
  "skipped_duplicate": []
}
```

Webhook v2 используется автоматически, если Plane его предоставляет. Для
legacy webhook бот преобразует `issue`/`issue_comment` во внутренний формат и
учитывает поля `action` и `activity`.

## Подключение сотрудников

Добавьте свой числовой Telegram user ID в `ADMIN_TELEGRAM_USER_IDS`. Затем
отправьте боту:

```text
/linkcode PLANE_USER_UUID @ИМЯ_В_PLANE
```

`@ИМЯ_В_PLANE` — имя, которое появляется в тексте комментария после выбора
пользователя из списка упоминаний Plane. Оно может содержать пробелы, например
`@Анна Иванова`.

Бот вернёт одноразовую ссылку. Передайте её соответствующему сотруднику.
Ссылка действует 60 минут по умолчанию и может быть использована только один
раз.

Альтернативно ссылку можно создать внутри контейнера:

```bash
docker compose exec notifier python -m app.cli create-link PLANE_USER_UUID \
  --mention-name "Анна Иванова"
```

Plane Webhooks v2 в документированном payload комментария передаёт текст, но
не UUID упомянутых пользователей. Поэтому для уведомлений об упоминаниях важно
задать точное имя при создании ссылки. Если конкретная версия Plane всё же
передаёт `mentioned_user_ids`, сервис использует их автоматически. Для уже
привязанного пользователя создайте новую ссылку с именем и подключите её ещё
раз — существующая запись безопасно обновится.

### Команды пользователя

- `/start` — включить уведомления;
- `/connect КОД` — подключить Plane вручную;
- `/status` — показать состояние связи;
- `/stop` — отключить уведомления;
- `/help` — справка.

## Сообщение пользователю

Уведомление о назначении содержит название задачи, номер, приоритет и срок.
Уведомление об упоминании содержит название задачи и фрагмент комментария. В
обоих случаях бот добавляет кнопку **Открыть в Plane**. Если ваша версия Plane
использует другой формат ссылок, измените `WORK_ITEM_URL_TEMPLATE` в `.env`.

Доступные подстановки:

- `{base}`;
- `{workspace_slug}`;
- `{project_id}`;
- `{work_item_id}`;
- `{sequence_id}`.

## Локальная разработка

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements-dev.txt
pytest
ruff check .
```

Для получения webhook Plane локально понадобится HTTPS-туннель. Не запускайте
более одного процесса Uvicorn: приложение использует SQLite, а Telegram polling
должен выполняться только в одном экземпляре.

## Безопасность

- запросы Plane проверяются по `X-Plane-Signature`;
- в webhook-режиме запросы Telegram проверяются по
  `X-Telegram-Bot-Api-Secret-Token`;
- коды связи одноразовые и хранятся только в виде SHA-256;
- токены пользователей Plane сервису не нужны;
- контейнер работает от непривилегированного пользователя;
- секреты не включаются в Docker-образ.

## Лицензия

MIT. Проект написан самостоятельно и не содержит кода community-мостов Plane.
