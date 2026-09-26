"""Самопроверка логики crawl без сети: python3 test_crawl.py"""
import importlib.util
import json
import os
import tempfile

_spec = importlib.util.spec_from_file_location(
    'tg_use', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg-use.py'))
tg_use = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg_use)
esc, state_key, write_artifacts = tg_use.esc, tg_use.state_key, tg_use.write_artifacts
scrub = tg_use.scrub

secret = '8602734479:AAHxEXAMPLE-TOKEN_1234567890abcd'
assert scrub(f'Here is the token: {secret}') == 'Here is the token: [REDACTED]'
assert scrub(f'x {secret[:-2]}-_ конец') == 'x [REDACTED] конец'  # хвост на -/_ : без хвостового \b не осталось бы хвоста
assert scrub('bot 8602734479 без секрета') == 'bot 8602734479 без секрета'

st = {'text': 'Привет\nвторая строка', 'buttons': ['Bots', 'API']}
assert state_key(st) == state_key({'text': 'Привет\nвторая строка', 'buttons': ['API', 'Bots']})  # порядок кнопок не важен
assert state_key(st) != state_key({'text': 'Другое', 'buttons': ['API', 'Bots']})  # другой текст = другой ключ
assert esc('"Say" hi\nnext') == "'Say' hi" and esc('') == '(нет текста)' and len(esc('x' * 100)) == 40

os.chdir(tempfile.mkdtemp())
flow = {'states': [{'id': 'a1b2c3d4', 'text': 'Меню', 'buttons': ['Bots']}],
        'edges': [{'from': 'a1b2c3d4', 'to': 'e5f6a7b8', 'button': 'Bots'}]}
write_artifacts(flow)
assert json.load(open('artifacts/flow.json')) == flow
md = open('artifacts/flow.md').read()
assert 'a1b2c3d4["Меню"]' in md and 'a1b2c3d4 -->|"Bots"| e5f6a7b8' in md and md.count('```mermaid') == 1

# install_skill: копия SKILL.md в ~/.claude/skills/tg-use/ с абсолютным путём к CLI
repo = os.path.dirname(os.path.abspath(__file__))
home = tempfile.mkdtemp()
dst = tg_use.install_skill(repo, home)
assert dst == os.path.join(home, '.claude', 'skills', 'tg-use', 'SKILL.md')
installed = open(dst).read()
assert f'python3 {os.path.join(repo, "tg-use.py")}' in installed  # CLI достижим из любой папки
assert installed.startswith('---')  # frontmatter скилла не тронут
print('ok: state_key / esc / write_artifacts / install_skill')
