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

# дубль-сообщение бота (тот же текст) = реакция по reaction_key, но то же состояние по state_key
assert tg.state_key({'text': 'a', 'buttons': []}) == tg.state_key({'text': 'a', 'buttons': []})
assert tg.reaction_key({'text': 'a', 'buttons': [], 'n': 1}) != tg.reaction_key({'text': 'a', 'buttons': [], 'n': 2})

# в CLI не осталось обращений к внешним моделям (критерий приёмки эпика)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg-use.py')).read()
for banned in ('ChatAnthropic', 'ChatOpenAI', 'ChatBrowserUse', 'fallback_llm', 'llm_timeout',
               'Agent(', 'api_key', 'API_KEY', 'base_url', 'z.ai', 'openai', 'anthropic'):
    assert banned not in src, f'в CLI осталось обращение к модели: {banned}'
print('ok')
