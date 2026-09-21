"""Самопроверка рук CLI на stub-странице: python3 test_hands.py (offline, без сети и браузера)."""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import urllib.error
import urllib.request

_spec = importlib.util.spec_from_file_location(
    'tg_use', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg-use.py'))
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)
tg.WAIT, tg.POLL = 0.2, 0.05  # ускоренный таймаут тишины — тест не ждёт 12 c
tg.OPEN_POLL = 0.01  # и паузу проб open — иначе revive-тест ждал бы 30 c


class FakeEl:
    def __init__(self):
        self.clicked = 0

    async def click(self):
        self.clicked += 1


class StubPage:
    """Страница-заглушка: evaluate отдаёт ответы по очереди; лишнее касание = падение теста."""

    def __init__(self, evals=(), elements=()):
        self.evals = list(evals)
        self.elements = list(elements)
        self.selected = 0
        self.enters = 0

    async def evaluate(self, js, arg=None):
        assert self.evals, 'неожиданный вызов evaluate (лишнее касание страницы)'
        return self.evals.pop(0)

    async def get_elements_by_css_selector(self, sel):
        self.selected += 1
        return self.elements

    async def press(self, key):
        self.enters += 1


def st(text, buttons=(), n=1):
    return {'text': text, 'buttons': list(buttons), 'n': n, 'bodyLen': 10}


def j(v):
    return json.dumps(v, ensure_ascii=False)


def expect(exc_type, fn, fragment):
    try:
        asyncio.run(fn) if asyncio.iscoroutine(fn) else fn()
    except exc_type as e:
        assert fragment in str(e), f'{fragment!r} нет в {e!r}'
        return
    raise AssertionError(f'ожидали {exc_type.__name__} с {fragment!r}, не дождались')


# deny-лист: отказ до первого касания страницы
page = StubPage()
expect(RuntimeError, tg.do_click(page, 'Yes, delete it'), 'deny')
expect(RuntimeError, tg.do_click(page, 'Включить'), 'deny')
assert not page.evals and page.selected == 0

# промах кнопки: поиск вернул -1, до элементов дело не дошло
page = StubPage([j(st('Меню', ['Bots'])), '-1'])
expect(RuntimeError, tg.do_click(page, 'Нет такой'), 'нет под последним сообщением')
assert page.selected == 0

# успешный клик: клик по найденной кнопке, реакция по n+1
el = FakeEl()
s1, s2 = st('Меню', ['Bots'], n=1), st('Раздел Bots', ['Back'], n=2)
page = StubPage([j(s1), '0', j(s2)], [el])
out = asyncio.run(tg.do_click(page, 'Bots'))
assert out['text'] == s2['text'] and el.clicked == 1 and page.enters == 0

# send: не /-команда отвергается до касания страницы (предохранитель от живых людей)
page = StubPage()
expect(RuntimeError, tg.do_send(page, 'привет'), 'только команды')
assert not page.evals

# send: успех — вставка текста и Enter
s3 = st('Ответ бота', n=3)
page = StubPage([j(s1), 'typed:true', j(s3)])
out = asyncio.run(tg.do_send(page, '/start'))
assert out['text'] == s3['text'] and page.enters == 1

# send: поле не приняло текст
page = StubPage([j(s1), 'typed:false'])
expect(RuntimeError, tg.do_send(page, '/start'), 'не удалось ввести')

# тишина: wait_reaction доходит до таймаута и падает
page = StubPage([j(st('Меню', n=1))] * 20)
expect(RuntimeError, tg.wait_reaction(page, tg.reaction_key(st('Меню', n=1))),
       'реакции бота не последовало')

# дубль-сообщение бота (тот же текст, n+1) считается реакцией, а не тишиной
page = StubPage([j(st('Меню', n=2))])
out = asyncio.run(tg.wait_reaction(page, tg.reaction_key(st('Меню', n=1))))
assert out['n'] == 2

# open: кривой username — отказ до connect (валидация формата, offline)
expect(SystemExit, tg.cmd_open('bad name!'), 'жду username')

# save: --button без --from — отказ до чтения flow и до connect
os.chdir(tempfile.mkdtemp())
expect(SystemExit, tg.cmd_save('', 'Bots', ''), '--button без --from')

# save: неизвестный --from — отказ до connect (висячее ребро)
os.makedirs('artifacts')
with open('artifacts/flow.json', 'w') as f:
    json.dump({'bot': '@x', 'states': [{'id': 'a1b2c3d4', 'text': 'Меню', 'buttons': []}],
               'edges': []}, f)
expect(SystemExit, tg.cmd_save('zzzzzz', '', ''), 'нет такого состояния')


# --- cmd_open: guard по hash (#@username), а не по display name из .chat-info ---

def oi(h, body, title='LeadHunter (8602734479)'):
    """Ответ JS_OPEN_INFO: display name без username — ровно кейс @leadhunter_..._bot."""
    return j({'hash': h, 'title': title, 'bodyLen': body})


class OpenPage:
    """Страница для cmd_open: evaluate маршрутизируется по содержимому JS-сниппета."""

    def __init__(self, info=(), found=(), has_search=True, href='https://web.telegram.org/k/'):
        self.info = list(info)
        self.found = list(found)  # ответы JS_CLICK_FOUND: peer-id или ''
        self.has_search = has_search
        self.href = href
        self.reloads = 0

    async def evaluate(self, js, arg=None):
        # маршрутизация по константам модуля: правка селекторов в tg-use.py не ломает фейк
        if js == tg.JS_OPEN_INFO:
            assert self.info, 'неожиданный JS_OPEN_INFO (лишняя проба)'
            return self.info.pop(0)
        if js == tg.JS_CLICK_FOUND:
            return self.found.pop(0)
        if js == tg.JS_SEARCH_TRIGGER:
            return 'ok'
        if 'location.href' in js:  # инлайн-проба, не константа
            return self.href
        assert js == tg.JS_OPEN_SEARCH, 'неожиданный evaluate: ' + js[:60]
        return '1' if self.has_search else '0'

    async def reload(self):
        self.reloads += 1


class FakeBrowser:
    cdp_url = 'ws://127.0.0.1:9222/devtools/browser/x'  # ws→http даст base 127.0.0.1:9222

    def __init__(self, page):
        self.page = page
        self.stopped = 0

    async def must_get_current_page(self):
        return self.page

    async def stop(self):
        self.stopped += 1


def run_open(pages):
    """cmd_open на фейках: pages по порядку connect-ов; kill подменён (записывает base, вернул 2)."""
    browsers = [FakeBrowser(p) for p in pages]
    state = {'n': 0}
    killed = []

    async def fake_connect():
        state['n'] += 1
        return browsers[state['n'] - 1]

    async def run():
        real_connect, real_kill = tg.connect, tg.kill_webk_workers
        tg.connect, tg.kill_webk_workers = fake_connect, (lambda b: (killed.append(b), 2)[1])
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                await tg.cmd_open('@leadhunter_8602734479_bot')
        finally:
            tg.connect, tg.kill_webk_workers = real_connect, real_kill
        return json.loads(out.getvalue())

    return asyncio.run(run()), browsers, killed


# поиск нашёл чат: клик по результату → hash переписан на peer-id → PASS
# (третья проба — финальная перечитка плашки после закрытия поиска)
page = OpenPage(info=[oi('#-old', 300), oi('#8602734479', 300),
                      oi('#8602734479', 300, title='LeadHunter (8602734479)')],
                found=['8602734479'])
out, browsers, killed = run_open([page])
assert out['opened'] == '@leadhunter_8602734479_bot' and out['hash'] == '#8602734479'
assert page.found == [] and page.reloads == 0 and browsers[0].stopped == 1 and killed == []

# результатов нет (username не в диалогах) — чистый SystemExit, ничего не отправлено
page = OpenPage(info=[oi('#-old', 300)], found=[''] * 10)
expect(SystemExit, lambda: run_open([page]), 'не найден в диалогах')

# клик был, но hash на peer-id не переписался → чистый SystemExit
page = OpenPage(info=[oi('#-old', 300)] + [oi('#-old', 300)] * 10 + [oi('#-old', 300)],
                found=['8602734479'])
expect(SystemExit, lambda: run_open([page]), 'не открылся')

# нет поля поиска webk → чистый SystemExit
page = OpenPage(info=[oi('#-old', 300)], has_search=False)
expect(SystemExit, lambda: run_open([page]), 'нет поля поиска')

# revive: 15 проб мёртвого UI → kill(base) → фреш-сессия → reload → wait_open revive_page → поиск → клик
dead = OpenPage(info=[oi('', 0)] * 15)
alive = OpenPage(info=[oi('#', 200),  # wait_open внутри revive_page: UI ожил
                       oi('#8602734479', 200),
                       oi('#8602734479', 300, title='LeadHunter (8602734479)')],
                 found=['8602734479'])
out, browsers, killed = run_open([dead, alive])
assert out['opened'] == '@leadhunter_8602734479_bot'
assert killed == ['http://127.0.0.1:9222'] and browsers[0].stopped == 1
assert dead.reloads == 0 and alive.reloads == 1  # reload только у фреш-сессии revive

# revive не спас: чистый SystemExit с числом убитых воркеров
dead2, still = OpenPage(info=[oi('', 0)] * 15), OpenPage(info=[oi('', 0)] * 15)
expect(SystemExit, lambda: run_open([dead2, still]), 'не ожил даже после kill 2 воркеров')


# --- kill_webk_workers: /json/close только worker'ам webk; отказ CDP HTTP — SystemExit ---

class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


calls = []


def fake_urlopen(url, timeout=None):
    calls.append(url)
    return FakeResp(j([{'id': 'w1', 'type': 'shared_worker', 'url': 'https://web.telegram.org/k/worker.js'},
                       {'id': 'p1', 'type': 'page', 'url': 'https://web.telegram.org/k/'}]))


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    assert tg.kill_webk_workers('http://127.0.0.1:9222') == 1
    assert calls == ['http://127.0.0.1:9222/json/list', 'http://127.0.0.1:9222/json/close/w1']

    def boom(url, timeout=None):
        raise urllib.error.URLError('refused')

    urllib.request.urlopen = boom
    expect(SystemExit, lambda: tg.kill_webk_workers('http://127.0.0.1:9222'), 'ничего не отправлено')
finally:
    urllib.request.urlopen = real_urlopen

# страховка расползания: unit-тесты не касаются двери наружу (CDP/браузер)
# сам файл не сканируем — его литералы живут здесь
banned = ['cdp_url', 'BrowserSession(', 'connect()']
here = os.path.dirname(os.path.abspath(__file__))
for name in ('test_skeleton.py', 'test_crawl.py'):
    src = open(os.path.join(here, name)).read()
    hits = [b for b in banned if b in src]
    assert not hits, f'{name}: unit-тест трогает дверь наружу: {hits}'
print('ok: do_click / do_send / wait_reaction / cmd_save / cmd_open (поиск+клик, guard, revive) / kill_webk_workers')
