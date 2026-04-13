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
2. Зарубежный Linux-сервер, куда можно зайти по SSH.
3. Docker и Docker Compose на российском сервере.
4. Telegram-бот.
5. Telegram supergroup с включёнными topics/forum.
6. Данные Max из `web.max.ru`: `__oneme_auth` и `__oneme_device_id`.
7. SSH-ключ, которым российский сервер будет заходить на foreign-сервер.

## Самый простой сценарий развёртывания

Ниже описан самый прямой и рабочий путь: `max-bridge` сам загружает код на foreign-сервер, сам кладёт туда `.env`, сам ставит Docker/Compose при необходимости и сам запускает `tg-relay`.

### Шаг 1. Подготовьте Telegram

1. Создайте бота через `@BotFather` и сохраните токен.
2. Создайте **supergroup** в Telegram.
3. Включите в ней **Topics / Forum**.
4. Добавьте бота в эту группу.
5. Выдайте боту права администратора как минимум на отправку сообщений и `Manage Topics`.
6. Если нужен путь `Telegram -> Max`, отключите у бота privacy mode через `@BotFather`, иначе бот не увидит обычные сообщения в темах.
7. Узнайте числовой `TG_CHAT_ID` этой группы. Обычно это число вида `-1001234567890`.

Простой способ узнать `TG_CHAT_ID`:

1. Добавьте бота в нужную supergroup.
2. Отправьте в группу команду, например `/ping`.
3. Откройте `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`.
4. Возьмите значение `message.chat.id`.

Если нужен только путь `Max -> Telegram`, можно оставить `REPLY_ENABLED=false`.

### Шаг 2. Подготовьте foreign-сервер

Примеры ниже рассчитаны на Ubuntu/Debian. На других дистрибутивах логика та же.

Сначала на российском bridge-узле создайте отдельную пару SSH-ключей для доступа к foreign-серверу:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/max2tg_relay -C max2tg-relay
cat ~/.ssh/max2tg_relay.pub
```

Публичную часть добавьте пользователю `relay`, приватную потом вставьте в `FOREIGN_SSH_PRIVATE_KEY`.

Создайте отдельного пользователя `relay`, подготовьте SSH и каталог приложения:

```bash
sudo adduser --disabled-password --gecos "" relay

sudo install -d -m 700 -o relay -g relay /home/relay/.ssh
sudo install -d -m 755 -o relay -g relay /home/relay/max2tg
sudo install -d -m 755 -o relay -g relay /home/relay/max2tg/data
sudo install -d -m 755 -o relay -g relay /home/relay/max2tg/logs
```

Добавьте публичный SSH-ключ, соответствующий приватному ключу, который потом положите в `FOREIGN_SSH_PRIVATE_KEY` на bridge-узле:

```bash
sudo sh -c 'printf "%s\n" "ssh-ed25519 AAAA... ваш_публичный_ключ" > /home/relay/.ssh/authorized_keys'
sudo chown relay:relay /home/relay/.ssh/authorized_keys
sudo chmod 600 /home/relay/.ssh/authorized_keys
```

Дайте `relay` безпарольный `sudo`, потому что автодеплой делает именно это:

- при необходимости ставит Docker;
- при необходимости ставит Docker Compose;
- запускает Docker daemon;
- запускает `docker compose up -d --build`.

Самый простой и предсказуемый вариант для выделенного single-purpose сервера:

```bash
echo 'relay ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/90-relay-max2tg >/dev/null
sudo chmod 440 /etc/sudoers.d/90-relay-max2tg
sudo visudo -cf /etc/sudoers.d/90-relay-max2tg
```

Какие права важны:

- `/home/relay/.ssh` -> `0700`, владелец `relay:relay`
- `/home/relay/.ssh/authorized_keys` -> `0600`, владелец `relay:relay`
- `/home/relay/max2tg` -> `0755`, владелец `relay:relay`
- `/home/relay/max2tg/data` -> `0755`, владелец `relay:relay`
- `/home/relay/max2tg/logs` -> `0755`, владелец `relay:relay`

Этого достаточно. Дополнительные ACL обычно не нужны.

### Шаг 3. Подготовьте `.env` для foreign relay

Возьмите шаблон [.env.relay.example](./.env.relay.example) и сделайте из него, например, локальный файл `.env.relay`.

Минимальный рабочий пример:

```env
APP_ROLE=tg-relay
RELAY_SHARED_SECRET=change_me_to_a_long_random_secret
TG_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TG_CHAT_ID=-1001234567890
RELAY_BIND_HOST=0.0.0.0
RELAY_BIND_PORT=8080
TOPIC_DB_PATH=data/topics.sqlite3
COMMAND_DB_PATH=data/commands.sqlite3
REPLY_ENABLED=true
DEBUG=false
```

Что сюда писать:

- `RELAY_SHARED_SECRET` -> длинную случайную строку; она должна быть одинаковой на обоих узлах
- `TG_BOT_TOKEN` -> токен из `@BotFather`
- `TG_CHAT_ID` -> числовой id Telegram supergroup/forum
- `RELAY_BIND_HOST` -> оставьте `0.0.0.0`
- `RELAY_BIND_PORT` -> обычно `8080`
- `TOPIC_DB_PATH` -> обычно `data/topics.sqlite3`
- `COMMAND_DB_PATH` -> обычно `data/commands.sqlite3`
- `REPLY_ENABLED=true` -> если хотите ответы из Telegram обратно в Max

Если на foreign-хосте уже занят `127.0.0.1:8080`, добавьте ещё:

```env
RELAY_HOST_PORT=18080
```

Потом закодируйте содержимое `.env.relay` в base64 одной строкой.

Linux/macOS:

```bash
base64 < .env.relay | tr -d '\n'
```

PowerShell:

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content .env.relay -Raw)))
```

Сохраните результат: это значение пойдёт в `FOREIGN_RELAY_ENV_B64` на bridge-узле.

### Шаг 4. Заполните `.env` для российского bridge-узла

Скопируйте шаблон [.env.example](./.env.example) в `.env`:

```bash
cp .env.example .env
```

Минимально важные поля:

```env
APP_ROLE=max-bridge
RELAY_SHARED_SECRET=change_me_to_a_long_random_secret

MAX_TOKEN=значение___oneme_auth_из_web.max.ru
MAX_DEVICE_ID=значение___oneme_device_id_из_web.max.ru

REPLY_ENABLED=true
DEBUG=false

FOREIGN_SSH_HOST=relay.example.com
FOREIGN_SSH_PORT=22
FOREIGN_SSH_USER=relay
FOREIGN_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"
FOREIGN_APP_DIR=/home/relay/max2tg

REMOTE_DEPLOY_ENABLED=true
FOREIGN_RELAY_ENV_B64=сюда_вставьте_base64_из_предыдущего_шага
```

Что куда писать:

- `MAX_TOKEN` -> значение ключа `__oneme_auth` из `web.max.ru`
- `MAX_DEVICE_ID` -> значение ключа `__oneme_device_id` из `web.max.ru`
- `RELAY_SHARED_SECRET` -> та же строка, что и в `.env.relay`
- `FOREIGN_SSH_HOST` -> IP или домен foreign-сервера
- `FOREIGN_SSH_USER` -> `relay`
- `FOREIGN_SSH_PRIVATE_KEY` -> весь приватный ключ целиком, который соответствует `/home/relay/.ssh/authorized_keys`
- `FOREIGN_APP_DIR` -> `/home/relay/max2tg`
- `FOREIGN_RELAY_ENV_B64` -> base64-строка из шага 3
- `REPLY_ENABLED` -> `true`, если нужен путь `Telegram -> Max`
- `MAX_CHAT_IDS` -> необязательно; если оставить пустым, bridge слушает все доступные чаты Max

Откуда брать `MAX_TOKEN` и `MAX_DEVICE_ID`:

1. Откройте `https://web.max.ru/`.
2. Авторизуйтесь.
3. Откройте DevTools.
4. Перейдите в `Application -> Local Storage -> https://web.max.ru`.
5. Скопируйте `__oneme_auth` в `MAX_TOKEN`.
6. Скопируйте `__oneme_device_id` в `MAX_DEVICE_ID`.

Примечание по SSH-ключу в `.env`:

```env
FOREIGN_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"
```

Можно и сырым многострочным значением, если источник окружения сохраняет реальные переводы строк.

### Шаг 5. Запустите bridge-узел

На российском сервере:

```bash
docker compose up -d --build
```

Что произойдёт на первом старте:

1. `max-bridge` соберёт snapshot текущего репозитория.
2. По SSH загрузит его в `/home/relay/max2tg` на foreign-сервер.
3. Положит туда `.env`, собранный из `FOREIGN_RELAY_ENV_B64`.
4. Запустит `scripts/bootstrap_remote.sh`.
5. Скрипт сам поставит Docker/Compose при необходимости и выполнит `docker compose up -d --build`.
6. `max-bridge` поднимет SSH tunnel.
7. После успешного `/healthz` начнёт слушать Max.

### Шаг 6. Проверьте, что всё поднялось

На bridge-узле:

```bash
docker compose logs -f
```

На foreign-сервере:

```bash
ssh relay@relay.example.com
cd /home/relay/max2tg
sudo docker compose ps
sudo docker compose logs -f
```

Проверка health endpoint на foreign-сервере:

```bash
curl http://127.0.0.1:8080/healthz
```

Если вы задали `RELAY_HOST_PORT`, используйте его вместо `8080`.

Признаки, что всё хорошо:

- на foreign-хосте контейнер `max2tg` в статусе `Up`
- `GET /healthz` возвращает `{"status":"ok"}`
- новое сообщение из Max создаёт тему в Telegram
- ответ в теме Telegram возвращается в Max, если `REPLY_ENABLED=true`

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
| `FOREIGN_SSH_PRIVATE_KEY` | да | полный приватный SSH-ключ |
| `FOREIGN_APP_DIR` | нет | обычно `/home/relay/max2tg` |
| `REMOTE_DEPLOY_ENABLED` | нет | `true` по умолчанию |
| `FOREIGN_RELAY_ENV_B64` | да, если `REMOTE_DEPLOY_ENABLED=true` | base64 содержимого `.env.relay` |

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
| `REPLY_ENABLED` | нет | `true`, если нужен путь `Telegram -> Max` |
| `DEBUG` | нет | `true` или `false` |

## Если не хотите давать `relay` безпарольный sudo

Автоматический bootstrap в текущем виде требует `root` или `passwordless sudo` на foreign-хосте. Это не пожелание README, это прямое условие в `scripts/bootstrap_remote.sh`.

Если такой доступ давать нельзя, есть рабочий обходной путь:

1. Разворачивайте `tg-relay` на foreign-сервере вручную.
2. В bridge `.env` поставьте `REMOTE_DEPLOY_ENABLED=false`.
3. Оставьте SSH-доступ, чтобы `max-bridge` мог поднять tunnel.
4. Если relay опубликован на нестандартном localhost-порту, задайте одинаковый `RELAY_HOST_PORT` и в relay `.env`, и в bridge `.env`.

В этом режиме `max-bridge` не копирует код на foreign-сервер и не запускает bootstrap, а только открывает tunnel.

## Где лежат данные

На foreign-узле:

- `data/topics.sqlite3` -> соответствия `Max chat <-> Telegram topic`
- `data/commands.sqlite3` -> очередь команд `Telegram -> Max`
- `logs/max2tg.log` -> лог контейнера внутри bind-mounted каталога `./logs`

На bridge-узле локальный `topic_store` больше не используется.

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

## Обновление

Если меняете код на российском узле и `REMOTE_DEPLOY_ENABLED=true`, обычно достаточно снова выполнить:

```bash
docker compose up -d --build
```

На следующем старте bridge заново соберёт snapshot и перезальёт relay на foreign-хост.

## Тесты

```bash
python -m pytest
```

Покрываются:

- role-aware конфиг и валидация env;
- batching `Max -> relay`;
- relay HTTP API;
- очередь `Telegram -> Max`;
- сквозной round-trip двухузловой схемы.
