# Статус проекта

**Дата проверки:** 21 октября 2026 года. **Репозиторий:** `agd01991/Teg_role_bot`. **Основная ветка:** `master`.

Подготовлен промежуточный этап **ALPHA-001 для закрытого тестирования в Telegram**. Запуск и ограничения — в [alpha/README](../alpha/README.md), фактическая проверка — в [протоколе](testing/ALPHA-001.md). Полный P0 и продуктовая приёмка A01–A63 не объявлены завершёнными.

## Ветки и база

Git/GitHub подтверждают основную ветку `master` (`ca37235`), PR #9 `codex` → `master` (`333387e`) и PR #10 `m4plkv-codex/-res-001` → `codex` (`13603e5`). PR #10 включает исправление гонки отзыва прав из #11.

- [PR #12](https://github.com/agd01991/Teg_role_bot/pull/12), `fix/pr-9-spec-consistency` → `codex`: документальные исправления #9. В альфу перенесены требования, включая уточнения четырёх последующих замечаний: граница пояснения, обычный участник без продуктового grant, глобальное удаление после restore, полный жизненный цикл маркера P1.
- [PR #13](https://github.com/agd01991/Teg_role_bot/pull/13), `fix/pr-10-probe-checks` → ветка PR #10: устранены 18 замечаний Ruff; 47 регрессий probe прошли.
- `feat/telegram-test-alpha` начинается от `35c6cdf` PR #13; её PR направляется в `fix/pr-10-probe-checks`. Эта ветка содержит интеграцию документальных исправлений, без слияния остальных PR.

Автоматическое слияние, переписывание истории и удаление веток не выполнялись.

## Реализованный объём

| Область | Результат | Проверка / оставшаяся граница |
| --- | --- | --- |
| Общие роли | Создание с одноразовым подтверждением, reply-назначение/снятие, список, вызов обычным участником | Настоящий Dispatcher + управляемый Telegram транспорт; полный жизненный цикл роли дальше |
| Доступ | Явные разрешённые чаты/операторы, свежие проверки API, версии членства, отзыв после выхода | Нативный admin человека не выдаёт продуктовые права; окончательный onboarding D-017 открыт |
| Данные | PostgreSQL, tenant FK, индексы, начальная схема, отдельный runtime-пользователь | Нет upgrade/down миграций и операционного backup/restore drill |
| Очередь | Пять адресатов/часть, лимиты, TTL, retry_after, persistent dedup, темы/reply, uncertain без повтора | Перезапуск отменяет старые pending; uncertain требует ручной сверки |
| Приватность | Opt-out, глобальное удаление, независимый журнал и replay до открытия приложения | Минимальный ID-tombstone остаётся; полная эксплуатационная процедура хранения копий ещё нужна |
| Эксплуатация | Windows launcher, секреты вне Git, локальный PostgreSQL, pinned lock/image, CI | Docker Desktop должен быть запущен; токен вводится локально |
| P1/P2 | Требования описаны | Реализации нет; личные/скрытые роли, Mini App, платежи, all недоступны |

## Точные команды и результаты

Из корня рабочей копии выполнены:

```powershell
.\start-test.ps1 -SetupOnly
.\.venv-alpha\Scripts\python.exe alpha/check.py
.\.venv\Scripts\python.exe -m black --check alpha
.\.venv\Scripts\python.exe -m ruff check alpha
```

CPython 3.12.14 / aiogram 3.31.0 / asyncpg 0.31.0 / PostgreSQL 17.11: **51 тест OK без пропусков**, 48,987 секунды. Из них 43 сценария используют настоящую отдельную БД, 9 также проходят через aiogram Dispatcher. Black 26.10.1 и Ruff 0.17.1 — без замечаний. До этого по найденным дефектам исправлены версии членства автора/получателя в очереди, гонка удаления с worker и доступ к собственному удалению в ЛС после выхода.

Для прежнего probe (aiogram 3.22.0 в отдельном окружении) выполнены:

```powershell
$env:PYTHONPATH = [IO.Path]::GetFullPath('experiments/telegram_probe/src')
.\.venv\Scripts\python.exe -m unittest discover -s experiments/telegram_probe/tests -v
.\.venv\Scripts\python.exe -m black --check experiments/telegram_probe/src experiments/telegram_probe/tests
.\.venv\Scripts\python.exe -m ruff check experiments/telegram_probe/src experiments/telegram_probe/tests
.\.venv\Scripts\python.exe -m compileall -q experiments/telegram_probe/src experiments/telegram_probe/tests
.\.venv\Scripts\python.exe -m pip check
git --git-dir=.git --work-tree=. diff --check
```

Повторно 21 октября 2026 года на неизменённых исходниках `35c6cdf`: **47 тестов OK**, Black (6 файлов), полный набор Ruff, синтаксис, зависимости и diff — успешно. Источники probe в ALPHA-001 не меняются. CI повторяет проверки альфы и probe в отдельных окружениях с PostgreSQL service для альфы.

## Что остаётся проверить

Реальный токен не предоставлялся и в файлы не записывался; приложение не запускалось с настоящим Telegram-ботом. Android/iOS/Desktop, Privacy Mode, фактические entities/конфликт username, переходы тем, ссылки по ID и уведомления требуют [ручных сценариев](testing/ALPHA-001.md) и [RES-001](research/RES-001.md). Все продуктовые A01–A63 сохраняют честный статус «не выполнялась».

Далее: получить результаты закрытого прогона, устранить найденные дефекты отдельным PR, завершить RES-001 и спланировать недостающие P0-функции. Необходимо отдельно принять onboarding, жизненный цикл ролей, политику/quiet hours, удаление чата, миграции, нагрузку, резервирование и восстановление. P1/P2 не перенесены в этот этап.


Дополнительные итоговые команды:

```powershell
.\.venv-alpha\Scripts\python.exe -m compileall -q alpha
.\.venv-alpha\Scripts\python.exe -m pip check
.\.venv-alpha\Scripts\python.exe .venv/verify_docs.py .
```

Синтаксис PowerShell проверен через Language.Parser.ParseFile, без ошибок. Compileall и pip check прошли. Одноразовый локальный verify_docs.py проверил 12 Markdown-файлов, 75 локальных ссылок/якорей, 28 таблиц, A01–A63/D-001–D-024 и P0 gate. Скрипт проверки не является runtime-зависимостью. Во время повторного прогона Docker Desktop был недоступен: этот прогон прерван и не засчитан. После сохранения его временных каталогов сокетов и перезапуска без сброса БД полный прогон повторён успешно (51/51). Обновлённая регрессия также проверяет, что события членства не создают запись удалённого профиля до собственного optin.
