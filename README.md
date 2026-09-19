# tg-use
tg-use — агент-тестировщик/копировщик Telegram-ботов через web.telegram.org на browser-use

## Установка

```sh
pip3 install -r requirements.txt          # в прокси-окружении: env -u ALL_PROXY -u all_proxy pip3 ...
python3 -m playwright install chromium
```

## Логин

```sh
python3 tg-use.py login   # откроется окно с QR — отсканируй телефоном один раз
```

Сессия сохраняется в `~/.tg-use-agent-profile` (= пароль от аккаунта) — повторный
запуск подхватит её без QR. При первом запуске macOS спросит доступ к «Chromium
Safe Storage»: вводи пароль входа и жми «Разрешать всегда» — это шифрование
cookies, без него сессия не переживёт перезапуск.

LLM: задай `OPENAI_API_KEY` (+ опционально `OPENAI_BASE_URL`, `OPENAI_MODEL`) —
любой OpenAI-совместимый endpoint; иначе fallback на `ANTHROPIC_API_KEY`.
