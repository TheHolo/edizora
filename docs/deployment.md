# Развёртывание

Для этой машины основной сценарий — **Windows + LM Studio**, бот и worker запускаются одной командой.
Пошаговая установка с нуля находится в [README](../README.md#установка-с-нуля-на-windows).
Ниже сохранены варианты для Linux/systemd и Docker Compose.

## Windows: запуск, обновление и восстановление

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\run-local.ps1 -Check
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\run-local.ps1 -SmokeTest
.\start-local.cmd
```

Скрипты используют `.venv-local` и сами переходят в корень проекта. При установленном uv зависимости
берутся из `uv.lock`; запасной путь — Python/pip. LM Studio запускается отдельно.
Автозапуск Windows не устанавливается скриптом: сначала проверьте ручной запуск и публикацию в тестовый канал.

Перед обновлением остановите бота через Ctrl+C и выполните:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\run-local.ps1 -Backup
```

Затем обновите исходники (`git pull --ff-only`, если нет локальных незакоммиченных изменений), повторите
установку зависимостей и обе проверки. `.env` не перезаписывается, модели не скачиваются автоматически.

Для отката SQLite:

1. Остановите все процессы, использующие базу.
2. Сохраните текущую базу и существующие файлы `database.db-wal`/`database.db-shm` в отдельной папке.
3. Положите выбранную проверенную `.sqlite3`-копию по пути `DATABASE_URL`, под именем рабочей базы.
4. Убедитесь, что рядом не остались старые `-wal`/`-shm` от заменённой базы; сохраните их вместе со старой базой.
5. Восстановите совместимую версию исходников, `.env` и нужный storage, затем выполните `-Check`.

Резервная копия не содержит `.env`, файлы медиа и веса модели. Сама SQLite содержит данные публикаций
и должна храниться с теми же правами доступа, что рабочая база.

## Подготовка Telegram

1. Создайте бота через [@BotFather](https://t.me/BotFather).
2. Узнайте числовые Telegram ID администраторов.
3. Добавьте бота администратором в каждый целевой канал.
4. Разрешите публикацию сообщений.
5. Узнайте chat ID каналов; обычно он начинается с `-100`.

## Установка systemd с нуля

Команды ниже рассчитаны на Ubuntu 24.04 и каталог `/opt/edizora`.

### 1. Пакеты

```bash
sudo apt update
sudo apt install -y ca-certificates git python3.12 python3.12-venv util-linux
python3.12 --version
```

`util-linux` нужен скрипту обновления для `flock`.

### 2. Системный пользователь и код

```bash
sudo useradd --system --user-group \
  --home-dir /opt/edizora \
  --shell /usr/sbin/nologin \
  edizora

sudo install -d -o edizora -g edizora -m 0750 \
  /opt/edizora

sudo -u edizora git clone \
  --branch main --single-branch \
  REPLACE_WITH_REPOSITORY_URL \
  /opt/edizora

cd /opt/edizora
```

Для приватного репозитория заранее настройте deploy key или другой read-only доступ под `edizora`. Каталог назначения перед `git clone` должен быть пустым.

### 3. Python

```bash
sudo -u edizora python3.12 -m venv \
  /opt/edizora/.venv

sudo -u edizora \
  /opt/edizora/.venv/bin/python -m pip install --upgrade pip

sudo -u edizora \
  /opt/edizora/.venv/bin/python -m pip install -e \
  /opt/edizora
```

Варианты extras:

```bash
# SOCKS
python -m pip install -e '.[proxy]'

# PostgreSQL
python -m pip install -e '.[postgres]'

# Оба
python -m pip install -e '.[proxy,postgres]'
```

В production выполняйте эти команды через `sudo -u edizora` и Python из созданного venv.

### 4. Конфигурация

```bash
sudo -u edizora cp .env.example .env
sudo chmod 0600 .env
sudoedit .env
```

Проверьте загрузку:

```bash
sudo -u edizora \
  /opt/edizora/.venv/bin/python -c \
  "from app.config import Settings; s=Settings(); print(len(s.admin_ids), len(s.channels_json))"
```

Секреты не должны находиться в `bot-settings.toml`.

### 5. Unit

```bash
sudo cp deploy/edizora.service \
  /etc/systemd/system/edizora.service

sudo systemctl daemon-reload
sudo systemctl enable --now edizora
sudo systemctl status edizora --no-pager
```

Unit запускает:

```text
/opt/edizora/.venv/bin/python -m app.main
```

и использует `WorkingDirectory=/opt/edizora`, поэтому Pydantic находит `.env` и `bot-settings.toml` без отдельного `EnvironmentFile`.

### 6. Проверка

```bash
sudo journalctl -u edizora -n 100 --no-pager
```

В Telegram выполните `/health full`, затем создайте тестовый черновик и опубликуйте его в тестовый канал.

## Обновление systemd

Скрипт `deploy/update.sh`:

- проверяет пользователя, unit, каталог и ветку;
- берёт `flock`, чтобы не запускаться параллельно;
- требует чистые tracked-файлы;
- разрешает только fast-forward от `origin/main`;
- останавливает сервис;
- копирует SQLite в `data/backups`;
- обновляет код и зависимости;
- запускает `compileall`;
- стартует сервис и проверяет, что он не перезапустился за первые секунды.

Запуск:

```bash
cd /opt/edizora
sudo ./deploy/update.sh
```

Переменные скрипта при нестандартной установке:

- `SERVICE_NAME`;
- `APP_USER`;
- `GIT_REMOTE`;
- `DEPLOY_BRANCH`;
- `APP_ROOT`.

Пример:

```bash
sudo DEPLOY_BRANCH=main GIT_REMOTE=origin ./deploy/update.sh
```

Скрипт автоматически пытается снова запустить сервис, если обновление упало после остановки.

## Резервные копии и откат

Перед каждым systemd-обновлением локальная SQLite копируется в:

```text
data/backups/database-YYYYMMDD-HHMMSS.db
```

PostgreSQL этим скриптом не резервируется; настройте `pg_dump` отдельно.

Для отката кода безопаснее создать в Git новый revert commit и снова выполнить обычное обновление. Если требуется восстановить SQLite:

1. остановите сервис;
2. сохраните отдельную копию текущей базы;
3. замените `data/database.db` выбранной резервной копией;
4. проверьте владельца и режим файла;
5. запустите сервис и `/health full`.

Не восстанавливайте старую базу поверх работающего процесса.

## Docker Compose

```bash
cp .env.example .env
# заполните .env
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Compose использует:

- volume `bot-data` для `/app/data`;
- volume `bot-storage` для `/app/storage`;
- restart policy `unless-stopped`;
- публикацию news API только на `127.0.0.1:8091` хоста.

Остановка:

```bash
docker compose down
```

Volumes при этом сохраняются. Команда `docker compose down -v` удалит данные и для обычного обновления не нужна.

Обновление:

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=100 bot
```

Перед обновлением отдельно сделайте резервную копию Docker volume или SQLite-файла из volume.

## Домашний worker как отдельный процесс

На домашней машине можно запускать worker вручную, пользовательским systemd unit или планировщиком. Ему нужны:

- репозиторий и virtualenv с extra `news-worker`;
- `.env` с `HOME_WORKER_TOKEN`;
- запущенный Ollama;
- закрытый маршрут до news API VPS.

Команда процесса:

```bash
news-home-worker
```

Проверка одной задачи:

```bash
news-home-worker --once
```

Не запускайте несколько домашних workers с одинаковым `worker_id`, если хотите различать их в статусах. Атомарный lease защищает саму задачу от одновременной выдачи.

## Права и каталоги

Рекомендуемая структура:

```text
/opt/edizora/
├── .git/
├── .venv/
├── .env
├── app/
├── data/
├── deploy/
├── storage/
├── bot-settings.toml
└── pyproject.toml
```

Весь рабочий каталог должен принадлежать `edizora`, а `.env` иметь режим `0600`. Не запускайте application process от root.
