import sqlite3
from config import Config

conn = sqlite3.connect(Config.DATABASE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('Users table entries (volunteer roles):')
for row in cur.execute("SELECT id, username, email, role, status FROM users WHERE role = 'volunteer'"):
    print(dict(row))

print('\nVolunteers table:')
for row in cur.execute('SELECT id, user_id, name, phone, is_approved FROM volunteers'):
    print(dict(row))

conn.close()
