"""tg-use — руки для агент-харнеса: тестировщик Telegram-ботов через web.telegram.org (клиент /k/).

В CLI нет ни одного обращения к внешней модели: мозг — модель харнеса, который
запускает этот CLI (README: два режима browser-use; наш — Browser Harness CLI +
скилл). browser-use используется как библиотека браузера (BrowserSession):
навигация, ожидания и клики — его зона. Встроенный агент со своим циклом не
используется — обход и выбор следующего шага делает харнес (см. SKILL.md).

Безопасность: темп 2–3 c между действиями задаёт харнес между вызовами CLI;
только диалоги с ботами; deny-лист мутационных кнопок; скраб токенов в каждом
выводе; ~/.tg-use-agent-profile = пароль (полный доступ к аккаунту).
"""

import os

os.environ.setdefault('BROWSER_USE_LOGGING_LEVEL', 'warning')  # до импорта: чистый CLI-вывод

import argparse
import asyncio
import hashlib
import json
import logging
import re
import time

from browser_use.browser import BrowserSession

logging.getLogger('browser_use').setLevel(logging.WARNING)  # event-логи библиотеки — не наш вывод

TG_URL = 'https://web.telegram.org/k/'
PROFILE = os.path.expanduser('~/.tg-use-agent-profile')  # = пароль: полный доступ к аккаунту
CDP_FILE = os.path.expanduser('~/.tg-use-cdp')  # login пишет сюда адрес живого браузера
ART = 'artifacts'  # артефакты: flow.json, flow.md, report.json
POLL = 2.5    # темп 2–3 c между действиями в чате
WAIT = 12.0   # потолок ожидания реакции бота после клика/отправки
DANGEROUS = ('delete', 'удал', 'transfer', 'revoke', 'отзыв', 'переда',
             'yes', 'да,', 'turn on', 'turn off', 'enable', 'disable',
             'включ', 'выключ')  # деструктив, подтверждения и переключатели настроек бота

JS_STATE = '''() => {
  const bubbles = [...document.querySelectorAll('.bubble.is-in')];
  const last = bubbles[bubbles.length - 1];
  if (!last) return {text: '', buttons: []};
  const t = last.querySelector('.translatable-message') || last.querySelector('.message');
  const buttons = [...last.querySelectorAll('button.reply-markup-button')]
    .map(b => (b.querySelector('.reply-markup-button-text') || b).innerText.trim());
  return {text: (t ? t.innerText : '').trim(), buttons, n: bubbles.length};
}'''

# индекс кнопки по подписи среди всех кнопок документа (для get_elements_by_css_selector)
JS_FIND_BUTTON = '''(label) => {
  const all = [...document.querySelectorAll('button.reply-markup-button')];
  const bubbles = [...document.querySelectorAll('.bubble.is-in')];
  const last = bubbles[bubbles.length - 1];
  const btn = last && [...last.querySelectorAll('button.reply-markup-button')]
    .find(b => (b.querySelector('.reply-markup-button-text') || b).innerText.trim() === label);
  if (!btn) return -1;
  btn.scrollIntoView({block: 'center'});
  return all.indexOf(btn);
}'''

JS_TYPE = '''(text) => {
  const field = document.querySelector('.input-message-input');
  if (!field) return 'no-field';
  field.focus();
  return 'typed:' + String(document.execCommand('insertText', false, text));
}'''


def is_dangerous(label: str) -> bool:
    return any(w in label.lower() for w in DANGEROUS)


def scrub(text: str) -> str:
    """Затереть секреты (токены ботов вида 1234567890:AA...) перед выводом/записью в артефакты."""
    # без хвостового \b: если токен кончается на -/_, границы слова нет и 1–2 символа остались бы видимыми
    return re.sub(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}', '[REDACTED]', text)


def esc(s: str, limit: int = 40) -> str:
    """Первая строка строки, обрезанная и безопасная для кавычек Mermaid."""
    return s.splitlines()[0][:limit].replace('"', "'") if s else '(нет текста)'


def state_key(st: dict) -> str:
    """Ключ дедупа состояния: sha1(текст + отсортированные подписи кнопок)."""
    return hashlib.sha1((st['text'] + str(sorted(st['buttons']))).encode()).hexdigest()


def reaction_key(st: dict) -> str:
    """Ключ для ожидания реакции: тот же ключ + число входящих сообщений,
    чтобы повтор-дубль от бота (тот же текст) считался реакцией, а не тишиной."""
    return hashlib.sha1((str(st.get('n', 0)) + st['text'] + str(sorted(st['buttons']))).encode()).hexdigest()


def check_expect(text: str, expect: dict | None) -> tuple[bool, str]:
    """Ожидания шага против текста последнего сообщения бота: contains и/или regex."""
    for kind, pat in (expect or {}).items():
        if kind not in ('contains', 'regex'):  # опечатка в сценарии = падение, не ложный PASS
            return False, f'неизвестный expect: {kind!r} (умею contains/regex)'
        if kind == 'contains' and pat not in text:
            return False, f'в тексте нет «{pat}»'
        if kind == 'regex' and not re.search(pat, text):
            return False, f'в тексте нет /{pat}/'
    return True, ''


def write_artifacts(flow: dict, d: str = ART) -> None:
    """flow.json + flow.md (Mermaid); перезаписывается целиком после каждого save."""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'flow.json'), 'w') as f:
        json.dump(flow, f, ensure_ascii=False, indent=2)
    lines = ['flowchart TD']
    for st in flow['states']:
        lines.append(f'    {st["id"]}["{esc(st["text"])}"]')
    for e in flow['edges']:
        lines.append(f'    {e["from"]} -->|"{esc(e["button"], 25)}"| {e["to"]}')
    with open(os.path.join(d, 'flow.md'), 'w') as f:
        f.write('# flow\n\n```mermaid\n' + '\n'.join(lines) + '\n```\n')


async def connect() -> BrowserSession:
    """Подключиться к браузеру, оставленному login (адрес в ~/.tg-use-cdp)."""
    try:
        cdp = open(CDP_FILE).read().strip()
    except FileNotFoundError:
        raise SystemExit('нет живого браузера: сначала python3 tg-use.py login')
    browser = BrowserSession(cdp_url=cdp)
    try:
        await browser.start()
    except Exception as e:
        raise SystemExit(f'браузер {cdp} не отвечает ({type(e).__name__}: {e}); перезапусти login')
    return browser


async def read_state(page) -> dict:
    """Текст последнего сообщения бота + подписи кнопок (из живого чата)."""
    return json.loads(await page.evaluate(JS_STATE))


async def wait_reaction(page, before_key: str) -> dict:
    """Опрашивать состояние, пока бот не среагирует; тишина — ошибка ребра."""
    deadline = time.monotonic() + WAIT
    while True:
        await asyncio.sleep(POLL)
        st = await read_state(page)
        if reaction_key(st) != before_key:
            return st
        if time.monotonic() > deadline:
            raise RuntimeError('реакции бота не последовало (состояние не изменилось)')


async def do_click(page, label: str) -> dict:
    """Нажать кнопку по подписи и вернуть новое состояние; отказ по deny-листу и промаху."""
    if is_dangerous(label):
        raise RuntimeError(f'«{label}» — опасная кнопка, CLI её не нажимает (deny-лист)')
    before = await read_state(page)
    idx = json.loads(await page.evaluate(JS_FIND_BUTTON, label))
    if idx < 0:
        raise RuntimeError(f'кнопки «{label}» нет под последним сообщением бота')
    button = (await page.get_elements_by_css_selector('button.reply-markup-button'))[idx]
    await button.click()
    return await wait_reaction(page, reaction_key(before))


async def do_send(page, text: str) -> dict:
    """Отправить команду в поле ввода и вернуть новое состояние."""
    # ponytail: только команды на / — предохранитель от сообщений живым людям; убрать, если боту нужен текст
    if not text.startswith('/'):
        raise RuntimeError('send: только команды на /')
    before = await read_state(page)
    typed = await page.evaluate(JS_TYPE, text)
    if typed != 'typed:true':
        raise RuntimeError(f'не удалось ввести «{text}» (поле ввода: {typed or "нет ответа"})')
    await page.press('Enter')
    return await wait_reaction(page, reaction_key(before))


def shown(st: dict) -> dict:
    """Состояние для вывода/артефактов: скраб токенов + короткий id."""
    st = dict(st, text=scrub(st['text']))
    return dict(st, id=state_key(st)[:8])


async def cmd_login() -> None:
    """Headed-браузер с persistent-профилем; адрес живого браузера — в ~/.tg-use-cdp."""
    browser = BrowserSession(user_data_dir=PROFILE, headless=False)
    await browser.start()
    try:
        page = await browser.must_get_current_page()
        await page.goto(TG_URL)
        with open(CDP_FILE, 'w') as f:
            f.write(browser.cdp_url)
        print(f'браузер {browser.cdp_url} — адрес записан в {CDP_FILE}; окно не закрывать до конца работы.')
        try:
            input(
                'Если QR — отсканируй телефоном (Telegram → Настройки → Устройства → '
                'Привязать устройство); если список чатов — сессия уже живая. Enter, когда закончишь. '
            )
        except EOFError:  # запущено без tty: окно держится открытым до остановки процесса
            print('stdin закрыт: окно держится открытым; останови процесс (Ctrl+C/kill), когда закончишь.')
            await asyncio.Event().wait()
    finally:
        await browser.stop()
        if os.path.exists(CDP_FILE):
            os.remove(CDP_FILE)
    print(f'Сессия сохранена в {PROFILE}; следующий запуск подхватит её без QR.')


JS_OPEN_INFO = '''() => {
  const el = document.querySelector('.chat-info');  // верхняя плашка открытого чата
  return {title: el ? el.innerText.trim().split('\\n')[0] : '',
          bodyLen: document.body ? document.body.innerText.length : 0};  // 0 = не смонтирован
}'''

# клик по чату бота в списке чатов (хэш при старте webk надёжно не читает)
JS_OPEN_CHAT = '''(name) => {
  const needle = name.toLowerCase();
  const items = [...document.querySelectorAll('a[href^="#"], [class*="chat-item"]')]
    .filter(e => e.offsetParent !== null && e.innerText);
  const el = items.find(e => e.innerText.toLowerCase().includes(needle));
  if (!el) return false;
  el.scrollIntoView({block: 'center'});
  el.click();
  return true;
}'''

JS_SET_HASH = '''(name) => { location.hash = "#@" + name; }'''


def kill_webk_workers(base: str) -> int:
    """Убить shared-воркеры webk по CDP HTTP-базе (например http://127.0.0.1:9222).
    Их залипание = вечная загрузка вкладки; /json — единственная дверь к worker-таргетам.
    ponytail: причина залипания не выяснена; если webk починят — просто удалить вызов."""
    import urllib.request
    with urllib.request.urlopen(base + '/json/list', timeout=5) as r:
        targets = json.load(r)
    ids = [t['id'] for t in targets
           if t.get('type') == 'shared_worker' and 'web.telegram.org' in t.get('url', '')]
    for i in ids:
        urllib.request.urlopen(f'{base}/json/close/{i}', timeout=5).read()
    return len(ids)


async def wait_open(page, tries: int) -> dict:
    """Заголовок открытого чата + смонтированность UI; проба до паузы (DOM-опрос — не темп)."""
    for _ in range(tries):
        st = json.loads(await page.evaluate(JS_OPEN_INFO))
        if st['title'] or st['bodyLen']:
            return st
        await asyncio.sleep(2)
    return {'title': '', 'bodyLen': 0}


async def cmd_open(bot: str) -> None:
    """Открыть чат бота в живом браузере: reload/переход + клик по чату; guard по заголовку; без отправок."""
    name = bot.lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{4,64}', name):  # формат для UX; в JS имя идёт параметром
        raise SystemExit(f'{bot!r}: жду username вида @name (латиница, цифры, _)')
    browser = await connect()
    try:
        page = await browser.must_get_current_page()
        url = await page.evaluate('() => location.href')
        if url.startswith(TG_URL):
            # reload библиотечный (CDP Page.reload): evaluate-location.reload() залипшую страницу не поднимает
            await page.evaluate(JS_SET_HASH, name)
            await page.reload()
        else:
            # пустая/чужая вкладка: полный переход, руками ничего не набираем
            await page.goto(f'{TG_URL}#@{name}')
        st = await wait_open(page, 15)  # медленный старт — норма: до ~30 c
        if not st['bodyLen']:
            # воркеры webk залипли (вечная загрузка): kill + reload ФРЕШ-сессией — сессия,
            # пережившая залипание, отваливается от таргета и молча не делает reload
            await browser.stop()
            base = (browser.cdp_url or '').split('/devtools')[0].replace('ws', 'http', 1)
            killed = kill_webk_workers(base)
            browser = await connect()
            page = await browser.must_get_current_page()
            await page.reload()
            st = await wait_open(page, 15)
            if not st['bodyLen']:
                raise SystemExit(f'webk не ожил даже после kill {killed} воркеров + перезапуска сессии; '
                                 f'ничего не отправлено')
        if name.lower() not in st['title'].lower():
            # приложение живо, но чат не открыт — клик по чату в списке
            if json.loads(await page.evaluate(JS_OPEN_CHAT, name)):
                st = await wait_open(page, 8)
            else:
                st['title'] = ''
    finally:
        await browser.stop()
    if name.lower() not in st['title'].lower():
        raise SystemExit(f'чат {bot} не открылся (заголовок: {st["title"] or "пусто"}); ничего не отправлено')
    print(json.dumps({'opened': bot, 'title': st['title']}, ensure_ascii=False))


async def cmd_state() -> None:
    browser = await connect()
    try:
        print(json.dumps(shown(await read_state(await browser.must_get_current_page())),
                         ensure_ascii=False, indent=2))
    finally:
        await browser.stop()


async def cmd_click(label: str) -> None:
    browser = await connect()
    try:
        page = await browser.must_get_current_page()
        st = shown(await do_click(page, label))
        print(f'клик «{label}» →', flush=True)
        print(json.dumps(st, ensure_ascii=False, indent=2))
    finally:
        await browser.stop()


async def cmd_save(from_id: str, button: str, bot: str) -> None:
    """Дописать текущее состояние (и ребро from --button, если задано) в flow.json/flow.md."""
    if button and not from_id:  # иначе кнопка потерялась бы молча
        raise SystemExit('--button без --from: ребро некуда прикрепить')
    try:
        flow = json.load(open(os.path.join(ART, 'flow.json')))
    except FileNotFoundError:
        flow = {'bot': bot, 'states': [], 'edges': []}
    if from_id and from_id[:8] not in {s['id'] for s in flow['states']}:
        # опечатка в id = висячее ребро и битая ссылка в Mermaid; проверяем до подключения к браузеру
        raise SystemExit(f'--from {from_id[:8]}: нет такого состояния в {ART}/flow.json')
    browser = await connect()
    try:
        page = await browser.must_get_current_page()
        st = shown(await read_state(page))
    finally:
        await browser.stop()
    new = st['id'] not in {s['id'] for s in flow['states']}
    if new:
        flow['states'].append({k: st[k] for k in ('id', 'text', 'buttons')})
    if from_id:
        edge = {'from': from_id[:8], 'to': st['id'], 'button': button}
        if edge not in flow['edges']:
            flow['edges'].append(edge)
    write_artifacts(flow)
    print(json.dumps({'id': st['id'], 'new': new, 'states': len(flow['states']),
                      'edges': len(flow['edges'])}, ensure_ascii=False))


async def cmd_test(scenario_path: str) -> None:
    """Прогнать сценарий [{do, expect}] → artifacts/report.json; падение = exit 1."""
    steps = json.load(open(scenario_path))
    browser = await connect()
    page = await browser.must_get_current_page()
    results = []
    try:
        for i, step in enumerate(steps):
            do = step.get('do', {})
            t0 = time.monotonic()
            error = ''
            try:
                if 'click' in do:
                    st = shown(await do_click(page, do['click']))
                elif 'send' in do:
                    st = shown(await do_send(page, do['send']))
                elif not do:
                    st = shown(await read_state(page))
                else:
                    raise RuntimeError(f'неизвестный шаг {json.dumps(do)}: жду {{"click"}} или {{"send"}}')
                ok, why = check_expect(st['text'], step.get('expect'))
            except Exception as e:
                ok, st, why = False, {'text': '', 'buttons': []}, f'{type(e).__name__}: {e}'
            results.append({'step': i + 1, 'do': do, 'expect': step.get('expect'),
                            'pass': ok, 'ms': round((time.monotonic() - t0) * 1000),
                            'text': st['text'], **({'error': why} if why else {})})
            print(f'шаг {i + 1}: {"ok" if ok else "FAIL"} {why}', flush=True)
            if not ok:
                break  # дальше сценарий бессмыслен: хрупкие шаги после сломанного врут
    finally:
        await browser.stop()
    passed = all(r['pass'] for r in results)
    os.makedirs(ART, exist_ok=True)
    with open(os.path.join(ART, 'report.json'), 'w') as f:
        json.dump({'scenario': os.path.basename(scenario_path), 'pass': passed, 'steps': results},
                  f, ensure_ascii=False, indent=2)
    print(f'итог: {"PASS" if passed else "FAIL"} ({len(results)} шагов) → {ART}/report.json')
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog='tg-use', description='Руки для харнеса: Telegram-боты через web.telegram.org')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('login', help='headed-браузер с persistent-профилем (QR сканирует человек)')
    p_open = sub.add_parser('open', help='открыть чат бота: deep-link + reload, guard по заголовку, без отправок')
    p_open.add_argument('bot', help='бот в форме @name')
    sub.add_parser('state', help='JSON: последнее сообщение бота + подписи кнопок')
    p_click = sub.add_parser('click', help='нажать inline-кнопку по подписи (пустая реакция = ошибка ребра)')
    p_click.add_argument('label')
    p_save = sub.add_parser('save', help='дописать состояние/ребро в artifacts/flow.json + flow.md')
    p_save.add_argument('--from', dest='from_id', default='', help='id состояния, из которого вышло ребро')
    p_save.add_argument('--button', default='', help='подпись кнопки ребра')
    p_save.add_argument('--bot', default='', help='имя бота (только для первого save)')
    p_test = sub.add_parser('test', help='прогнать сценарий → artifacts/report.json (fail = exit 1)')
    p_test.add_argument('scenario', help='JSON: [{"do": {"click"/"send": ...}, "expect": {"contains"/"regex": ...}}]')
    args = parser.parse_args()

    try:
        if args.cmd == 'login':
            asyncio.run(cmd_login())
        elif args.cmd == 'open':
            asyncio.run(cmd_open(args.bot))
        elif args.cmd == 'state':
            asyncio.run(cmd_state())
        elif args.cmd == 'click':
            asyncio.run(cmd_click(args.label))
        elif args.cmd == 'save':
            asyncio.run(cmd_save(args.from_id, args.button, args.bot))
        elif args.cmd == 'test':
            asyncio.run(cmd_test(args.scenario))
    except RuntimeError as e:  # ошибка руки (deny-лист, промах кнопки, нет реакции) — не traceback
        raise SystemExit(f'ошибка: {e}')


if __name__ == '__main__':
    main()
