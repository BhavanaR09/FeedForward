from database import get_db_connection
from werkzeug.security import generate_password_hash
import datetime

conn = get_db_connection()
cursor = conn.cursor()

username = "admin"
password = "admin@123"
email = "admin@feedforward.local"

password_hash = generate_password_hash(password)

# Check whether admin already exists
admin = cursor.execute(
    "SELECT id, username FROM users WHERE role = ?",
    ("admin",)
).fetchone()

if admin:

    # RESET EXISTING ADMIN
    cursor.execute(
        """
        UPDATE users
        SET password_hash = ?,
            status = ?
        WHERE id = ?
        """,
        (password_hash, "active", admin[0])
    )

    conn.commit()

    print("====================================")
    print("Admin password reset successfully!")
    print("Username:", admin[1])
    print("Password:", password)
    print("Status: active")
    print("====================================")

else:

    # CREATE NEW ADMIN
    cursor.execute(
        """
        INSERT INTO users
        (username, email, password_hash, role, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            email,
            password_hash,
            "admin",
            "active",
            datetime.datetime.now().isoformat()
        )
    )

    conn.commit()

    print("====================================")
    print("Admin account created successfully!")
    print("Username:", username)
    print("Password:", password)
    print("Status: active")
    print("====================================")

conn.close()