"""Самопроверка логики crawl без сети: python3 test_crawl.py"""
import importlib.util
import json
import os
import shutil
import tempfile

_spec = importlib.util.spec_from_file_location(
    'tg_use', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tg_use', '__init__.py'))
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

# install_skill: копия SKILL.md в ~/.claude/skills/tg-use/. Репо-режим: команды
# переписываются на абсолютный путь к CLI. Пакет-режим (pip install): источник —
# SKILL.md внутри пакета, команды — консольный скрипт tg-use.
repo = os.path.dirname(os.path.abspath(__file__))
home = tempfile.mkdtemp()
dst = tg_use.install_skill(repo, home)
assert dst == os.path.join(home, '.claude', 'skills', 'tg-use', 'SKILL.md')
installed = open(dst).read()
assert f'python3 {os.path.join(repo, "tg-use.py")}' in installed  # CLI достижим из любой папки
assert installed.startswith('---')  # frontmatter скилла не тронут
pkg_dir = tempfile.mkdtemp()  # фейковый site-packages/tg_use: SKILL.md внутри, .claude/ нигде выше
shutil.copy(os.path.join(repo, 'tg_use', 'SKILL.md'), os.path.join(pkg_dir, 'SKILL.md'))
pkgdst = tg_use.install_skill(pkg_dir, tempfile.mkdtemp())
pkgtext = open(pkgdst).read()
assert 'tg-use state' in pkgtext  # команды — консольный скрипт пакета
assert 'python3 tg-use.py' not in pkgtext  # не осталось репо-формы
assert open(os.path.join(repo, '.claude', 'skills', 'tg-use', 'SKILL.md')).read() == \
    open(os.path.join(repo, 'tg_use', 'SKILL.md')).read()  # копия в пакете не отстала
# main() должен передавать install_skill репо-корень (argv[0] шима), а не каталог
# пакета: `python3 tg-use.py skill-install` из репо обязан дать repo-режим
# (абсолютные пути), а не пакетный (голый tg-use, которого нет на PATH)
import subprocess
import sys
tmp_home = tempfile.mkdtemp()
subprocess.run([sys.executable, os.path.join(repo, 'tg-use.py'), 'skill-install'],
               env=dict(os.environ, HOME=tmp_home), check=True, capture_output=True)
reinstalled = open(os.path.join(tmp_home, '.claude', 'skills', 'tg-use', 'SKILL.md')).read()
assert f'python3 {os.path.join(repo, "tg-use.py")}' in reinstalled

# ревью #35: раскладка не должна зависеть от argv[0] — консольный скрипт venv
# получает argv[0]=.../bin/tg-use, и на таком repo_dir установка падала
# FileNotFoundError. Фейковый venv: bin/tg-use + site-packages/tg_use, ни
# одного .claude/ по дереву вверх
fake = tempfile.mkdtemp()
site_pkg = os.path.join(fake, 'lib', 'python3.12', 'site-packages', 'tg_use')
os.makedirs(site_pkg)
shutil.copy(os.path.join(repo, 'tg_use', '__init__.py'), os.path.join(site_pkg, '__init__.py'))
shutil.copy(os.path.join(repo, 'tg_use', 'SKILL.md'), os.path.join(site_pkg, 'SKILL.md'))
os.makedirs(os.path.join(fake, 'bin'))
with open(os.path.join(fake, 'bin', 'tg-use'), 'w') as f:
    f.write('from tg_use import main\nmain()\n')
venv_home = tempfile.mkdtemp()
r = subprocess.run([sys.executable, os.path.join(fake, 'bin', 'tg-use'), 'skill-install'],
                   env=dict(os.environ, PYTHONPATH=os.path.dirname(site_pkg), HOME=venv_home),
                   capture_output=True, text=True)
assert r.returncode == 0, f'консольный скрипт упал: {r.stderr[-400:]}'
venv_skill = open(os.path.join(venv_home, '.claude', 'skills', 'tg-use', 'SKILL.md')).read()
assert 'tg-use state' in venv_skill  # пакетный режим для консольного скрипта
assert f'python3 {os.path.join(repo, "tg-use.py")}' not in venv_skill
print('ok: state_key / esc / write_artifacts / install_skill / skill-install из репо')
