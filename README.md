# max2tg

Двухузловой мост между **Max** и **Telegram**:

- `max-bridge` на российском сервере держит соединение с Max, форматирует сообщения и скачивает вложения.
- `tg-relay` на зарубежном сервере держит Telegram Bot API, создает темы и принимает ответы из Telegram обратно в очередь для Max.

`max-bridge` сам по SSH разворачивает `tg-relay`, поднимает SSH tunnel и общается с relay по приватному HTTP API.

## Как это работает

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
Telegram topics
```

Поток Max -> Telegram:

1. `max-bridge` получает событие из Max.
2. Формирует Telegram-ready batch с уже подготовленным текстом/подписями и байтами вложений.
3. Отправляет batch в `tg-relay` по внутреннему API через SSH tunnel.
4. `tg-relay` находит или создает тему Telegram и отправляет туда batch.

Поток Telegram -> Max:

1. `tg-relay` получает сообщение в теме Telegram.
2. По `topic_store` определяет соответствующий чат Max.
3. Складывает команду в локальную очередь `command_store`.
4. `max-bridge` long-poll'ом забирает команду и отправляет ее в Max.

## Роли

### `APP_ROLE=max-bridge`

Используется на российском сервере.

Обязательные env:

- `RELAY_SHARED_SECRET`
- `MAX_TOKEN`
- `MAX_DEVICE_ID`
- `FOREIGN_SSH_HOST`
- `FOREIGN_SSH_USER`
- `FOREIGN_SSH_PRIVATE_KEY`
- `FOREIGN_RELAY_ENV_B64`, если `REMOTE_DEPLOY_ENABLED=true`

Опционально:

- `MAX_CHAT_IDS`
- `REPLY_ENABLED`
- `DEBUG`
- `FOREIGN_SSH_PORT`
- `FOREIGN_APP_DIR`
- `RELAY_TUNNEL_LOCAL_PORT`
- `REMOTE_DEPLOY_ENABLED`

### `APP_ROLE=tg-relay`

Используется на зарубежном сервере.

Обязательные env:

- `RELAY_SHARED_SECRET`
- `TG_BOT_TOKEN`
- `TG_CHAT_ID`

Опционально:

- `RELAY_BIND_HOST`
- `RELAY_BIND_PORT`
- `TOPIC_DB_PATH`
- `COMMAND_DB_PATH`
- `REPLY_ENABLED`
- `DEBUG`

## Быстрый старт

### 1. Подготовьте foreign relay env

Возьмите шаблон [.env.relay.example](./.env.relay.example), заполните его и закодируйте в base64 одной строкой.

Linux/macOS:

```bash
base64 -w0 .env.relay.example
```

PowerShell:

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content .env.relay.example -Raw)))
```

Полученную строку вставьте в `FOREIGN_RELAY_ENV_B64` на российском сервере.

### 2. Заполните env для bridge-узла

Скопируйте [.env.example](./.env.example) в `.env` и заполните:

```bash
cp .env.example .env
```

SSH key note for `.env` / `docker compose env_file`:

```env
FOREIGN_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"
```

Raw multiline key input also works if your environment source preserves real line breaks.

Главное:

- `MAX_TOKEN` и `MAX_DEVICE_ID` берутся из `web.max.ru`
- `FOREIGN_SSH_PRIVATE_KEY` содержит приватный ключ целиком
- `RELAY_SHARED_SECRET` должен совпадать на обоих узлах

### 3. Запустите bridge-узел

```bash
docker compose up -d --build
```

На старте `max-bridge`:

1. собирает snapshot текущего репозитория;
2. загружает его на зарубежный сервер;
3. кладет туда foreign `.env`;
4. при необходимости ставит Docker/Compose и запускает `docker compose up -d --build`;
5. поднимает SSH tunnel;
6. после успешного `/healthz` стартует Max listener.

## Режим без автодеплоя

Если relay уже развернут вручную, можно оставить туннель, но отключить загрузку snapshot:

```env
REMOTE_DEPLOY_ENABLED=false
```

В этом режиме `max-bridge` не копирует код на зарубежный сервер и только открывает SSH tunnel.

## Docker

Один и тот же образ используется для обеих ролей.

`docker-compose.yml` всегда публикует relay-порт только на `127.0.0.1` хоста:

```text
127.0.0.1:${RELAY_BIND_PORT}:${RELAY_BIND_PORT}
```

Поэтому `tg-relay` не торчит наружу, даже если внутри контейнера слушает `0.0.0.0`.

## Внутренний API relay

- `POST /internal/telegram-batch`
- `GET /internal/max-commands/pull`
- `POST /internal/max-commands/{id}/ack`
- `GET /healthz`

Все внутренние endpoint'ы, кроме `/healthz`, требуют заголовок:

```text
X-Relay-Secret: <RELAY_SHARED_SECRET>
```

## Хранилища

На relay-узле:

- `TOPIC_DB_PATH` — соответствия `Max chat <-> Telegram topic`
- `COMMAND_DB_PATH` — очередь команд `Telegram -> Max`

На bridge-узле локальный `topic_store` больше не используется.

## Тесты

```bash
python -m pytest
```

Покрываются:

- role-aware конфиг и валидация env;
- batching Max -> relay;
- relay HTTP API;
- очередь Telegram -> Max;
- сквозной round-trip двухузловой схемы.
