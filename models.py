import os
import re
import smtplib
import ssl
import sqlite3
import threading
import time
import datetime
from email.message import EmailMessage

from flask import session
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
from database import get_db_connection

IMPORTANT_NOTIFICATION_KEYWORDS = (
    'accepted', 'approved', 'rejected', 'assigned', 'pickup', 'picked up', 'delivered',
    'completed', 'expired', 'approaching expiry', 'payment successful', 'payment failed',
    'confirmation', 'verified', 'admin', 'inspection', 'receipt', 'donation'
)


def should_send_email_for_notification(message):
    if not message or not isinstance(message, str):
        return False
    lower_message = message.lower()
    return any(keyword in lower_message for keyword in IMPORTANT_NOTIFICATION_KEYWORDS)


def _notification_title_from_message(message):
    text = (message or '').strip()
    if not text:
        return 'FeedForward Notification'
    low = text.lower()
    if 'accepted' in low:
        return 'Donation Accepted'
    if 'assigned' in low:
        return 'Pickup Assignment'
    if 'picked up' in low or 'pickup' in low:
        return 'Pickup Update'
    if 'delivered' in low:
        return 'Delivery Update'
    if 'completed' in low:
        return 'Donation Completed'
    if 'rejected' in low:
        return 'Donation Rejected'
    if 'expired' in low:
        return 'Donation Expired'
    if 'approved' in low:
        return 'Inspection Approved'
    if 'payment failed' in low:
        return 'Payment Failed'
    if 'payment successful' in low:
        return 'Payment Successful'
    if 'admin' in low:
        return 'Admin Notification'
    if 'inspection' in low:
        return 'Inspection Update'
    return 'FeedForward Notification'


def _get_notification_email_log_table():
    conn = get_db_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS notification_email_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, event_key TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, event_key), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)")
    conn.commit()
    conn.close()


def _record_notification_email_result(user_id, event_key, subject, status, error=None):
    _get_notification_email_log_table()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO notification_email_log (user_id, event_key, subject, status, error) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, event_key) DO UPDATE SET subject = excluded.subject, status = excluded.status, error = excluded.error, created_at = CURRENT_TIMESTAMP",
        (user_id, event_key, subject, status, error)
    )
    conn.commit()
    conn.close()


def _notification_email_already_sent(user_id, event_key):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM notification_email_log WHERE user_id = ? AND event_key = ? AND status = 'sent'",
        (user_id, event_key)
    ).fetchone()
    conn.close()
    return bool(row)


def send_notification_email(user_email, subject, message):
    if not user_email:
        return False

    recipient = (user_email or '').strip()
    if not recipient or '@' not in recipient:
        return False

    mail_server = (Config.MAIL_SERVER or 'smtp.gmail.com').strip()
    mail_port = int(Config.MAIL_PORT or 587)
    mail_user = (Config.MAIL_USERNAME or '').strip()
    mail_pass = (Config.MAIL_PASSWORD or '').strip()
    sender = (Config.MAIL_DEFAULT_SENDER or mail_user or 'noreply@feedforward.local').strip()
    use_tls = bool(Config.MAIL_USE_TLS)
    use_ssl = bool(Config.MAIL_USE_SSL)

    if not mail_user or not mail_pass:
        print(f"[Notification Email] SMTP credentials are not configured for {recipient}. Email not sent.")
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = recipient
    msg.set_content(message)

    try:
        context = ssl.create_default_context()
        if use_ssl or mail_port == 465:
            with smtplib.SMTP_SSL(mail_server, mail_port, context=context, timeout=15) as server:
                server.login(mail_user, mail_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(mail_user, mail_pass)
                server.send_message(msg)
        return True
    except Exception as exc:
        print(f"[Notification Email] Failed to send email to {recipient}: {exc}")
        return False


def _send_notification_email_background(user_email, subject, body):
    try:
        send_notification_email(user_email, subject, body)
    except Exception as exc:
        print(f"[Notification Email] Background delivery failed: {exc}")


def build_notification_email_content(user_name, title, message, donation_id=None, donation_type=None, status=None, page_url=None):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        'Hello ' + (user_name or 'User') + ',',
        '',
        f'{title}',
        '',
        message,
    ]

    if donation_id:
        lines.append('')
        lines.append(f'Donation ID: {donation_id}')
    if donation_type:
        lines.append(f'Donation Type: {donation_type}')
    if status:
        lines.append(f'Status: {status}')
    lines.extend([
        f'Date/Time: {timestamp}',
        '',
        'Please log in to FeedForward to view the complete details.',
        '',
        'Regards,',
        'FeedForward Team'
    ])

    if page_url:
        lines.append('')
        lines.append(f'Open FeedForward: {page_url}')

    return '\n'.join(lines)


class User(UserMixin):
    def __init__(self, id, username, email, password_hash, role, status, full_name=None, phone=None, address=None, profile_image_path=None, last_login=None, created_at=None, roles=None, active_role=None):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self._role = role
        self.status = status
        self.full_name = full_name
        self.phone = phone
        self.address = address
        self.profile_image_path = profile_image_path
        self.last_login = last_login
        self.created_at = created_at
        self.roles = roles or ([role] if role else [])
        self.active_role = active_role if active_role is not None else (self.roles[0] if self.roles else self._role)

    @property
    def role(self):
        return self.active_role if self.active_role is not None else (self._role if self._role else (self.roles[0] if self.roles else None))

    @role.setter
    def role(self, value):
        self.active_role = value

    def has_role(self, role):
        target = (role or '').strip().lower()
        if not target:
            return False
        if self.active_role and self.active_role.lower() == target:
            return True
        if self._role and self._role.lower() == target:
            return True
        return any(r and r.lower() == target for r in self.roles)

    @staticmethod
    def _get_roles_for_user(user_id, fallback_role=None):
        conn = get_db_connection()
        rows = conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,)).fetchall()
        conn.close()
        roles = [row['role'] for row in rows]
        if not roles and fallback_role:
            roles = [fallback_role]
        return roles

    @staticmethod
    def _build_from_row(user_row, active_role=None):
        roles = User._get_roles_for_user(user_row['id'], fallback_role=user_row['role'])
        if active_role and active_role not in roles:
            active_role = None
        if active_role is None:
            active_role = user_row['role'] if user_row['role'] else (roles[0] if roles else None)
        
        # Convert string dates to datetime objects if needed
        def _to_datetime(value):
            if not value:
                return None
            if isinstance(value, datetime.datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.datetime.fromisoformat(value)
                except (ValueError, TypeError):
                    try:
                        return datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
                    except (ValueError, TypeError):
                        return None
            return None
        
        return User(
            id=user_row['id'],
            username=user_row['username'],
            email=user_row['email'],
            password_hash=user_row['password_hash'],
            role=user_row['role'],
            status=user_row['status'],
            full_name=user_row['full_name'] if 'full_name' in user_row.keys() else None,
            phone=user_row['phone'] if 'phone' in user_row.keys() else None,
            address=user_row['address'] if 'address' in user_row.keys() else None,
            profile_image_path=user_row['profile_image_path'] if 'profile_image_path' in user_row.keys() else None,
            last_login=_to_datetime(user_row['last_login']) if 'last_login' in user_row.keys() else None,
            created_at=_to_datetime(user_row['created_at']),
            roles=roles,
            active_role=active_role
        )

    @staticmethod
    def get(user_id):
        conn = get_db_connection()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if user_row:
            active_role = session.get('active_role') if session is not None else None
            return User._build_from_row(user_row, active_role=active_role)
        return None

    @staticmethod
    def find_by_username(username):
        normalized = (username or '').strip() if username else username
        if not normalized:
            return None
        conn = get_db_connection()
        user_row = conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (normalized,)).fetchone()
        conn.close()
        if user_row:
            return User._build_from_row(user_row)
        return None

    @staticmethod
    def find_by_email(email):
        normalized = email.strip().lower() if email else email
        if not normalized:
            return None
        conn = get_db_connection()
        user_row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?", (normalized,)).fetchone()
        conn.close()
        if user_row:
            return User._build_from_row(user_row)
        return None

    @staticmethod
    def find_by_username_and_role(username, role):
        normalized_username = (username or '').strip() if username else username
        normalized_role = (role or '').strip().lower()
        if not normalized_username or not normalized_role:
            return None
        conn = get_db_connection()
        user_row = conn.execute(
            "SELECT u.* FROM users u WHERE LOWER(u.username) = LOWER(?) AND (u.role = ? OR EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = u.id AND LOWER(ur.role) = ?))",
            (normalized_username, normalized_role, normalized_role)
        ).fetchone()
        conn.close()
        if user_row:
            return User._build_from_row(user_row)
        return None

    @staticmethod
    def find_by_email_and_role(email, role):
        normalized_email = email.strip().lower() if email else email
        normalized_role = (role or '').strip().lower()
        if not normalized_email or not normalized_role:
            return None
        conn = get_db_connection()
        user_row = conn.execute(
            "SELECT u.* FROM users u WHERE LOWER(u.email) = ? AND (u.role = ? OR EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = u.id AND LOWER(ur.role) = ?))",
            (normalized_email, normalized_role, normalized_role)
        ).fetchone()
        conn.close()
        if user_row:
            return User._build_from_row(user_row)
        return None

    @staticmethod
    def _username_exists(username):
        if not username:
            return False
        conn = get_db_connection()
        row = conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
        conn.close()
        return bool(row)

    @staticmethod
    def _ensure_unique_username(username, role=None, full_name=None, phone=None):
        base_username = (username or '').strip()
        if not base_username:
            # Generate from full_name in lowercase, but keep the format
            base_username = re.sub(r'[^a-z0-9]+', '', (full_name or 'user').strip().lower()) or 'user'
            if role:
                base_username = f"{base_username}_{role}"
        else:
            # Keep the original case the user provided, but clean special characters
            base_username = re.sub(r'[^a-zA-Z0-9]+', '', base_username) or 'user'
        
        # First, try the base username as-is (no suffix)
        if not User._username_exists(base_username):
            return base_username
        
        # If it exists, append phone suffix or default suffix
        if phone and phone.isdigit() and len(phone) >= 4:
            suffix = phone[-4:]
        else:
            suffix = '0000'
        candidate = f"{base_username}_{suffix}"
        counter = 1
        while User._username_exists(candidate):
            candidate = f"{base_username}_{suffix}_{counter}"
            counter += 1
        return candidate

    @staticmethod
    def add_role(user_id, role, status='active'):
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                (user_id, role, status)
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def create(username, email, password, role, status='active', full_name=None, phone=None):
        normalized_email = email.strip().lower() if email else email
        hashed_password = generate_password_hash(password)
        last_error = None
        for attempt in range(5):
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                existing = cursor.execute("SELECT id FROM users WHERE LOWER(email) = ?", (normalized_email,)).fetchone()
                if existing:
                    role_exists = cursor.execute(
                        "SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?",
                        (existing['id'], role)
                    ).fetchone()
                    if role_exists:
                        return existing['id']
                    cursor.execute(
                        "INSERT INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                        (existing['id'], role, status)
                    )
                    conn.commit()
                    return existing['id']
                final_username = User._ensure_unique_username(username, role=role, full_name=full_name, phone=phone)
                cursor.execute(
                    "INSERT INTO users (username, email, password_hash, role, status, full_name, phone) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (final_username, normalized_email, hashed_password, role, status, full_name, phone)
                )
                user_id = cursor.lastrowid
                cursor.execute(
                    "INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                    (user_id, role, status)
                )
                conn.commit()
                return user_id
            except sqlite3.IntegrityError as e:
                conn.rollback()
                last_error = e
                if 'unique constraint' not in str(e).lower():
                    raise
                time.sleep(0.2)
            except sqlite3.OperationalError as e:
                conn.rollback()
                last_error = e
                if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                    raise
                time.sleep(0.2)
            except Exception as e:
                conn.rollback()
                last_error = e
                raise
            finally:
                conn.close()
        print(f"Error creating user after retries: {last_error}")
        if last_error is not None:
            raise last_error
        return None

    @staticmethod
    def create_unified(full_name, email, password, role, phone, status='active'):
        normalized_email = email.strip().lower() if email else email
        hashed_password = generate_password_hash(password)
        last_error = None
        for attempt in range(5):
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                existing = cursor.execute("SELECT id FROM users WHERE LOWER(email) = ?", (normalized_email,)).fetchone()
                if existing:
                    role_exists = cursor.execute(
                        "SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?",
                        (existing['id'], role)
                    ).fetchone()
                    if role_exists:
                        return existing['id']
                    cursor.execute(
                        "INSERT INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                        (existing['id'], role, status)
                    )
                    conn.commit()
                    return existing['id']
                username = User._ensure_unique_username(full_name, role=role, full_name=full_name, phone=phone)
                cursor.execute(
                    "INSERT INTO users (username, full_name, email, phone, password_hash, role, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (username, full_name, normalized_email, phone, hashed_password, role, status)
                )
                user_id = cursor.lastrowid
                cursor.execute(
                    "INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                    (user_id, role, status)
                )
                conn.commit()
                return user_id
            except sqlite3.IntegrityError as e:
                conn.rollback()
                last_error = e
                if 'unique constraint' not in str(e).lower():
                    raise
                time.sleep(0.2)
            except sqlite3.OperationalError as e:
                conn.rollback()
                last_error = e
                if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                    raise
                time.sleep(0.2)
            except Exception as e:
                conn.rollback()
                last_error = e
                raise
            finally:
                conn.close()
        print(f"Error creating unified user after retries: {last_error}")
        if last_error is not None:
            raise last_error
        return None

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def get_profile_data(self):
        """Fetch role-specific profile data."""
        conn = get_db_connection()
        data = None
        if self.role == 'admin':
            data = conn.execute("SELECT * FROM admins WHERE user_id = ?", (self.id,)).fetchone()
        elif self.role == 'donor':
            data = conn.execute("SELECT * FROM donors WHERE user_id = ?", (self.id,)).fetchone()
        elif self.role == 'ngo':
            data = conn.execute("SELECT * FROM ngos WHERE user_id = ?", (self.id,)).fetchone()
        elif self.role == 'volunteer':
            data = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (self.id,)).fetchone()
        conn.close()
        return dict(data) if data is not None else None


class Admin:
    @staticmethod
    def create(user_id, name, phone):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO admins (user_id, name, phone) VALUES (?, ?, ?)",
            (user_id, name, phone)
        )
        conn.commit()
        conn.close()


class Donor:
    @staticmethod
    def create(user_id, company_name, contact_name, phone, address, latitude=None, longitude=None, organization_type=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO donors (user_id, company_name, contact_name, phone, address, latitude, longitude, organization_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, company_name, contact_name, phone, address, latitude, longitude, organization_type)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_by_user_id(user_id):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM donors WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return row


class NGO:
    @staticmethod
    def create(user_id, ngo_name, registration_number, contact_person, phone, address, latitude=None, longitude=None, is_verified=0, organization_type=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ngos (user_id, ngo_name, registration_number, contact_person, phone, address, latitude, longitude, organization_type, is_verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, ngo_name, registration_number, contact_person, phone, address, latitude, longitude, organization_type, is_verified)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_by_user_id(user_id):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM ngos WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return row


class Volunteer:
    @staticmethod
    def create(user_id, name, phone, address, latitude=None, longitude=None, is_approved=0, transport_type=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO volunteers (user_id, name, phone, address, latitude, longitude, is_approved, transport_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, phone, address, latitude, longitude, is_approved, transport_type)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_by_user_id(user_id):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return row


class FoodDonation:
    @staticmethod
    def create(donor_id, food_name, category, veg_non_veg, quantity, expiry_time, pickup_time, address, latitude=None, longitude=None, image_path=None, qr_code_path=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO food_donations (donor_id, food_name, category, veg_non_veg, quantity, expiry_time, pickup_time, address, latitude, longitude, image_path, qr_code_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (donor_id, food_name, category, veg_non_veg, quantity, expiry_time, pickup_time, address, latitude, longitude, image_path, qr_code_path)
        )
        donation_id = cursor.lastrowid
        
        # Create corresponding pending entry in food_distribution.
        # Use INSERT OR IGNORE to avoid UNIQUE constraint errors when a row already exists.
        cursor.execute(
            "INSERT OR IGNORE INTO food_distribution (donation_id, status) VALUES (?, 'Pending')",
            (donation_id,)
        )
        if cursor.rowcount == 0:
            cursor.execute(
                "UPDATE food_distribution SET status = 'Pending' WHERE donation_id = ?",
                (donation_id,)
            )
        record_donation_activity(conn, 'food', donation_id, 'Donation Added', 'Pending')
        
        conn.commit()
        conn.close()
        return donation_id


def record_donation_activity(conn, donation_type, donation_id, event, status=None, actor_user_id=None, actor_role=None):
    """Persist one idempotent lifecycle event for any supported donation type."""
    conn.execute(
        """INSERT OR IGNORE INTO donation_activity
           (donation_type, donation_id, event, status, actor_user_id, actor_role)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (donation_type, donation_id, event, status, actor_user_id, actor_role)
    )


class Orphanage:
    @staticmethod
    def create(name, contact_person=None, phone=None, email=None, address=None, location=None, created_by=None, is_approved=0):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orphanages (name, contact_person, phone, email, address, location, is_approved, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, contact_person, phone, email, address, location, is_approved, created_by)
        )
        orphanage_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return orphanage_id

    @staticmethod
    def get_by_name(name):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM orphanages WHERE LOWER(name) = LOWER(?) LIMIT 1", (name,)).fetchone()
        conn.close()
        return row


class ClothesDonation:
    @staticmethod
    def create(donor_id, item_type, category, size, quantity, condition, pickup_time, address, latitude=None, longitude=None, image_path=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO clothes_donations (donor_id, item_type, category, size, quantity, condition, pickup_time, address, latitude, longitude, image_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (donor_id, item_type, category, size, quantity, condition, pickup_time, address, latitude, longitude, image_path)
        )
        donation_id = cursor.lastrowid
        record_donation_activity(conn, 'clothes', donation_id, 'Donation Added', 'Pending')
        conn.commit()
        conn.close()
        return donation_id


class BookDonation:
    @staticmethod
    def create(donor_id, title, category, quantity, condition, pickup_time, address, author=None, latitude=None, longitude=None, image_path=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO book_donations (donor_id, title, author, category, quantity, condition, pickup_time, address, latitude, longitude, image_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (donor_id, title, author, category, quantity, condition, pickup_time, address, latitude, longitude, image_path)
        )
        donation_id = cursor.lastrowid
        record_donation_activity(conn, 'books', donation_id, 'Donation Added', 'Pending')
        conn.commit()
        conn.close()
        return donation_id


class MoneyDonation:
    @staticmethod
    def create(donor_id, amount, purpose, payment_method, note=None, orphanage_id=None, razorpay_order_id=None):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO money_donations (donor_id, orphanage_id, amount, currency, purpose, payment_method, note, payment_status, donation_status, razorpay_order_id) VALUES (?, ?, ?, 'INR', ?, ?, ?, 'Pending', 'Pending', ?)",
            (donor_id, orphanage_id, amount, purpose, payment_method, note, razorpay_order_id)
        )
        donation_id = cursor.lastrowid
        record_donation_activity(conn, 'money', donation_id, 'Donation Created', 'Pending')
        conn.commit()
        conn.close()
        return donation_id


class Emergency:
    @staticmethod
    def create(title, disaster_type, location, severity, details, start_date, end_date, is_active, created_by):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO emergencies (title, disaster_type, location, severity, details, start_date, end_date, is_active, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, disaster_type, location, severity, details, start_date, end_date, is_active, created_by)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT e.*, u.username as created_by_username FROM emergencies e "
            "LEFT JOIN users u ON e.created_by = u.id "
            "ORDER BY e.is_active DESC, e.created_at DESC"
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_active():
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT e.*, u.username as created_by_username FROM emergencies e "
            "LEFT JOIN users u ON e.created_by = u.id "
            "WHERE e.is_active = 1 ORDER BY e.created_at DESC"
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_by_id(emergency_id):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM emergencies WHERE id = ?", (emergency_id,)).fetchone()
        conn.close()
        return row

    @staticmethod
    def toggle_active(emergency_id):
        conn = get_db_connection()
        emergency = conn.execute("SELECT is_active FROM emergencies WHERE id = ?", (emergency_id,)).fetchone()
        if emergency:
            new_state = 0 if emergency['is_active'] else 1
            conn.execute("UPDATE emergencies SET is_active = ? WHERE id = ?", (new_state, emergency_id))
            conn.commit()
        conn.close()

    @staticmethod
    def delete(emergency_id):
        conn = get_db_connection()
        conn.execute("DELETE FROM emergencies WHERE id = ?", (emergency_id,))
        conn.commit()
        conn.close()


class EmergencyModeRequest:
    @staticmethod
    def create(user_id, note):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO emergency_mode_requests (user_id, note) VALUES (?, ?)",
            (user_id, note)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_pending_by_user(user_id):
        conn = get_db_connection()
        row = conn.execute(
            "SELECT * FROM emergency_mode_requests WHERE user_id = ? AND status = 'Pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def get_pending():
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT r.*, u.username as requester_username FROM emergency_mode_requests r "
            "LEFT JOIN users u ON r.user_id = u.id "
            "WHERE r.status = 'Pending' ORDER BY r.created_at DESC"
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_by_id(request_id):
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM emergency_mode_requests WHERE id = ?", (request_id,)).fetchone()
        conn.close()
        return row

    @staticmethod
    def update_status(request_id, status, reviewed_by):
        conn = get_db_connection()
        conn.execute(
            "UPDATE emergency_mode_requests SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, reviewed_by, request_id)
        )
        conn.commit()
        conn.close()


class MessageReply:
    @staticmethod
    def create(contact_message_id, admin_user_id, recipient_email, subject, reply_message, status, error=None):
        conn = get_db_connection()
        try:
            cursor = conn.execute(
                """INSERT INTO message_replies
                   (contact_message_id, admin_user_id, recipient_email, subject, reply_message, status, sent_at, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact_message_id,
                    admin_user_id,
                    recipient_email,
                    subject,
                    reply_message,
                    status,
                    datetime.datetime.utcnow().isoformat() if status == 'sent' else None,
                    error
                )
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


class Notification:
    @staticmethod
    def create(user_id, message, *, title=None, donation_id=None, donation_type=None, status=None, event_key=None, send_email=True):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notifications (user_id, message) VALUES (?, ?)",
            (user_id, message)
        )
        notification_id = cursor.lastrowid
        conn.commit()
        conn.close()

        if not send_email:
            return notification_id

        try:
            email_conn = get_db_connection()
            user_row = email_conn.execute(
                "SELECT username, email FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            email_conn.close()
            if not user_row or not user_row['email']:
                return notification_id

            notification_title = title or _notification_title_from_message(message)
            event_name = event_key or f"notification:{notification_id}"
            subject = f"FeedForward - {notification_title}"
            app_url = os.environ.get('APP_BASE_URL', 'http://localhost:5000')
            body = build_notification_email_content(
                user_name=user_row['username'],
                title=notification_title,
                message=message,
                donation_id=donation_id,
                donation_type=donation_type,
                status=status,
                page_url=app_url
            )

            if _notification_email_already_sent(user_id, event_name):
                return notification_id

            email_sent = send_notification_email(user_row['email'], subject, body)
            _record_notification_email_result(
                user_id,
                event_name,
                subject,
                'sent' if email_sent else 'failed',
                None if email_sent else 'SMTP delivery failed'
            )
        except Exception as exc:
            print(f"[Notification Email] Notification delivery failed: {exc}")

        return notification_id

    @staticmethod
    def get_unread(user_id):
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC", (user_id,)).fetchall()
        conn.close()
        return rows

    @staticmethod
    def mark_all_read(user_id):
        conn = get_db_connection()
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
