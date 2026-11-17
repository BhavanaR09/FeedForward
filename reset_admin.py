from database import get_db_connection
from werkzeug.security import generate_password_hash, check_password_hash

USERNAME = "admin"
NEW_PASSWORD = "admin@123"

print("Starting admin password reset...")

conn = get_db_connection()

admin = conn.execute("""
    SELECT id, username, role, status
    FROM users
    WHERE LOWER(username) = LOWER(?)
      AND LOWER(role) = 'admin'
""", (USERNAME,)).fetchone()

if not admin:
    print("❌ Admin account was not found.")
    conn.close()
    exit()

print("Admin found:")
print("ID:", admin["id"])
print("Username:", admin["username"])
print("Role:", admin["role"])
print("Status:", admin["status"])

# Generate a completely new password hash
new_hash = generate_password_hash(
    NEW_PASSWORD,
    method="scrypt"
)

print("New hash generated:")
print(new_hash[:40] + "...")

# Update the EXISTING admin account
conn.execute("""
    UPDATE users
    SET password_hash = ?,
        status = 'active'
    WHERE id = ?
""", (new_hash, admin["id"]))

conn.commit()

# Read it back from the database
saved = conn.execute("""
    SELECT password_hash
    FROM users
    WHERE id = ?
""", (admin["id"],)).fetchone()

conn.close()

if not saved:
    print("❌ Could not read the updated admin account.")
    exit()

# Verify the EXACT hash saved in SQLite
password_is_correct = check_password_hash(
    saved["password_hash"],
    NEW_PASSWORD
)

print()
print("================================")
print("PASSWORD RESET RESULT")
print("================================")

if password_is_correct:
    print("✅ SUCCESS!")
    print("Username: admin")
    print("Password: admin@123")
    print("Password verification: TRUE")
else:
    print("❌ FAILED!")
    print("Password verification: FALSE")