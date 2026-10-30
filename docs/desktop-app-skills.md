# Skills для реализации desktop-приложения

Поиск выполнен 2026-10-28 по инструкции локального `find-skills`: просмотрен [каталог skills.sh](https://skills.sh/), выполнен `npx --yes skills find tauri`, проверены карточки и исходные репозитории. Новые skills не установлены: на этом этапе подготовлен набор для реализации.

Счётчики ниже — приблизительный снимок каталога, а не гарантия качества. Репозиторий [Vercel](https://github.com/vercel-labs/agent-skills) имел около 33,1 тыс. stars, [Anthropic](https://github.com/anthropics/skills) — около 184 тыс. Проверка популярности и владельца не заменяет чтение конкретной версии инструкций перед установкой.

| Skill | Источник / установки | Применение |
| --- | --- | --- |
| [vercel-react-best-practices](https://skills.sh/vercel-labs/agent-skills/vercel-react-best-practices) | Vercel, ~844 тыс. | React-rendering, async-зависимости, размер bundle; брать применимые к клиентскому React правила |
| [web-design-guidelines](https://skills.sh/vercel-labs/agent-skills/web-design-guidelines) | Vercel, ~791 тыс. | Проверка интерфейса, доступности и взаимодействий |
| [frontend-design](https://skills.sh/anthropics/skills/frontend-design) | Anthropic, ~1 млн | Визуальная система, аккуратный рабочий интерфейс; сохранить идентичность Edizora |
| [webapp-testing](https://skills.sh/anthropics/skills/webapp-testing) | Anthropic, ~190 тыс. | Проверки frontend через Playwright, screenshots, логи; дополнение к native E2E |

Команды установки выбранных навыков (выполнять на этапе реализации после чтения содержимого; не устанавливать весь репозиторий целиком):

```powershell
npx skills add vercel-labs/agent-skills --skill vercel-react-best-practices
npx skills add vercel-labs/agent-skills --skill web-design-guidelines
npx skills add anthropics/skills --skill frontend-design
npx skills add anthropics/skills --skill webapp-testing
```

Для воспроизводимости записать выбранный commit источника и фактический путь установки. React-skill содержит также Next.js-рекомендации: не переносить SSR/RSC и deployment на Vercel в desktop SPA. Design-skill не должен превращать рабочую панель в рекламный лендинг. Browser testing не покрывает Rust, tray, sidecar, installer и shutdown; это отдельные Windows-проверки. Общий networkidle не использовать как критерий готовности экранов с polling.

## Что найдено для Tauri

CLI нашёл `nodnarbnitram/claude-code-extensions@tauri-v2` (~8,1 тыс. установок; карточка округляла до 8,0 тыс.). Однако [исходный репозиторий](https://github.com/nodnarbnitram/claude-code-extensions) имел всего 16 stars, автор сторонний. [Карточка](https://skills.sh/nodnarbnitram/claude-code-extensions/tauri-v2) также содержит непроверенные обещания экономии времени/токенов. В основной рекомендованный набор не включён; это кандидат на отдельное изучение, не доказанная best practice.

Для desktop-оболочки использовать официальные [Tauri guides](https://v2.tauri.app/start/), [sidecar](https://v2.tauri.app/develop/sidecar/), [capabilities](https://v2.tauri.app/security/capabilities/) и [native testing](https://v2.tauri.app/develop/tests/webdriver/). Нет необходимости устанавливать сомнительный skill только ради покрытия каждого названия технологии.
