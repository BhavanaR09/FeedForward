import os
import re

ROOT = os.path.join(os.path.dirname(__file__), '..', 'templates')

patterns = [
    (r"\{\%", r"\%\}"),
    (r"\{\{", r"\}\}"),
]

issues = []
for root, dirs, files in os.walk(ROOT):
    for f in files:
        if not f.endswith('.html'):
            continue
        path = os.path.join(root, f)
        with open(path, 'r', encoding='utf-8') as fh:
            txt = fh.read()
        for a,b in patterns:
            ca = len(re.findall(a, txt))
            cb = len(re.findall(b, txt))
            if ca != cb:
                issues.append((path, a, ca, b, cb))

if not issues:
    print('No unmatched Jinja delimiters found in templates.')
else:
    print('Found potential delimiter mismatches:')
    for p,a,ca,b,cb in issues:
        print(f"{p}: {a} -> {ca}, {b} -> {cb}")
