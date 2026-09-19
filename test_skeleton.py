"""Самопроверка скелета: python3 test_skeleton.py (без фреймворков)."""
import importlib.util

spec = importlib.util.spec_from_file_location('tg_use', 'tg-use.py')
assert spec and spec.loader
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

assert tg.parse_state('{"text": "меню", "buttons": ["/start"]}') == {'text': 'меню', 'buttons': ['/start']}
assert tg.parse_state('```json\n{"text": "a", "buttons": []}\n```') == {'text': 'a', 'buttons': []}
assert tg.PROFILE.endswith('.tg-use-agent-profile')
print('ok')
