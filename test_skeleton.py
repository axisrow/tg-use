"""Самопроверка скелета: python3 test_skeleton.py (без фреймворков)."""
import importlib.util
import os

spec = importlib.util.spec_from_file_location('tg_use', 'tg-use.py')
assert spec and spec.loader
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

assert tg.PROFILE.endswith('.tg-use-agent-profile')
assert tg.check_expect('Привет, меню готово', {'contains': 'меню'})[0]
assert not tg.check_expect('Привет', {'contains': 'меню'})[0]
assert tg.check_expect('Токен истекает 2026-09-20', {'regex': r'\d{4}-\d{2}-\d{2}'})[0]
assert not tg.check_expect('Привет', {'regex': r'^меню$'})[0]
assert tg.check_expect('любой', None)[0]  # шаг без expect проходит всегда
ok, why = tg.check_expect('abc', {'contains': 'x', 'regex': r'\d'})
assert not ok and why.startswith('в тексте нет')
assert not tg.check_expect('abc', {'containes': 'a'})[0]  # опечатка в expect = падение, не ложный PASS

# реакция = смена текста/кнопок, ИЛИ рост n, подтверждённый двумя подряд одинаковыми пробами
a = {'text': 'a', 'buttons': ['b'], 'n': 1}
assert tg.state_key(a) == tg.state_key({'text': 'a', 'buttons': ['b'], 'n': 5})  # n сам по себе не меняет состояние
assert tg.reaction_seen({'text': 'другое', 'buttons': ['b'], 'n': 1}, a, '')  # смена контента — реакция сразу
assert not tg.reaction_seen(dict(a), a, '')  # тишина
grown = {'text': 'a', 'buttons': ['b'], 'n': 2}
assert not tg.reaction_seen(grown, a, '')  # рост n первой пробой не верим: список бабблов догружается сам
assert tg.reaction_seen(grown, a, tg.reaction_key(grown))  # две подряд одинаковые пробы — верим
assert not tg.reaction_seen({'text': 'a', 'buttons': ['b'], 'n': 0}, a, tg.reaction_key(grown))  # n не вырос

# в CLI не осталось обращений к внешним моделям (критерий приёмки эпика)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg-use.py')).read()
for banned in ('ChatAnthropic', 'ChatOpenAI', 'ChatBrowserUse', 'fallback_llm', 'llm_timeout',
               'Agent(', 'api_key', 'API_KEY', 'base_url', 'z.ai', 'openai', 'anthropic'):
    assert banned not in src, f'в CLI осталось обращение к модели: {banned}'
print('ok')
