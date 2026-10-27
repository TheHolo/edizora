# Конфигурация

Настройки разделены на два файла:

- `.env` — секреты и параметры конкретного окружения;
- `bot-settings.toml` — несекретное поведение приложения, которое можно хранить в Git.

## Приоритет значений

Для обычного запуска приоритет такой:

1. значения, явно переданные конструктору `Settings` в коде;
2. секция `[bot]` или `[home_worker]` из `bot-settings.toml`;
3. переменные окружения и `.env`;
4. значения по умолчанию.

Поэтому несекретный параметр из TOML намеренно перекрывает одноимённую переменную окружения. Секретные поля не разрешены в TOML и всегда приходят из `.env`, окружения или явного аргумента.

Неизвестный ключ в TOML считается ошибкой. Это защищает от опечаток и случайного commit секретов.

## `.env` основного бота

| Переменная | Обязательность | Назначение |
| --- | --- | --- |
| `BOT_TOKEN` | обязательно | токен Telegram Bot API |
| `ADMIN_IDS` | обязательно | числовые Telegram ID через запятую |
| `DATABASE_URL` | нет | SQLAlchemy URL, по умолчанию локальная SQLite |
| `DEFAULT_CHANNEL_ALIAS` | обязательно при каналах | fallback alias основного канала |
| `CHANNELS_JSON` | обязательно | JSON-объект зарегистрированных каналов |
| `HTTP_PROXY` | нет | HTTP(S)-прокси |
| `SOCKS_PROXY` | нет | SOCKS4/5-прокси; имеет приоритет над HTTP |
| `PIXIV_COOKIES` | нет | cookies для закрытых работ Pixiv |
| `NEWS_WORKER_TOKEN` | нет | включает news API и создание новостей |
| `NEWS_API_BIND_HOST` | нет | отдельный bind host; нужен, например, внутри Docker |

В `python -m app.local` общий токен генерируется в памяти, bind принудительно равен `127.0.0.1`,
а `news_model_name` берётся из выбранного AI-провайдера. Это явные параметры запуска с наивысшим приоритетом.
Настройки и секреты в `.env` при этом не перезаписываются.

Пример:

```env
BOT_TOKEN=123456:replace_with_real_token
ADMIN_IDS=123456789,987654321
DATABASE_URL=sqlite+aiosqlite:///./data/database.db
DEFAULT_CHANNEL_ALIAS=artwork
CHANNELS_JSON='{"artwork":{"chat_id":"-1001234567890","title":"Artwork","publish_mode":"auto"}}'
HTTP_PROXY=
SOCKS_PROXY=
PIXIV_COOKIES=
NEWS_WORKER_TOKEN=
```

## `CHANNELS_JSON`

Значение должно быть JSON-объектом на одной строке. Alias — ключ объекта:

```json
{
  "artwork": {
    "chat_id": "-1001234567890",
    "title": "Artwork",
    "publish_mode": "auto",
    "publish_interval_seconds": 3600,
    "enabled": true
  }
}
```

В `.env` этот JSON обычно заключается в одинарные кавычки.

| Поле канала | Значение |
| --- | --- |
| `chat_id` | обязательный Telegram chat ID |
| `title` | отображаемое название; по умолчанию alias |
| `publish_mode` | `auto`, `photo` или `document` |
| `publish_interval_seconds` | начальный интервал нового канала |
| `caption_template` | доверенный HTML-шаблон художественной подписи |
| `tags` | автоматические теги канала, например `["arknights"]`; по умолчанию `[]` |
| `enabled` | доступность канала, по умолчанию `true` |

После создания интервал и основной канал сохраняются в базе и не перезаписываются TOML/JSON при каждом старте. Остальные перечисленные поля синхронизируются.

Теги канала добавляются перед ручными тегами и тегами источника, с удалением дублей и соблюдением общего лимита. Они видны в карточке канала, предпросмотре и публикации, даже при отключённых тегах источника. При переносе черновика в другой канал меняются и его автоматические теги. Полностью ручная подпись изображения остаётся целиком под вашим управлением, включая хештеги. Если `tags` отсутствует в JSON существующего канала, сохранённые теги сохраняются; `"tags":[]` их очищает.

## Секция `[bot]`

| Параметр | По умолчанию | Ограничение и назначение |
| --- | ---: | --- |
| `storage_path` | `./storage` | папка временных и загруженных файлов |
| `log_level` | `INFO` | уровень Python logging |
| `worker_count` | `1` | от 1 до 8 публикационных workers |
| `max_job_attempts` | `3` | от 1 до 10 попыток |
| `download_timeout` | `60` | общий HTTP timeout, минимум 5 секунд |
| `max_download_size_mb` | `47` | от 1 до 47 MiB |
| `max_tags` | `20` | максимум итоговых тегов |
| `max_tag_length` | `64` | длина нормализованного тега |
| `max_urls_per_message` | `10` | от 1 до 50 ссылок в batch |
| `pixiv_media_limit_enabled` | `true` | ограничивать число страниц альбома |
| `pixiv_max_images` | `10` | от 1 до 1000 выбранных изображений |
| `auto_add_source_tags` | `true` | добавлять теги источника после пользовательских |
| `auto_translate_titles` | `true` | перевод названий на английский через LM Studio, оригинал сохраняется |
| `translation_timeout` | `5` | от 1 до 30 секунд |
| `translation_lmstudio_timeout` | `60` | ожидание локального перевода, от 1 до 600 секунд |
| `translation_fallback_mymemory` | `true` | при ошибке LM Studio отправить название в MyMemory; `false` оставляет только локальный перевод |
| `delete_files_after_publish` | `true` | удалять каталог job после успеха |
| `files_ttl_hours` | `24` | зарезервирован; TTL-очистка пока не запускается |
| `timezone` | `UTC` | IANA timezone для ввода времени и дневных снимков |
| `news_api_host` | `127.0.0.1` | host news API |
| `news_api_port` | `8091` | порт от 1 до 65535 |
| `news_task_lease_seconds` | `1800` | lease от 60 до 21600 секунд |
| `news_model_name` | `gemma4:12b` | модель, которую должна объявить домашняя машина |

Пример проекта использует `Asia/Vladivostok`.

## Шаблон художественной подписи

`caption_template` может использовать:

- `{title}`;
- `{original_title}` — название без перевода;
- `{translated_title_block}` — отдельная строка перевода курсивом;
- `{description_block}`;
- `{author_block}` — готовая строка автора, без пустой ссылки; для прямых файлов отсутствует;
- `{author_name}`;
- `{author_url}`;
- `{source_url}`;
- `{source_label}`;
- `{provider_name}`;
- `{published_at_block}`;
- `{media_count}` и `{media_count_block}` — число выбранных файлов и строка для альбома;
- `{hashtags}`;
- `{hashtags_block}`.

Данные источника экранируются, но сам шаблон считается доверенным HTML. Ошибка имени placeholder обнаружится при построении предпросмотра, поэтому проверяйте новый шаблон на тестовом посте.

Старый `{title}` по-прежнему содержит перевод `(TL: …)`, если он есть. Новый стандартный шаблон использует
`{original_title}` и `{translated_title_block}`. Лимит подписи считается по видимому тексту после разбора HTML;
длины `href` и разметки не уменьшают доступное место.

## Домашний `.env`

Все поля имеют префикс `HOME_WORKER_`:

```env
HOME_WORKER_TOKEN=the_same_secret_as_news_worker_token
HOME_WORKER_VPS_API_URL=http://127.0.0.1:8091
HOME_WORKER_OLLAMA_BASE_URL=http://127.0.0.1:11434
HOME_WORKER_OLLAMA_MODEL=gemma4:12b
```

`HOME_WORKER_TOKEN` нельзя переносить в TOML.

Для локального запуска используйте вместо Ollama:

```dotenv
HOME_WORKER_LLM_PROVIDER=lmstudio
HOME_WORKER_LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
HOME_WORKER_LMSTUDIO_MODEL=
HOME_WORKER_LMSTUDIO_API_KEY=
```

Ключ API нужен только при включённой авторизации LM Studio; в TOML он запрещён.
Обычный `news-home-worker` без `llm_provider` сохраняет прежний default `ollama`.
`app.local` без явного выбора провайдера использует `lmstudio`.

В локальном режиме `/models` показывает каталог LM Studio с кнопками выбора; `python -m app.local --models` выводит тот же список в консоль. Выбор через кнопку или `--select-model "ID"` сохраняется в `<storage_path>/lmstudio-model.json` и переопределяет `lmstudio_model` для данного адреса сервера. Без выбранной модели бот запускается, новости ожидают, английский перевод использует MyMemory, если резерв включён. Смена модели применяется между заданиями; ожидающие и повторные новости получают актуальную модель при выдаче worker. Активные задания сохраняют модель до завершения. В раздельной конфигурации VPS + `app.home_worker` строгая маршрутизация по `news_model_name` сохраняется: `/models` управляет только переводом названий на машине бота.

## Секция `[home_worker]`

| Параметр | По умолчанию | Назначение |
| --- | ---: | --- |
| `vps_api_url` | `http://127.0.0.1:8091` | API VPS; удалённый адрес должен использовать HTTPS |
| `worker_id` | hostname | имя домашнего worker |
| `poll_interval_seconds` | `5` | пауза между пустыми poll |
| `lease_seconds` | `1800` | запрашиваемый lease, 60–7200 секунд |
| `request_timeout_seconds` | `30` | timeout VPS API |
| `max_retries` | `3` | число повторов VPS API |
| `retry_backoff_seconds` | `1` | начальный exponential backoff |
| `llm_provider` | зависит от команды | `lmstudio` или `ollama`, см. выше |
| `lmstudio_base_url` | `http://127.0.0.1:1234/v1` | только loopback; `/v1` добавляется, если передан лишь адрес сервера |
| `lmstudio_model` | пусто | необязательный начальный ID; локальный выбор через `/models` имеет приоритет |
| `lmstudio_timeout_seconds` | `600` | timeout одного запроса; 10–3600 секунд |
| `lmstudio_max_retries` | `2` | число повторов временных ошибок/невалидного JSON, 0–5 |
| `lmstudio_context_length` | `8192` | бюджет контекста для разбиения текста; фактический контекст задаётся в LM Studio |
| `lmstudio_max_predict_tokens` | `1600` | предел ответа; дополнительно ограничен третью контекста |
| `ollama_base_url` | `http://127.0.0.1:11434` | только loopback |
| `ollama_model` | `gemma4:12b` | локальная модель |
| `ollama_timeout_seconds` | `600` | timeout одного запроса модели |
| `ollama_max_retries` | `2` | повторы Ollama |
| `ollama_keep_alive` | `10m` | время удержания модели |
| `ollama_context_length` | `8192` | `num_ctx`, 2048–131072 |
| `ollama_max_predict_tokens` | `1600` | максимум ответа, 256–8192 |
| `temperature` | `0.1` | от 0 до 0.5 |
| `max_source_chars_per_chunk` | `24000` | первичный размер части источника |
| `max_source_chunks` | `16` | максимум частей, 1–64 |
| `source_types` | все четыре типа | `website`, `youtube`, `telegram`, `manual` |
| `log_level` | `INFO` | уровень логов worker |

Общий предел `max_source_chars_per_chunk * max_source_chunks` — 2 000 000 символов.

## База данных

SQLite:

```env
DATABASE_URL=sqlite+aiosqlite:///./data/database.db
```

PostgreSQL:

```env
DATABASE_URL=postgresql+asyncpg://user:password@host/database
```

Для PostgreSQL установите extra:

```bash
python -m pip install -e '.[postgres]'
```

Схема создаётся при старте. Для старых MVP-баз применяются встроенные additive migrations; полноценного миграционного фреймворка в проекте пока нет.

Для хранения данных вне OneDrive на Windows:

```dotenv
DATABASE_URL=sqlite+aiosqlite:///C:/Edizora/data/database.db
```

```toml
[bot]
storage_path = "C:/Edizora/storage"
```

Редактируйте существующую секцию `[bot]`. Перенос текущей базы и storage делайте при остановленном боте;
одна смена путей не переносит сохранённые задания. Скрипты Windows работают из корня проекта, поэтому `.env`
и относительные пути не зависят от того, откуда вы запустили скрипт.
