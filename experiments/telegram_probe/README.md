# Telegram feasibility probe (RES-001)

Одноразовый инструмент исследования, **не каркас продукта**. Он хранит одну тестовую роль `probe` на чат только в памяти: перезапуск удаляет назначения, результаты и дедупликацию. PostgreSQL остаётся целевой БД пилота.

## Установка и автоматические тесты

Нужен CPython 3.12. Из корня репозитория:

### Linux/macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r experiments/telegram_probe/requirements.lock
python -m pip install --no-deps ./experiments/telegram_probe
python -m unittest discover -s experiments/telegram_probe/tests -v
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\experiments\telegram_probe\requirements.lock
python -m pip install --no-deps .\experiments\telegram_probe
python -m unittest discover -s experiments\telegram_probe\tests -v
```

Тесты не читают токен и не обращаются в сеть. `requirements.lock` фиксирует runtime-зависимости, включая транзитивные; это не выбор стека будущего приложения.

## Безопасная настройка и получение ID

1. Создайте отдельного тестового бота через BotFather и добавьте его только в тестовый чат. Токен не публикуйте.
2. Из корня репозитория выполните `cp experiments/telegram_probe/.env.example .env` (PowerShell: `Copy-Item experiments/telegram_probe/.env.example .env`) и внесите токен. Все команды ниже запускаются из **корня репозитория**; `.env` игнорируется Git.
3. Оставьте списки ID пустыми и запустите `telegram-probe --discover-ids` (PowerShell и Unix одинаково внутри активного venv).
4. Отправьте `/start` в личном чате. В группе с Privacy Mode используйте доставляемую команду `/start@ActualBot` либо явное обращение `@ActualBot #probe`, подставив username из строки старта. Терминал покажет только `chat_id` и `user_id`, без текста. Остановите Ctrl+C.
5. Запишите ID тестовых чатов в `PROBE_ALLOWED_CHAT_IDS`, а ID операторов — в `PROBE_OPERATOR_USER_IDS`. Затем перезапустите **без** `--discover-ids`.

Discovery намеренно принимает ID из любого доставленного update, поэтому используйте отдельного бота и завершите режим сразу после сбора. Обычный режим игнорирует чаты вне allowlist; назначать может только пользователь одновременно из списка операторов и с текущим статусом администратора Telegram.

При старте выполняются настоящие `getMe` и `getWebhookInfo`. Username берётся из `getMe`, не из конфигурации. При неверном токене/недоступном API выводится тип безопасной ошибки. Если установлен webhook, запуск прекращается: инструмент **не удаляет webhook и не сбрасывает pending updates автоматически**. Удалите webhook осознанно через безопасный административный процесс, не помещая URL с токеном в историю shell или отчёт.

## Ручной сценарий

1. Ответьте командой `/probe_assign` или `/probe_assign@ActualBot` на обычное (не пересланное) сообщение участника. Подставьте username из `getMe`; команда другому боту игнорируется. Боты отклоняются; назначение связано только с числовым ID.
2. Отправьте `@ActualBot #probe` и `@ActualBot @probe`, подставив username, показанный при старте.
3. Ответы используют HTML-ссылки `tg://user?id=...`, исходные `message_id` и `message_thread_id`. Имена HTML-экранируются; пояснение пользователя вообще не повторяется.
4. Для нескольких частей установите `PROBE_CHUNK_SIZE=1` и небольшую `PROBE_CHUNK_DELAY_SECONDS`. Между частями удалите участника/закройте тему/удалите исходник согласно протоколу.

Polling явно запрашивает `message` и `chat_member`. Актуальное членство через `getChatMember` проверяется и перед назначением по reply, и перед каждой частью отправки; неизвестный статус или ошибка проверки означают отказ. Между успешной проверкой и отправкой остаётся неизбежная гонка. `creator`, `administrator` и `member` считаются присутствующими, `restricted` — только при `is_member=true`; при выходе назначение удаляется, а возвращение его не восстанавливает. События членства из чатов вне allowlist игнорируются. Ошибка отправки не переносит ответ в общий чат (`allow_sending_without_reply=False`); автоматических повторов timeout нет.

Диагностика сбоев содержит только этап, класс исключения, исход операции и числовые идентификаторы для сопоставления. Текст исключения, update, сообщение и тело ответа не журналируются; timeout отправки отмечается как `uncertain`, а известный сбой — как `send_failed`.

Полная матрица ручного исследования и поля фиксации — [docs/research/RES-001.md](../../docs/research/RES-001.md). Не используйте массовые запросы: этот probe ограничивает размер части пятью участниками, но не является production rate limiter или постоянной очередью.
