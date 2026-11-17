import os
import sys
from jinja2 import TemplateSyntaxError, Environment, FileSystemLoader

# Prefer the application's Jinja environment so custom filters (e.g. format_datetime)
# and globals are available while compiling templates. If importing the Flask
# app fails (when running the script from a different working dir), fall back
# to a standalone Jinja Environment.
ROOT = os.path.join(os.path.dirname(__file__), '..', 'templates')
ROOT = os.path.abspath(ROOT)

# Ensure project root is on sys.path so `from app import app` works when run
# from the tools subfolder.
proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if proj_root not in sys.path:
    sys.path.insert(0, proj_root)

try:
    from app import app
    env = app.jinja_env
except Exception as e:
    env = Environment(loader=FileSystemLoader(ROOT))
    print('Warning: could not import Flask app, using standalone Jinja environment:', e)

errors = []
for root, dirs, files in os.walk(ROOT):
    for f in files:
        if not f.endswith('.html'):
            continue
        full = os.path.join(root, f)
        name = os.path.relpath(full, ROOT).replace('\\', '/')
        try:
            env.get_template(name)
        except TemplateSyntaxError as e:
            errors.append((name, e.lineno, str(e)))
        except Exception as e:
            errors.append((name, None, f"{type(e).__name__}: {e}"))

if not errors:
    print('All templates compiled fine.')
else:
    print('Template syntax errors found:')
    for name, lineno, msg in errors:
        if lineno:
            print(f"{name} (line {lineno}): {msg}")
        else:
            print(f"{name}: {msg}")
