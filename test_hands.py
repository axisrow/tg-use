"""Самопроверка рук CLI на stub-странице: python3 test_hands.py (offline, без сети и браузера)."""
import asyncio
import importlib.util
import json
import os
import tempfile

_spec = importlib.util.spec_from_file_location(
    'tg_use', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg-use.py'))
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)
tg.WAIT, tg.POLL = 0.2, 0.05  # ускоренный таймаут тишины — тест не ждёт 12 c


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
    return {'text': text, 'buttons': list(buttons), 'n': n}


def j(v):
    return json.dumps(v, ensure_ascii=False)


def expect(exc_type, coro, fragment):
    try:
        asyncio.run(coro)
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

# страховка расползания: unit-тесты не касаются двери наружу (CDP/браузер)
# сам файл не сканируем — его литералы живут здесь
banned = ['cdp_url', 'BrowserSession(', 'connect()']
here = os.path.dirname(os.path.abspath(__file__))
for name in ('test_skeleton.py', 'test_crawl.py'):
    src = open(os.path.join(here, name)).read()
    hits = [b for b in banned if b in src]
    assert not hits, f'{name}: unit-тест трогает дверь наружу: {hits}'
print('ok: do_click / do_send / wait_reaction / cmd_save / cmd_open на stub-странице')
