"""tg-use — тестировщик/копировщик Telegram-ботов через web.telegram.org (клиент /k/).

Единственная runtime-зависимость — browser-use: навигация, ожидания и чтение DOM
живут внутри него. Наш код — только CLI, конфиг Browser/LLM и функции скелета.

Безопасность: темп 2–3 c между действиями; только диалоги с ботами, ни одного
сообщения живым людям; ~/.tg-use-agent-profile = пароль (полный доступ к аккаунту).
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
from collections import deque

from browser_use import Agent
from browser_use.browser import BrowserSession
from browser_use.llm import ChatAnthropic, ChatOpenAI

TG_URL = 'https://web.telegram.org/k/'
PROFILE = os.path.expanduser('~/.tg-use-agent-profile')  # = пароль: полный доступ к аккаунту
MAX_DEPTH = 4    # потолок BFS по глубине
MAX_STATES = 50  # потолок BFS по числу состояний
DANGEROUS = ('delete', 'удал', 'transfer', 'revoke', 'отзыв', 'переда',
             'yes', 'да,', 'turn on', 'turn off', 'enable', 'disable',
             'включ', 'выключ')  # деструктив, подтверждения и переключатели настроек бота


def is_dangerous(label: str) -> bool:
    return any(w in label.lower() for w in DANGEROUS)


def scrub(text: str) -> str:
    """Затереть секреты (токены ботов вида 1234567890:AA...) перед записью в артефакты."""
    # без хвостового \b: если токен кончается на -/_, границы слова нет и 1–2 символа остались бы видимыми
    return re.sub(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}', '[REDACTED]', text)


def esc(s: str, limit: int = 40) -> str:
    """Первая строка строки, обрезанная и безопасная для кавычек Mermaid."""
    return s.splitlines()[0][:limit].replace('"', "'") if s else '(нет текста)'


def llm():
    """LLM контура browser-use: любой OpenAI-совместимый endpoint (ключ и base_url в env), fallback — Anthropic."""
    if os.environ.get('OPENAI_API_KEY'):
        return ChatOpenAI(model=os.environ.get('OPENAI_MODEL', 'gpt-4o-mini'))
    # 240 c: glm-5.3 через прокси — reasoning-модель, тяжёлые шаги думают дольше дефолтных 90 c
    return ChatAnthropic(model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-5'), timeout=240)


async def ask(task: str, shot: str | None = None) -> str:
    """Единственный мост к браузеру: один ask = один прогон агента browser-use.

    shot — путь для скриншота страницы в момент конца прогона (делает browser-use).
    """
    browser = BrowserSession(user_data_dir=PROFILE, headless=False)
    try:
        # транспорт закреплён за клиентом /k/: агент каждый раз начинает с чистой вкладки
        history = await Agent(
            task=f'Открой {TG_URL} и затем: {task}', llm=llm(), browser=browser,
            llm_timeout=240,  # reasoning-модели думают дольше дефолтных 60–90 c
            # fallback из эпика: при ошибке основной модели (рвёт ~50% тяжёлых tool-use запросов) — ретрай другой
            fallback_llm=ChatAnthropic(model=os.environ.get('ANTHROPIC_FALLBACK_MODEL', 'glm-4.6'), timeout=240),
        ).run()
        if shot:  # скриншот последнего шага: агент сам пишет PNG в свой tmp-каталог, дублируем к себе
            try:
                paths = [p for p in history.screenshot_paths() if p and os.path.exists(p)]
                if paths:
                    shutil.copyfile(paths[-1], shot)
            except Exception:
                pass  # скриншот — артефакт, не данные
        return history.final_result() or ''
    finally:
        await browser.stop()  # иначе Chromium-процессы текут, а профиль залочен для следующих прогонов


def parse_state(raw: str) -> dict:
    """JSON из ответа LLM: срезать markdown-забор (```json или голый ```) при наличии."""
    s = raw.strip().removesuffix('```')
    if s.startswith('```'):
        s = s.removeprefix('```json').removeprefix('```')
    state = json.loads(s.strip())
    return {'text': state.get('text', ''), 'buttons': state.get('buttons', [])}


def where(bot: str | None) -> str:
    return f' (чат с {bot}; если он не открыт — найди его в списке чатов и открой)' if bot else ''


async def read_state(bot: str | None = None, shot: str | None = None) -> dict:
    """JSON: текст последнего сообщения бота + подписи кнопок."""
    raw = await ask(
        f'Ты в чате Telegram{where(bot)}. Прочитай последнее сообщение ОТ БОТА (не своё) '
        'и подписи всех inline-кнопок под ним. Верни ТОЛЬКО JSON без пояснений: '
        '{"text": "<текст сообщения>", "buttons": ["<подпись>", ...]}',
        shot=shot,
    )
    return parse_state(raw)


async def click(label: str, bot: str | None = None) -> str:
    result = await ask(
        f'В чате Telegram{where(bot)} нажми inline-кнопку «{label}» '
        'и дождись реакции бота (нового сообщения или смены клавиатуры).'
    )
    if not result.strip():  # abort агента = клик мог не случиться; тихо читать старое состояние нельзя
        raise RuntimeError(f'клик «{label}» не подтверждён агентом (пустой ответ)')
    return result


def state_key(st: dict) -> str:
    """Ключ дедупа состояния: sha1(текст + отсортированные подписи кнопок)."""
    return hashlib.sha1((st['text'] + str(sorted(st['buttons']))).encode()).hexdigest()


def write_artifacts(flow: dict) -> None:
    """flow.json + flow.md (Mermaid); перезаписывается после каждого нового состояния,
    чтобы прерванный прогон оставлял валидные артефакты."""
    with open('flow.json', 'w') as f:
        json.dump(flow, f, ensure_ascii=False, indent=2)
    lines = ['flowchart TD']
    for st in flow['states']:
        lines.append(f'    {st["id"]}["{esc(st["text"])}"]')
    for e in flow['edges']:
        lines.append(f'    {e["from"]} -->|"{esc(e["button"], 25)}"| {e["to"]}')
    with open('flow.md', 'w') as f:
        f.write('# flow\n\n```mermaid\n' + '\n'.join(lines) + '\n```\n')


async def cmd_crawl(bot: str, depth: int, start: str) -> None:
    """BFS по меню бота: входная команда, затем обход inline-кнопок; возврат к состоянию — повтором пути."""
    depth = min(depth, MAX_DEPTH)
    os.makedirs('shots', exist_ok=True)
    flow = {'states': [], 'edges': []}
    paths = {}  # id -> [кнопки от /start до состояния]; ключ = признак «состояние уже открыто»
    kids = {}   # id -> кнопки состояния
    edges = set()

    tainted = False  # после первого показа секрета скриншоты не снимаем: кадр — вся страница,
    # сообщение с токеном остаётся во вьюпорте выше текущего состояния

    async def goto(state_id: str, label: str) -> dict:
        """Повтором пути от /start вернуться к state_id, нажать label, прочитать новое состояние."""
        if os.path.exists('shots/_cur.png'):
            os.remove('shots/_cur.png')  # протухший кадр не должен переименоваться под чужой id
        for b in paths[state_id] + [label]:
            await click(b, bot=bot)
            await asyncio.sleep(2.5)  # темп 2–3 c между действиями в чате
        return await read_state(bot=bot, shot=None if tainted else 'shots/_cur.png')

    root = None
    for attempt in range(3):  # вход под той же дисциплиной, что и рёбра: провайдер рвётся на любом ask
        try:
            await ask(f'Открой чат с {bot} в Telegram, отправь {start} и дождись ответа бота.')
            await asyncio.sleep(2.5)  # темп 2–3 c между действиями в чате
            root = await read_state(bot=bot, shot='shots/_cur.png')
            break
        except Exception as e:
            print(f'вход {attempt + 1}/3 не удался: {type(e).__name__}: {e}', flush=True)
    if root is None:
        raise SystemExit('вход в чат не удался за 3 попытки')
    root['text'] = scrub(root['text'])
    if '[REDACTED]' in root['text']:
        tainted = True
    print(f'корень: {esc(root["text"], 70)} | кнопок: {len(root["buttons"])}', flush=True)
    rid = state_key(root)[:8]
    flow['states'].append({'id': rid, 'text': root['text'], 'buttons': root['buttons']})
    if not tainted and os.path.exists('shots/_cur.png'):
        os.replace('shots/_cur.png', f'shots/{rid}.png')
    paths[rid], kids[rid] = [], root['buttons']
    queue = deque([(rid, 0)])
    fails = 0
    while queue and len(flow['states']) < MAX_STATES:
        sid, d = queue.popleft()
        if d >= depth:
            continue
        for label in kids[sid]:
            if len(flow['states']) >= MAX_STATES:
                break
            if is_dangerous(label):
                print(f'⛔ «{label}» — опасная кнопка, не нажимаю', flush=True)
                continue
            try:
                st = await goto(sid, label)
            except Exception as e:
                fails += 1
                print(f'пропуск «{label}» из {sid}: {type(e).__name__}: {e}', flush=True)
                if fails >= 3:
                    raise SystemExit('3 сбоя подряд — стоп; частичные артефакты уже записаны')
                continue
            fails = 0
            st['text'] = scrub(st['text'])
            if '[REDACTED]' in st['text']:
                tainted = True  # дальше кадры этой страницы содержат секрет — скриншоты больше не сохраняем
            tid = state_key(st)[:8]
            if tid not in paths:  # новое состояние
                flow['states'].append({'id': tid, 'text': st['text'], 'buttons': st['buttons']})
                paths[tid], kids[tid] = paths[sid] + [label], st['buttons']
                queue.append((tid, d + 1))
                if not tainted and os.path.exists('shots/_cur.png'):
                    os.replace('shots/_cur.png', f'shots/{tid}.png')
            if (sid, tid, label) not in edges:
                edges.add((sid, tid, label))
                flow['edges'].append({'from': sid, 'to': tid, 'button': label})
            write_artifacts(flow)
            print(f'+{tid}: {esc(st["text"], 60)} | кнопок: {len(st["buttons"])}', flush=True)
    write_artifacts(flow)
    print(f'готово: состояний {len(flow["states"])}, рёбер {len(flow["edges"])} → flow.json, flow.md, shots/', flush=True)


async def cmd_login() -> None:
    """Headed-логин; QR сканирует человек, сессия живёт в ~/.tg-use-agent-profile."""
    browser = BrowserSession(user_data_dir=PROFILE, headless=False)
    await browser.start()
    await (await browser.must_get_current_page()).goto(TG_URL)
    try:
        input(
            'Открылось окно Telegram: если QR — отсканируй его телефоном '
            '(Telegram → Настройки → Устройства → Привязать устройство); '
            'если список чатов — сессия уже живая. Enter, когда готово. '
        )
    except EOFError:  # запущено без tty: окно держится открытым до остановки процесса
        print('stdin закрыт: окно держится открытым; останови процесс (Ctrl+C/kill), когда закончишь.')
        await asyncio.Event().wait()  # держать браузер открытым до kill/Ctrl+C
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
    p_crawl = sub.add_parser('crawl', help='BFS по меню бота → flow.json + flow.md + shots/')
    p_crawl.add_argument('bot', help='например @botfather')
    p_crawl.add_argument('--depth', type=int, default=MAX_DEPTH, help='глубина обхода (потолок 4)')
    p_crawl.add_argument('--start', default='/start', help='входная команда; для ботов без кнопок после /start — другая, напр. /mybots')
    args = parser.parse_args()

    if args.cmd == 'login':
        asyncio.run(cmd_login())
    elif args.cmd == 'ask':
        print(asyncio.run(ask(args.task)))
    elif args.cmd == 'read':
        print(json.dumps(asyncio.run(read_state()), ensure_ascii=False, indent=2))
    elif args.cmd == 'click':
        print(asyncio.run(click(args.label)))
    elif args.cmd == 'crawl':
        asyncio.run(cmd_crawl(args.bot, args.depth, args.start))


if __name__ == '__main__':
    main()
