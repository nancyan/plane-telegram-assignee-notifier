# Plane Telegram Assignee Notifier

Небольшой self-hosted сервис, который отправляет пользователю личное сообщение
в Telegram, когда его назначают исполнителем задачи в Plane.

Поддерживаются два случая:

- исполнитель указан при создании задачи (`workitem.created`);
- исполнитель добавлен позднее (`workitem.updated`).

Сервис использует Plane Webhooks v2, проверяет HMAC-подпись и защищается от
повторной отправки по стабильному `event_id`.

## Как это работает

1. Администратор создаёт одноразовую ссылку для Plane User UUID.
2. Пользователь открывает ссылку и нажимает Start в Telegram.
3. Сервис сохраняет связь `Plane user UUID → Telegram private chat_id`.
4. Plane отправляет событие о создании или изменении задачи.
5. Бот пишет только новым исполнителям и только в личный чат.

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

Заполните `.env`. `PUBLIC_BASE_URL` должен быть публичным HTTPS-адресом этого
сервиса. При запуске сервис автоматически регистрирует Telegram webhook.

Сгенерировать два секрета можно, например, так:

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
- события: `Work items → Created` и `Work items → Updated`;
- сохраните скачанный секрет в `PLANE_WEBHOOK_SECRET`.

После изменения `.env` перезапустите контейнер:

```bash
docker compose up -d --force-recreate
```

Используйте именно webhook v2. Старые webhook v1 не содержат стабильного
`event_id` и `previous_attributes`.

## Подключение сотрудников

Добавьте свой числовой Telegram user ID в `ADMIN_TELEGRAM_USER_IDS`. Затем
отправьте боту:

```text
/linkcode PLANE_USER_UUID
```

Бот вернёт одноразовую ссылку. Передайте её соответствующему сотруднику.
Ссылка действует 60 минут по умолчанию и может быть использована только один
раз.

Альтернативно ссылку можно создать внутри контейнера:

```bash
docker compose exec notifier python -m app.cli create-link PLANE_USER_UUID
```

### Команды пользователя

- `/start` — включить уведомления;
- `/connect КОД` — подключить Plane вручную;
- `/status` — показать состояние связи;
- `/stop` — отключить уведомления;
- `/help` — справка.

## Сообщение пользователю

Уведомление содержит название задачи, номер, приоритет, срок и кнопку
**Открыть в Plane**. Если ваша версия Plane использует другой формат ссылок,
измените `WORK_ITEM_URL_TEMPLATE` в `.env`.

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

Для получения webhook локально понадобится HTTPS-туннель. Не запускайте более
одного процесса Uvicorn: приложение использует SQLite и само регистрирует один
Telegram webhook.

## Безопасность

- запросы Plane проверяются по `X-Plane-Signature`;
- запросы Telegram проверяются по `X-Telegram-Bot-Api-Secret-Token`;
- коды связи одноразовые и хранятся только в виде SHA-256;
- токены пользователей Plane сервису не нужны;
- контейнер работает от непривилегированного пользователя;
- секреты не включаются в Docker-образ.

## Лицензия

MIT. Проект написан самостоятельно и не содержит кода community-мостов Plane.
