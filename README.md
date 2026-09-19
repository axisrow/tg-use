# tg-use
tg-use — агент-тестировщик/копировщик Telegram-ботов через web.telegram.org на browser-use

## Установка

```sh
env -u ALL_PROXY -u all_proxy pip3 install -r requirements.txt   # обход pip-прокси-бага
python3 -m playwright install chromium
```

## Логин

```sh
python3 tg-use.py login   # откроется окно с QR — отсканируй телефоном один раз
```

Сессия сохраняется в `~/.tg-agent-profile` (= пароль от аккаунта) — повторный
запуск подхватит её без QR.
