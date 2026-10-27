# Журнал реализации веб-платформы

Дата начала: 26 октября 2026, Asia/Vladivostok.

Основа: всё рабочее дерево `tg-bot-imagepublisher`, HEAD `a10f440ea4ceb7c82e3aac3a6f68b5aa5b854636`, включая незакоммиченные и новые файлы. Исходники сохранены отдельным ZIP до правок; `.env`, рабочие данные и виртуальные окружения в исходный пакет не включены. Разработка — в изолированной копии. Боевой runtime не запускался.

## S0 — исходное состояние

- Согласованный read-only SQLite Backup API snapshot хранится приватно вне релиза. `integrity_check=ok`, `foreign_key_check=0`.
- Числа сущностей: users 1, channels 2, jobs 60, media_items 12, publications 9, job_events 34, channel_member_snapshots 2, news_tasks 4. Подробный инвентарь с ID и схемой сохранён рядом со снимком, без токенов.
- Сохранены все прежние изменения. AGENTS.md в репозитории и доступных родительских каталогах не обнаружены.
- Исходная регрессия Python 3.12.13: 384 passed / 1 failed за 26.42 с. Сбой `test_offline_lmstudio_allows_bot_start_with_fallback_but_fails_smoke_test` возникает в окружении без optional news-worker dependencies; требуется повтор после установки полного профиля.
- Повтор исходной регрессии после установки news-worker: **385 passed за 17.43 с**. Исходный сбой был вызван неполным набором зависимостей окружения.
- Не использовать SQLite в OneDrive как новое активное размещение; переход в локальную папку только по явному плану.

## План S1–S8

| Этап | Реализация / доказательства |
|---|---|
| S1 | Журнал схемы, legacy upgrade на копии, версии Job/Channel, CAS, receipts, общий PlatformService; T-D01–D10/T-P04–P07. |
| S2 | Отдельный control store, роли/permits/gate, журнал попыток, durable inbox/operations, host/runtime; T-P10/P13/P16, T-A14/A15, T-M02–M04. |
| S3 | FastAPI typed contracts, session/CSRF/Telegram auth, media, CRUD/queue/channel APIs; T-A01–A03/T-S01–S09. |
| S4 | Русский graphite React/TS UI, адаптивность, autosave/conflicts, календарь, история/прогресс; реальные screenshots и T-U. |
| S5 | Mini App, отдельные worker credentials/lease, подготовленная очередь без домашнего AI; внешние клиенты проверять отдельно. |
| S6 | Lock files, manifest release, install Windows/Ubuntu, snapshot/restore/standby; WAL/hash/path проверки. |
| S7 | Три направления через controller, сохранённый checkpoint, retirement/handoff/lost ACK; fault injection, реальный VPS отдельно. |
| S8 | Полная регрессия, обновление копии реальной структуры, security/visual/performance evidence, инструкции и точные непроверенные пункты. |

## Решения

- Общие правила остаются в application services, HTTP и React — адаптеры.
- Долгосрочные сессии и миграционный прогресс пишутся в host state, отдельно от SQLite контента.
- Пароль Argon2id; local bootstrap code 60 с во fragment; Telegram replay допускает только тот же nonce в ограниченное окно. Краткий повторяемый Telegram token response шифруется в control store.
- Производственные mock-данные и фиктивный прогресс запрещены. Отсутствующий runtime/worker/внешний доступ возвращает объяснимое недоступное состояние.
- Детерминированный транспорт между локальными узлами доказывает протокол и отказы, но не является испытанием Windows ↔ Ubuntu или реального SSH/VPS.

## Внешние условия приёмки

В текущей сессии нет выделенного тестового Telegram-канала, разрешённого VPS Ubuntu 24.04 и Telegram iOS/Android/Desktop. Реальные публикации, установка на VPS и смена боевого размещения не разрешены поручением. Подготовить релиз и точные сценарии перед запросом внешних доступов.

## Итог доступной реализации, 27 октября

- S1–S3: общая модель/операции/host/API/auth реализованы, SQLite upgrade на согласованной копии сохраняет исходные поля/ID; интеграционные проверки CAS, gate, idempotency, неизвестных результатов и replay включены в регрессию.
- S4–S5: живой React frontend, OpenAPI types, отдельный домашний worker и Telegram adapters реализованы. Проверены 5 размеров окна и автосохранение после reload. Внешние Telegram-клиенты/модели не испытывались.
- S6–S7: deterministic release, installers, passive provision, backup/restore, три направления controller/SSH/bridge реализованы. Проверены локальные fault fixtures и два реальных процесса. Отдельно исправлены initial standby activation, Windows bot launcher и безопасный offline legacy cutover.
- S8: итоговые тесты, ZIP, manifest, снимки и отчёты собраны в `output/web-platform/2026-10-27`. Внешняя приёмка остаётся открытой по конкретным пунктам release notes; локальная панель не объявляется завершением всех внешних испытаний.
- Изолированные изменения перенесены в исходное рабочее дерево с проверкой исходных хешей, без конфликтов; чужие изменения не откатывались. Исходные `.env` и SQLite не заменялись, marker принятия исходного checkout не создавался в ходе тестов.
- Безопасность: общий повторный допуск долгих административных действий не применялся. После автоматического отклонения такого варианта реализован точный допуск сессии к конкретным operation/action; новая чувствительная операция по-прежнему требует свежего подтверждения.
