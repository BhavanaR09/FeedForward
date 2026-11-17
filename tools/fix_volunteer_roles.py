import sqlite3
from config import Config

conn = sqlite3.connect(Config.DATABASE)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('Scanning volunteers for missing user_roles...')
updated = 0
for row in cur.execute('SELECT id, user_id, name FROM volunteers'):
    user_id = row['user_id']
    user = cur.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        print(f"Volunteer record id={row['id']} references missing user_id={user_id}")
        continue
    role_row = cur.execute('SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?', (user_id, 'volunteer')).fetchone()
    if not role_row:
        cur.execute('INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)', (user_id, 'volunteer', 'active'))
        conn.commit()
        print(f"Added volunteer role for user {user['username']} (user_id={user_id})")
        updated += 1
    else:
        print(f"User {user['username']} (user_id={user_id}) already has volunteer role")

print('\nDone. Roles added:', updated)
conn.close()
