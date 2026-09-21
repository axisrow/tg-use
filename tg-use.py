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
import urllib.request

from browser_use.browser import BrowserSession

logging.getLogger('browser_use').setLevel(logging.WARNING)  # event-логи библиотеки — не наш вывод

TG_URL = 'https://web.telegram.org/k/'
PROFILE = os.path.expanduser('~/.tg-use-agent-profile')  # = пароль: полный доступ к аккаунту
CDP_FILE = os.path.expanduser('~/.tg-use-cdp')  # login пишет сюда адрес живого браузера
ART = 'artifacts'  # артефакты: flow.json, flow.md, report.json
POLL = 2.5    # темп 2–3 c между действиями в чате
WAIT = 12.0   # потолок ожидания реакции бота после клика/отправки
OPEN_POLL = 2.0  # пауза между пробами монтирования webk в wait_open
DANGEROUS = ('delete', 'удал', 'transfer', 'revoke', 'отзыв', 'переда',
             'pay', 'оплат', 'buy', 'wallet', 'invoice',
             'turn on', 'turn off', 'enable', 'disable',
             'включ', 'выключ')  # деструктив, платежи (вне v1) и переключатели настроек бота
DANGEROUS_WORDS = re.compile(r'\b(yes|да)\b')  # подтверждения — по границе слова:
# подстрока 'yes' ловила 'greyish', а 'да,' — не ловила голое «Да» (issue #10)

JS_STATE = '''() => {
  const bubbles = [...document.querySelectorAll('.bubble.is-in')];
  const last = bubbles[bubbles.length - 1];
  if (!last) return {text: '', buttons: [],
                     bodyLen: document.body ? document.body.innerText.length : 0};
  const t = last.querySelector('.translatable-message') || last.querySelector('.message');
  const buttons = [...last.querySelectorAll('button.reply-markup-button')]
    .map(b => (b.querySelector('.reply-markup-button-text') || b).innerText.trim());
  return {text: (t ? t.innerText : '').trim(), buttons, n: bubbles.length};
}'''

# клик той же пробой, что нашла кнопку: между поиском и кликом DOM не пере-запрашивается,
# так что бот не успеет переписать сообщение и увести клик в чужую кнопку
JS_CLICK_BUTTON = '''(label) => {
  const bubbles = [...document.querySelectorAll('.bubble.is-in')];
  const last = bubbles[bubbles.length - 1];
  const btn = last && [...last.querySelectorAll('button.reply-markup-button')]
    .find(b => (b.querySelector('.reply-markup-button-text') || b).innerText.trim() === label);
  if (!btn) return false;
  btn.scrollIntoView({block: 'center'});
  for (const type of ['mousedown', 'mouseup', 'click']) {
    btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  }
  return true;
}'''

JS_TYPE = '''(text) => {
  const field = document.querySelector('.input-message-input');
  if (!field) return 'no-field';
  field.focus();
  return 'typed:' + String(document.execCommand('insertText', false, text));
}'''


def is_dangerous(label: str) -> bool:
    label = label.lower()
    return any(w in label for w in DANGEROUS) or bool(DANGEROUS_WORDS.search(label))


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
    """Ключ пробы состояния (текст + кнопки + n): для сравнения двух подряд опросов.
    Сам по себе реакцией НЕ считается: список бабблов виртуализирован и n гуляет без бота."""
    return hashlib.sha1((str(st.get('n', 0)) + st['text'] + str(sorted(st['buttons']))).encode()).hexdigest()


def reaction_seen(st: dict, before: dict, prev_rk: str) -> bool:
    """Реакция бота на действие: сменились текст/кнопки, ИЛИ вырос n — но рост верим
    только после двух подряд одинаковых проб (догрузка истории меняет n без действия бота).
    Граница эвристики: если виртуализация сдула список ниже before.n, дубль бота может
    не дотянуть до before.n — таймаут (FAIL); ложных реакций это не даёт никогда."""
    if state_key(st) != state_key(before):
        return True
    return bool(prev_rk) and st.get('n', 0) > before.get('n', 0) and reaction_key(st) == prev_rk


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


def cdp_alive() -> str:
    """Адрес живого браузера из CDP_FILE, если тот отвечает по HTTP, иначе ''."""
    try:
        cdp = open(CDP_FILE).read().strip()
    except OSError:
        return ''
    base = cdp.split('/devtools')[0].replace('ws', 'http', 1)
    try:
        urllib.request.urlopen(base + '/json/version', timeout=3).read()
    except Exception:  # файл протух или браузер умер
        return ''
    return cdp


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
    """Текст последнего сообщения бота + подписи кнопок (из живого чата).
    Несмонтированная страница ≠ пустой чат: честная ошибка вместо «бот молчит»."""
    st = json.loads(await page.evaluate(JS_STATE))
    if not (st.get('bodyLen') or st.get('n')):  # нет сообщений И нет body → UI не смонтирован
        raise RuntimeError('страница webk не смонтирована (воркеры залипли?) — оживи: open @bot')
    return st


async def wait_reaction(page, before: dict) -> dict:
    """Опрашивать состояние, пока бот не среагирует (reaction_seen); тишина — ошибка ребра."""
    deadline = time.monotonic() + WAIT
    prev_rk = ''
    while True:
        await asyncio.sleep(POLL)
        st = await read_state(page)
        if reaction_seen(st, before, prev_rk):
            return st
        prev_rk = reaction_key(st)
        if time.monotonic() > deadline:
            raise RuntimeError('реакции бота не последовало (состояние не изменилось)')


async def do_click(page, label: str) -> dict:
    """Нажать кнопку по подписи и вернуть новое состояние; отказ по deny-листу и промаху."""
    if is_dangerous(label):
        raise RuntimeError(f'«{label}» — опасная кнопка, CLI её не нажимает (deny-лист)')
    before = await read_state(page)
    if not json.loads(await page.evaluate(JS_CLICK_BUTTON, label)):
        raise RuntimeError(f'кнопки «{label}» нет под последним сообщением бота')
    return await wait_reaction(page, before)


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
    return await wait_reaction(page, before)


def shown(st: dict) -> dict:
    """Состояние для вывода/артефактов: скраб токенов + короткий id."""
    st = dict(st, text=scrub(st['text']))
    return dict(st, id=state_key(st)[:8])


async def cmd_login() -> None:
    """Headed-браузер с persistent-профилем; адрес живого браузера — в ~/.tg-use-cdp.
    Повторный login к живому Chromium подключается, а не запускает второй: второй на том
    же профиле молча пересоздаёт браузер в пустом temp-профиле (SingletonLock) — сессия
    терялась при напечатанном «Сессия сохранена»."""
    alive = cdp_alive()
    if alive:
        print(f'живой Chromium уже работает ({alive}); второй не запускаю — переиспользую его.')
        browser = await connect()
    else:
        # ponytail: зомби-браузер без CDP-файла не ловим — после kill() в finally такие
        # не остаются; поймать руками удалённый файл при живом окне можно только так
        if os.path.exists(CDP_FILE):
            os.remove(CDP_FILE)  # протухший адрес мёртвого браузера только путает
        browser = BrowserSession(user_data_dir=PROFILE, headless=False)
        await browser.start()
    try:
        page = await browser.must_get_current_page()
        await page.goto(TG_URL)
        assert browser.cdp_url  # после start() адрес всегда есть; сужает тип для pyright
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
        if alive:
            await browser.stop()  # ранее запущенный Chromium остаётся живым, адрес в файле
        else:
            await browser.kill()  # stop() оставил бы зомби-браузер без CDP-файла: повторный
            if os.path.exists(CDP_FILE):
                os.remove(CDP_FILE)  # login считал бы, что браузера нет, хотя он жив
    if alive:
        print(f'браузер остаётся живым; подкоманды подключатся через {CDP_FILE}.')
    else:
        print(f'Сессия сохранена в {PROFILE}; следующий запуск подхватит её без QR.')


# guard по location.hash: webk пишет туда peerId открытого чата (#8602734479),
# а .chat-info показывает display name — с username не совпадает (BotFather — редкое совпадение)
JS_OPEN_INFO = '''() => {
  const el = document.querySelector('.chat-info');  // верхняя плашка открытого чата
  return {hash: location.hash,
          title: el ? el.innerText.trim().split('\\n')[0] : '',
          bodyLen: document.body ? document.body.innerText.length : 0};  // 0 = не смонтирован
}'''

# локальный поиск: панель может быть свёрнута (querySelector хватает скрытого
# поля-двойника) — берём только видимое поле; если панель закрыта, открывает триггер.
JS_OPEN_SEARCH = '''(query) => {
  const fields = [...document.querySelectorAll('.input-search-input')]
    .filter(e => e.offsetParent !== null);
  if (!fields.length) return 0;
  fields[0].focus();
  fields[0].value = '';  // без очистки старый запрос глушит новый ввод
  document.execCommand('insertText', false, query);
  return 1;
}'''

JS_SEARCH_TRIGGER = '''() => {
  const t = document.querySelector('.sidebar-header-search-trigger');
  if (t) t.click();
  return !!t;
}'''

# строка результата поиска: при открытой панели видимые .chatlist-chat — только
# результаты; их текст содержит display name (BotFather | 8 576 822 users), а не
# @username — матчим нормализованно (не-буквоцифры в мусор): форматы различаются.
# Полный набор mousedown/mouseup/click — голый el.click() tweb-строку не открывает
JS_CLICK_FOUND = '''(needle) => {
  const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const el = [...document.querySelectorAll('.chatlist-chat')]
    .find(e => e.offsetParent !== null && norm(e.innerText).includes(norm(needle)));
  if (!el) return '';
  const peer = el.getAttribute('data-peer-id') || (el.getAttribute('href') || '').slice(1);
  for (const type of ['mousedown', 'mouseup', 'click']) {
    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  }
  return peer;
}'''


def kill_webk_workers(base: str) -> tuple[int, int]:
    """Убить shared-воркеры webk по CDP HTTP-базе (например http://127.0.0.1:9222).
    Их залипание = вечная загрузка вкладки; /json — единственная дверь к worker-таргетам.
    Ответ /json/close контролируем живьём (200 + 'Target is closing'): если для
    worker-таргета close не сработал — честная деградация вместо молчаливого «успеха»,
    revive держится и на reload, но счёт закрытых в отчёте правдивый.
    ponytail: причина залипания не выяснена; если webk починят — просто удалить вызов.
    Глубокая дверь (WS к worker-таргету) не нужна, пока revive спасается reload'ом."""
    try:
        with urllib.request.urlopen(base + '/json/list', timeout=5) as r:
            targets = json.load(r)
        ids = [t['id'] for t in targets
               if t.get('type') == 'shared_worker' and 'web.telegram.org' in t.get('url', '')]
    except Exception as e:  # браузер висит — CDP HTTP может не ответить: чистый выход без traceback
        raise SystemExit(f'не удалось убить воркеры webk через {base} ({type(e).__name__}: {e}); '
                         f'ничего не отправлено')
    closed = 0
    for i in ids:
        try:
            with urllib.request.urlopen(f'{base}/json/close/{i}', timeout=5) as r:
                if r.status == 200 and 'closing' in r.read().decode('utf-8', 'replace').lower():
                    closed += 1
        except Exception:  # отказ отдельного close не валит revive — остаётся reload; счёт честный
            pass
    return closed, len(ids)


async def wait_open(page, tries: int, want: str | tuple = ()) -> dict:
    """Смонтированность UI; при want (str/кортеж) — ждать hash из want (чат открыт)."""
    wants = tuple(w.lower() for w in ((want,) if isinstance(want, str) else want))
    st = {}
    for _ in range(tries):
        st = json.loads(await page.evaluate(JS_OPEN_INFO))
        if st['bodyLen'] and (not wants or st['hash'].lower() in wants):
            return st
        await asyncio.sleep(OPEN_POLL)
    return st or {'hash': '', 'title': '', 'bodyLen': 0}


async def cmd_open(bot: str) -> None:
    """Открыть чат бота в живом браузере: hash-роутинг webk (#@username) + guard «webk съел хэш»; без отправок."""
    name = bot.lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{4,64}', name):  # формат для UX; в JS имя идёт параметром
        raise SystemExit(f'{bot!r}: жду username вида @name (латиница, цифры, _)')
    browser = await connect()
    try:
        page = await browser.must_get_current_page()
        url = await page.evaluate('() => location.href')
        if not url.startswith(TG_URL):
            # пустая/чужая вкладка: полный переход, руками ничего не набираем
            await page.goto(TG_URL)
        if not (await wait_open(page, 15))['bodyLen']:  # медленный старт — норма: до ~30 c
            browser, page = await revive_page(browser)  # воркеры webk залипли — оживляем
        # локальный поиск — путь username → чат; результат даёт data-peer-id для верификации
        if not json.loads(await page.evaluate(JS_OPEN_SEARCH, name)):
            await page.evaluate(JS_SEARCH_TRIGGER)  # панель поиска свёрнута — открыть кликом
            await asyncio.sleep(OPEN_POLL)
            if not json.loads(await page.evaluate(JS_OPEN_SEARCH, name)):
                raise SystemExit('нет поля поиска webk; ничего не отправлено')
        peer = ''
        for _ in range(10):  # ~20 c: индекс/рендер результатов
            peer = (await page.evaluate(JS_CLICK_FOUND, name.lower())) or ''
            if peer:
                break
            await asyncio.sleep(OPEN_POLL)
        if not peer:
            raise SystemExit(f'чат {bot} не найден в диалогах webk (поиск по username); '
                             f'ничего не отправлено')
        # tweb переписывает hash на #peerId (у ботов #<минус>peerId) или #@username
        wants = ('#' + peer, '#-' + peer, '#@' + name.lower())
        await wait_open(page, 10, want=wants)
        await asyncio.sleep(OPEN_POLL)  # дать плашке чата устаканиться после закрытия поиска
        st = json.loads(await page.evaluate(JS_OPEN_INFO))
    finally:
        await browser.stop()
    if not (st['bodyLen'] and st['hash'].lower() in wants):
        raise SystemExit(f'чат {bot} не открылся (hash: {st["hash"] or "пусто"}, '
                         f'заголовок: {st["title"] or "пусто"}); ничего не отправлено')
    print(json.dumps({'opened': bot, 'title': st['title'], 'hash': st['hash']}, ensure_ascii=False))


async def revive_page(browser) -> tuple:
    """Оживить залипшую страницу: kill воркеров при отключённом клиенте + reload фреш-сессией.
    Сессия, пережившая залипание, отваливается от таргета и молча не делает reload."""
    await browser.stop()
    base = (browser.cdp_url or '').split('/devtools')[0].replace('ws', 'http', 1)
    closed, total = kill_webk_workers(base)
    browser = await connect()
    try:  # SystemExit не Exception: глотаем всё, но фреш-сессию останавливаем и перебрасываем
        page = await browser.must_get_current_page()
        await page.reload()
        if not (await wait_open(page, 15))['bodyLen']:  # перемонтаж занимает ~5 c — ждём честно
            raise SystemExit(f'webk не ожил даже после kill {closed}/{total} воркеров + '
                             f'перезапуска сессии')
        return browser, page
    except BaseException:
        await browser.stop()
        raise


async def live_page() -> tuple:
    """Подключиться к живому браузеру; залипшую страницу оживить на месте. → (browser, page)."""
    browser = await connect()
    page = await browser.must_get_current_page()
    # ponytail: порог 8 проб ≈ 16 c — компромисс: живой медленный старт (норма до ~30 c)
    # чаще переживает, а мёртвую вкладку не тянем полминуты; убитые живые начнутся — поднять до 15
    if (await wait_open(page, 8))['bodyLen']:
        return browser, page
    return await revive_page(browser)


async def cmd_state() -> None:
    browser, page = await live_page()
    try:
        print(json.dumps(shown(await read_state(page)), ensure_ascii=False, indent=2))
    finally:
        await browser.stop()


async def cmd_click(label: str) -> None:
    browser, page = await live_page()
    try:
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
    browser, page = await live_page()
    try:
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
    browser, page = await live_page()
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
    p_open = sub.add_parser('open', help='открыть чат бота: поиск webk по username + клик, guard по peer-id hash, без отправок')
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
