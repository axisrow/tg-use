# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

tg-use — тестировщик/копировщик Telegram-ботов через web.telegram.org (клиент `/k/`).
Весь CLI — один файл `tg-use.py` («руки»: login/open/state/click/save/test); процедура обхода —
скилл `.claude/skills/tg-use/SKILL.md`, а решения «куда кликать дальше» принимает
модель харнеса, запускающая CLI. Единственная runtime-зависимость — browser-use,
всё остальное — stdlib. Правила кода — в AGENTS.md (лесенка ponytail: есть в
кодовой базе? → stdlib? → платформа? → одна строка? и только потом минимум;
абстракций «на будущее», обёрток над browser-use и конфиг-фреймворков не заводить).

## Ключевой инвариант: в CLI нет обращений к внешним моделям

Мозг — модель харнеса. Встроенный агент browser-use `Agent(task=..., llm=...)` и
любые LLM-контуры (ключи, endpoints, fallback-модели) отвергнуты владельцем
2026-09-20; browser-use используется только как библиотека браузера
(`BrowserSession`: навигация, ожидания, клики — его зона). Запрет проверяется
автоматически: test_skeleton.py падает, если в tg-use.py появились строки вида
`Agent(`, `api_key`, `openai`, `anthropic`, `base_url`…

## Команды

```sh
pip3 install -r requirements.txt      # в прокси-окружении: env -u ALL_PROXY -u all_proxy pip3 ...
python3 -m playwright install chromium
browser-use skill install             # подключить браузер к харнесу

python3 test_skeleton.py              # check_expect, state_key/reaction_key, запрет LLM-строк (offline)
python3 test_crawl.py                 # is_dangerous, scrub, esc, write_artifacts (offline)
```

Тесты — plain-скрипты с assert, без pytest: гонять один раз целиком, итог читать
из вывода (`ok`). Оба проходят без сети и без браузера; test_skeleton.py ищет
`tg-use.py` по относительному пути — запускать из корня репо. Файл `tg-use.py`
содержит дефис и обычным import не грузится — тесты подключают его через
`importlib.util.spec_from_file_location`; новые тесты делать так же.

CLI (каждая подкоманда — отдельный процесс):

```sh
python3 tg-use.py login               # headed Chromium + persistent-профиль; CDP-адрес → ~/.tg-use-cdp
python3 tg-use.py open <@bot>         # открыть чат бота: поиск webk по username + клик, guard по peer-id, без отправок
python3 tg-use.py state               # JSON: последнее сообщение бота + подписи кнопок
python3 tg-use.py click "<label>"     # нажать inline-кнопку → новое состояние (промах/тишина = exit 1)
python3 tg-use.py save [--from id --button label] [--bot @name]  # дописать artifacts/flow.json + flow.md
python3 tg-use.py test scenario.json  # сценарий [{"do": {"send"/"click"}, "expect": {"contains"/"regex"}}]
                                      # → artifacts/report.json; FAIL = exit 1
```

Категории тестов/команд (граница — цена ошибки): unit — три offline-скрипта,
bare-дефолт `python3 test_skeleton.py && python3 test_crawl.py && python3
test_hands.py`; `state`/`open` — live_read; `click`/`save` — live_write
(обратимое в диалоге с ботом); `test`/`login` — live_write_danger (необратимая
отправка боту / полный доступ к аккаунту). Гейты — в `.claude/settings.json`:
владелец 2026-09-20 разрешил `Bash(python3 tg-use.py:*)` целиком — категории
остались описанием цены ошибки, а не спросом харнеса.

## Архитектура

- **Разделение рук и мозга.** CLI ничего не решает: подкоманды атомарны, выбор
  следующего шага, темп (2–3 c между вызовами) и обход графа меню — работа
  харнеса по SKILL.md (малый обход: глубина ≤2, до ~5 состояний).
- **CDP-хендшейк.** `login` держит Chromium на профиле `~/.tg-use-agent-profile`
  (= пароль от аккаунта: не коммитить, не копировать, не чистить) и пишет адрес
  живого браузера в `~/.tg-use-cdp`. Остальные подкоманды через `connect()`
  цепляются к этому живому браузеру — без работающего `login` падают с
  подсказкой. Харнес может работать с тем же окном сам: `BU_CDP_URL=$(cat ~/.tg-use-cdp)`.
- **Артефакты** (`artifacts/`, перезаписываются целиком после каждого save):
  flow.json + flow.md (Mermaid) — карта меню; report.json — результат сценария.
  Дедуп состояний: `state_key` = sha1(текст + отсортированные подписи кнопок);
  `reaction_key` — ключ пробы для `reaction_seen`: реакция = смена текста/кнопок
  или рост числа бабблов, подтверждённый двумя подряд одинаковыми пробами
  (список виртуализирован и `n` гуляет без действия бота).
- **Безопасность вшита в CLI и не упрощается**: deny-лист `DANGEROUS` для
  мутационных кнопок (delete/revoke/переключатели настроек), скраб токенов
  `scrub()` в каждом выводе и артефакте, `do_send` принимает только команды на
  `/` (предохранитель от сообщений живым людям), ожидание реакции POLL 2.5 c /
  WAIT 12 c. Только диалоги с ботами; темп между действиями задаёт харнес.
- **Ошибки рук** — `RuntimeError` → `SystemExit` без traceback: deny-лист,
  промах кнопки, тишина бота и падение сценария — exit 1, а не исключение.
