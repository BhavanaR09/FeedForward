from database import get_db_connection
from werkzeug.security import check_password_hash

conn = get_db_connection()

admin = conn.execute("""
    SELECT id, username, email, password_hash, role, status
    FROM users
    WHERE LOWER(username) = LOWER(?)
""", ("admin",)).fetchone()

if not admin:
    print("❌ ADMIN NOT FOUND")
else:
    print("ID:", admin["id"])
    print("Username:", admin["username"])
    print("Email:", admin["email"])
    print("Role:", admin["role"])
    print("Status:", admin["status"])
    print("Hash:", admin["password_hash"][:30] + "...")

    print(
        "Password admin@123:",
        check_password_hash(admin["password_hash"], "admin@123")
    )

conn.close()