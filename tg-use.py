"""tg-use — тестировщик/копировщик Telegram-ботов через web.telegram.org (клиент /k/).

Единственная runtime-зависимость — browser-use: навигация, ожидания и чтение DOM
живут внутри него. Наш код — только CLI, конфиг Browser/LLM и функции скелета.

Безопасность: темп 2–3 c между действиями; только диалоги с ботами, ни одного
сообщения живым людям; ~/.tg-agent-profile = пароль (полный доступ к аккаунту).
"""

import argparse
import asyncio
import json
import os

from browser_use import Agent
from browser_use.browser import BrowserSession
from browser_use.llm import ChatAnthropic, ChatOpenAI

TG_URL = 'https://web.telegram.org/k/'
PROFILE = os.path.expanduser('~/.tg-use-agent-profile')  # = пароль: полный доступ к аккаунту


def llm():
    """LLM контура browser-use: любой OpenAI-совместимый endpoint (ключ и base_url в env), fallback — Anthropic."""
    if os.environ.get('OPENAI_API_KEY'):
        return ChatOpenAI(model=os.environ.get('OPENAI_MODEL', 'gpt-4o-mini'))
    return ChatAnthropic(model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-5'))


async def ask(task: str) -> str:
    """Единственный мост к браузеру: один ask = один прогон агента browser-use."""
    browser = BrowserSession(user_data_dir=PROFILE, headless=False)
    history = await Agent(task=task, llm=llm(), browser=browser).run()
    return history.final_result() or ''


def parse_state(raw: str) -> dict:
    state = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
    return {'text': state.get('text', ''), 'buttons': state.get('buttons', [])}


async def read_state() -> dict:
    """JSON: текст последнего сообщения бота + подписи кнопок."""
    raw = await ask(
        'Ты в открытом чате Telegram. Прочитай последнее сообщение ОТ БОТА (не своё) '
        'и подписи всех inline-кнопок под ним. Верни ТОЛЬКО JSON без пояснений: '
        '{"text": "<текст сообщения>", "buttons": ["<подпись>", ...]}'
    )
    return parse_state(raw)


async def click(label: str) -> str:
    return await ask(f'Нажми кнопку «{label}» в текущем чате и дождись реакции бота.')


async def cmd_login() -> None:
    """Headed-логин; QR сканирует человек, сессия живёт в ~/.tg-agent-profile."""
    browser = BrowserSession(user_data_dir=PROFILE, headless=False)
    await browser.start()
    await (await browser.must_get_current_page()).goto(TG_URL)
    try:
        input(
            'Открылось окно Telegram: если QR — отсканируй его телефоном '
            '(Telegram → Настройки → Устройства → Привязать устройство); '
            'если список чатов — сессия уже живая. Enter, когда готово. '
        )
    except EOFError:  # запущено без tty: браузер держится, пока процесс не остановят
        print('stdin закрыт: останови процесс (Ctrl+C/kill), когда закончишь.')
    finally:
        await browser.stop()
    print(f'Сессия сохранена в {PROFILE}; следующий запуск подхватит её без QR.')


def main() -> None:
    parser = argparse.ArgumentParser(prog='tg-use', description='Тестировщик Telegram-ботов через web.telegram.org')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('login', help='headed-логин web.telegram.org (QR сканирует человек)')
    p_ask = sub.add_parser('ask', help='одна задача агенту browser-use')
    p_ask.add_argument('task')
    sub.add_parser('read', help='состояние текущего чата как JSON')
    p_click = sub.add_parser('click', help='нажать inline-кнопку по подписи')
    p_click.add_argument('label')
    args = parser.parse_args()

    if args.cmd == 'login':
        asyncio.run(cmd_login())
    elif args.cmd == 'ask':
        print(asyncio.run(ask(args.task)))
    elif args.cmd == 'read':
        print(json.dumps(asyncio.run(read_state()), ensure_ascii=False, indent=2))
    elif args.cmd == 'click':
        print(asyncio.run(click(args.label)))


if __name__ == '__main__':
    main()
