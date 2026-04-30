# max2tg

Двухузловой мост между **Max** и **Telegram**.

- `max-bridge` живёт на российском сервере: слушает Max, упаковывает сообщения, поднимает SSH tunnel и при необходимости сам выкатывает relay на foreign-хост.
- `tg-relay` живёт на зарубежном сервере: держит Bot API, создаёт темы в Telegram forum, отправляет туда сообщения и складывает ответы из Telegram в очередь для Max.

## Как это устроено

```text
Max WebSocket
    |
    v
max-bridge (RU)
    |  SSH deploy + SSH tunnel
    v
tg-relay (foreign)
    |
    v
Telegram forum topics
```

Поток `Max -> Telegram`:

1. `max-bridge` получает событие из Max.
2. Готовит Telegram-ready batch с текстом и вложениями.
3. Отправляет batch в `tg-relay` через внутренний HTTP API по SSH tunnel.
4. `tg-relay` находит или создаёт тему Telegram и отправляет туда сообщение.

Поток `Telegram -> Max`:

1. `tg-relay` получает сообщение в теме Telegram.
2. По `topic_store` понимает, какому чату Max соответствует тема.
3. Складывает команду в локальную очередь `command_store`.
4. `max-bridge` забирает команду long-poll'ом и отправляет её в Max.

## Что нужно заранее

1. Российский Linux-сервер, где будет запущен `max-bridge`.
2. Зарубежный Linux-сервер, куда можно зайти по SSH под `root` или пользователем с passwordless sudo.
3. Telegram-бот.
4. Telegram supergroup с включёнными topics/forum.
5. Данные Max из `web.max.ru`: `__oneme_auth` и `__oneme_device_id`.

Docker и Docker Compose на bridge и foreign-хосте можно не готовить вручную: setup/bootstrap попытаются поставить их сами.

## Самый простой сценарий развёртывания

На российском bridge-сервере склонируйте репозиторий и запустите одну команду:

```bash
bash scripts/setup_bridge.sh
```

Скрипт спросит недостающие значения, создаст локальные секреты, подготовит foreign-сервер и запустит bridge через Docker Compose.

Можно передать всё флагами:

```bash
bash scripts/setup_bridge.sh \
  --foreign-admin root@relay.example.com \
  --foreign-port 22 \
  --max-token 'значение___oneme_auth_из_web.max.ru' \
  --max-device-id 'значение___oneme_device_id_из_web.max.ru' \
  --tg-bot-token '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11' \
  --tg-chat-id -1001234567890
```

Что делает `scripts/setup_bridge.sh`:

- генерирует `secrets/foreign.key`, если ключа ещё нет;
- создаёт `secrets/relay.env` для foreign relay;
- создаёт `.env` для bridge;
- по admin SSH создаёт пользователя `relay` на foreign-хосте;
- кладёт публичный ключ в `/home/relay/.ssh/authorized_keys`;
- создаёт `/home/relay/max2tg`, `data`, `logs`;
- выдаёт `relay` passwordless sudo для автоматического bootstrap;
- проверяет SSH-доступ `relay@foreign`;
- запускает `docker compose up -d --build` на bridge.

После старта `max-bridge` сам загрузит код на foreign-хост, положит туда relay `.env`, запустит `scripts/bootstrap_remote.sh`, поднимет `tg-relay` и откроет SSH tunnel.

## Подготовка Telegram

1. Создайте бота через `@BotFather` и сохраните токен.
2. Создайте **supergroup** в Telegram.
3. Включите в ней **Topics / Forum**.
4. Добавьте бота в эту группу.
5. Выдайте боту права администратора как минимум на отправку сообщений и `Manage Topics`.
6. Если нужен путь `Telegram -> Max`, отключите у бота privacy mode через `@BotFather`, иначе бот не увидит обычные сообщения в темах.
7. Узнайте числовой `TG_CHAT_ID`. Обычно это число вида `-1001234567890`.

Простой способ узнать `TG_CHAT_ID`:

1. Добавьте бота в нужную supergroup.
2. Отправьте в группу команду, например `/ping`.
3. Откройте `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`.
4. Возьмите значение `message.chat.id`.

Если нужен только путь `Max -> Telegram`, запускайте setup с `--reply-enabled false`.

## Где лежат секреты

Новый рекомендуемый путь хранит секреты файлами рядом с проектом:

- `.env` -> bridge-конфиг без многострочного приватного ключа;
- `secrets/foreign.key` -> приватный SSH-ключ для доступа bridge к foreign-хосту;
- `secrets/relay.env` -> настоящий `.env` для `tg-relay`.

`secrets/` добавлен в `.gitignore`, `.dockerignore` и исключён из remote deploy archive. В контейнер bridge он монтируется read-only как `/run/max2tg-secrets`.

## Проверка

На bridge-узле:

```bash
docker compose logs -f
```

На foreign-сервере:

```bash
ssh -i secrets/foreign.key relay@relay.example.com
cd /home/relay/max2tg
sudo docker compose ps
sudo docker compose logs -f
```

Проверка health endpoint на foreign-сервере:

```bash
curl http://127.0.0.1:8080/healthz
```

Если вы задали `--relay-host-port`, используйте этот порт вместо `8080`.

Признаки, что всё хорошо:

- на foreign-хосте контейнер `max2tg` в статусе `Up`;
- `GET /healthz` возвращает `{"status":"ok"}`;
- новое сообщение из Max создаёт тему в Telegram;
- ответ в теме Telegram возвращается в Max, если `REPLY_ENABLED=true`.

## Обновление

Если меняете код на bridge-узле и `REMOTE_DEPLOY_ENABLED=true`, обычно достаточно выполнить:

```bash
docker compose up -d --build
```

Можно также повторно запустить:

```bash
bash scripts/setup_bridge.sh
```

Повторный запуск переиспользует существующие `secrets/foreign.key`, `.env` и `secrets/relay.env`, если значения уже есть.

## Шпаргалка по env

### Bridge `.env`

| Переменная | Обязательна | Что писать |
| --- | --- | --- |
| `APP_ROLE` | да | `max-bridge` |
| `RELAY_SHARED_SECRET` | да | общий секрет для внутреннего API |
| `MAX_TOKEN` | да | значение `__oneme_auth` из `web.max.ru` |
| `MAX_DEVICE_ID` | да | значение `__oneme_device_id` из `web.max.ru` |
| `MAX_CHAT_IDS` | нет | список chat id через запятую, если хотите слушать не всё |
| `REPLY_ENABLED` | нет | `true` или `false` |
| `DEBUG` | нет | `true` или `false` |
| `RELAY_TUNNEL_LOCAL_PORT` | нет | локальный порт туннеля, обычно `18080` |
| `RELAY_BIND_PORT` | нет | указывать только если на relay меняли bind port |
| `RELAY_HOST_PORT` | нет | нужен при `REMOTE_DEPLOY_ENABLED=false`, если relay опубликован на другом localhost-порту |
| `FOREIGN_SSH_HOST` | да | IP/домен foreign-сервера |
| `FOREIGN_SSH_PORT` | нет | обычно `22` |
| `FOREIGN_SSH_USER` | да | `relay` |
| `FOREIGN_SSH_PRIVATE_KEY_FILE` | рекомендуется | путь к ключу внутри контейнера, обычно `/run/max2tg-secrets/foreign.key` |
| `FOREIGN_SSH_PRIVATE_KEY` | legacy | полный приватный SSH-ключ, если file-based вариант не используется |
| `FOREIGN_APP_DIR` | нет | обычно `/home/relay/max2tg` |
| `REMOTE_DEPLOY_ENABLED` | нет | `true` по умолчанию |
| `FOREIGN_RELAY_ENV_FILE` | рекомендуется | путь к relay env внутри контейнера, обычно `/run/max2tg-secrets/relay.env` |
| `FOREIGN_RELAY_ENV_B64` | legacy | base64 содержимого relay env, если file-based вариант не используется |

### Relay `.env`

| Переменная | Обязательна | Что писать |
| --- | --- | --- |
| `APP_ROLE` | да | `tg-relay` |
| `RELAY_SHARED_SECRET` | да | тот же секрет, что и на bridge |
| `TG_BOT_TOKEN` | да | токен Telegram-бота |
| `TG_CHAT_ID` | да | id Telegram forum supergroup |
| `RELAY_BIND_HOST` | нет | обычно `0.0.0.0` |
| `RELAY_BIND_PORT` | нет | обычно `8080` |
| `RELAY_HOST_PORT` | нет | задайте, только если `127.0.0.1:8080` на foreign-хосте уже занят |
| `TOPIC_DB_PATH` | нет | обычно `data/topics.sqlite3` |
| `COMMAND_DB_PATH` | нет | обычно `data/commands.sqlite3` |
| `MESSAGE_DB_PATH` | нет | обычно `data/messages.sqlite3` |
| `REPLY_ENABLED` | нет | `true`, если нужен путь `Telegram -> Max` |
| `DEBUG` | нет | `true` или `false` |

## Legacy/manual сценарий

Если не хотите использовать `scripts/setup_bridge.sh`, старый путь всё ещё работает:

1. Создайте ключ вручную.
2. Подготовьте foreign-пользователя `relay` вручную.
3. В `.env` можно указать `FOREIGN_SSH_PRIVATE_KEY` inline.
4. Relay env можно положить в `FOREIGN_RELAY_ENV_B64`.
5. Запустите bridge:

```bash
docker compose up -d --build
```

Если не хотите давать `relay` passwordless sudo, поставьте `REMOTE_DEPLOY_ENABLED=false`, разверните `tg-relay` вручную и оставьте SSH-доступ только для tunnel.

## Docker

Используется один и тот же образ для обеих ролей.

`docker-compose.yml` публикует relay-порт только в `127.0.0.1` foreign-хоста:

```text
127.0.0.1:${RELAY_HOST_PORT:-${RELAY_BIND_PORT:-8080}}:${RELAY_BIND_PORT:-8080}
```

То есть `tg-relay` не торчит наружу даже тогда, когда внутри контейнера слушает `0.0.0.0`.

## Внутренний API relay

- `POST /internal/telegram-batch`
- `GET /internal/max-commands/pull`
- `POST /internal/max-commands/{id}/ack`
- `GET /healthz`

Все внутренние endpoint'ы, кроме `/healthz`, требуют заголовок:

```text
X-Relay-Secret: <RELAY_SHARED_SECRET>
```

## Тесты

Для локальных проверок:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Покрываются:

- role-aware конфиг и валидация env;
- batching `Max -> relay`;
- relay HTTP API;
- очередь `Telegram -> Max`;
- сквозной round-trip двухузловой схемы.
