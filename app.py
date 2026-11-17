import os
import datetime
import hashlib
import hmac
import random
import re
import smtplib
import ssl
import string
import time
import requests
import secrets
from email.message import EmailMessage
from email_validator import EmailNotValidError, validate_email
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, send_file, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from database import get_db_connection, init_db, ensure_food_distribution_columns
from models import User, Donor, NGO, Volunteer, FoodDonation, Orphanage, ClothesDonation, BookDonation, MoneyDonation, Emergency, EmergencyModeRequest, MessageReply, Notification, record_donation_activity, send_notification_email
from forms import LoginForm, AdminLoginForm, DonorRegisterForm, NGORegisterForm, VolunteerRegisterForm, ProfileForm, DonationForm, ClothesDonationForm, BookDonationForm, MoneyDonationForm, FeedbackForm, PlatformFeedbackForm, ContactForm, EmergencyForm, EmergencyModeRequestForm, SystemSettingsForm
from utils import generate_qr_code, export_donations_to_excel, export_donations_to_pdf, generate_delivery_certificate, export_user_portfolio_to_pdf
from search import SearchHelper
from chatbot_service import generate_chatbot_reply
import razorpay

app = Flask(__name__)
app.config.from_object(Config)
app.config.setdefault('EXPIRY_CHECK_INTERVAL_SECONDS', 60)
app._LAST_EXPIRY_CHECK = 0.0

# Ensure the database schema exists and any missing columns are added on startup.
init_db()

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Jinja helper to format timestamps consistently across donation views
@app.template_filter('format_datetime')
def format_datetime(value, fmt="%d %b %Y, %I:%M %p"):
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            try:
                value = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return value
    if isinstance(value, datetime.datetime):
        return value.strftime(fmt)
    return str(value)

# Temporary OTP cache for password reset flows. Keyed by method:identifier.
otp_store = {}


def _parse_best_before_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    for candidate in (text, text.replace('T', ' ')):
        try:
            return datetime.datetime.fromisoformat(candidate)
        except ValueError:
            pass

    formats = [
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
        '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%Y',
        '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y',
        '%m/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M', '%m/%d/%Y',
        '%d %b %Y %I:%M %p', '%d %b %Y %H:%M', '%d %B %Y %I:%M %p', '%d %B %Y %H:%M',
        '%b %d %Y %I:%M %p', '%B %d %Y %I:%M %p', '%b %d %Y', '%B %d %Y'
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass

    lower = text.lower()
    now = datetime.datetime.now()
    base_date = now
    if 'tomorrow' in lower:
        base_date = now + datetime.timedelta(days=1)
    elif 'today' in lower:
        base_date = now

    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', lower)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)
        if ampm and ampm.lower() == 'pm' and hour < 12:
            hour += 12
        if ampm and ampm.lower() == 'am' and hour == 12:
            hour = 0
        try:
            return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None

    return None


def _auto_expire_donations(conn=None, force=True):
    if conn is None:
        conn = get_db_connection()
        close_after = True
    else:
        close_after = False

    try:
        if not force:
            interval_seconds = int(app.config.get('EXPIRY_CHECK_INTERVAL_SECONDS', 60))
            last_check = float(getattr(app, '_LAST_EXPIRY_CHECK', 0.0))
            if last_check and (time.monotonic() - last_check) < interval_seconds:
                return []

        app._LAST_EXPIRY_CHECK = time.monotonic()
        now = datetime.datetime.now()
        rows = conn.execute(
            "SELECT id, food_name, donor_id, status, expiry_time FROM food_donations WHERE status NOT IN ('Expired', 'Delivered', 'Completed', 'Cancelled') ORDER BY expiry_time ASC LIMIT 500"
        ).fetchall()
        expired_ids = []

        for row in rows:
            expiry_dt = _parse_best_before_datetime(row['expiry_time'])
            if not expiry_dt:
                continue
            if now < expiry_dt:
                continue

            expired_ids.append(row['id'])
            conn.execute(
                "UPDATE food_donations SET status = 'Expired' WHERE id = ? AND status NOT IN ('Expired', 'Delivered', 'Completed', 'Cancelled')",
                (row['id'],)
            )
            conn.execute(
                "UPDATE food_distribution SET status = 'Expired' WHERE donation_id = ? AND status NOT IN ('Completed', 'Cancelled', 'Delivered', 'Expired')",
                (row['id'],)
            )

            donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (row['donor_id'],)).fetchone()
            if donor_user:
                Notification.create(
                    donor_user['user_id'],
                    f"Your food donation {row['food_name']} has expired because the Best Before time has passed.",
                    title='Donation Expired',
                    donation_id=row['id'],
                    donation_type='Food',
                    status='Expired',
                    event_key=f"expiry:donor:{row['id']}",
                    send_email=True
                )

            dist = conn.execute("SELECT ngo_id, volunteer_id FROM food_distribution WHERE donation_id = ?", (row['id'],)).fetchone()
            if dist:
                if dist['ngo_id']:
                    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (dist['ngo_id'],)).fetchone()
                    if ngo_user:
                        Notification.create(
                            ngo_user['user_id'],
                            f"Food donation {row['food_name']} has expired and can no longer be accepted.",
                            title='Donation Expired',
                            donation_id=row['id'],
                            donation_type='Food',
                            status='Expired',
                            event_key=f"expiry:ngo:{row['id']}",
                            send_email=True
                        )
                if dist['volunteer_id']:
                    volunteer_user = conn.execute("SELECT user_id FROM volunteers WHERE id = ?", (dist['volunteer_id'],)).fetchone()
                    if volunteer_user:
                        Notification.create(
                            volunteer_user['user_id'],
                            f"Food donation {row['food_name']} has expired. Pickup and delivery are no longer allowed.",
                            title='Donation Expired',
                            donation_id=row['id'],
                            donation_type='Food',
                            status='Expired',
                            event_key=f"expiry:volunteer:{row['id']}",
                            send_email=True
                        )

        if expired_ids:
            conn.commit()
            return expired_ids
        return []
    finally:
        if close_after:
            conn.close()


@app.before_request
def auto_expire_before_request():
    if request.method != 'GET' or request.path.startswith('/static') or request.path.startswith('/api/'):
        return

    try:
        _auto_expire_donations(force=False)
    except Exception as exc:
        print(f"[Expiry] Failed to expire stale donations: {exc}")


def normalize_phone(phone):
    if not phone:
        return phone
    return phone.strip().replace(' ', '')


def normalize_email(email):
    return (email or '').strip().lower()


def normalize_otp_code(code):
    return (code or '').strip()


def canonical_identifier(method, identifier):
    if method == 'email':
        return identifier.strip().lower()
    if method == 'phone':
        return normalize_phone(identifier)
    return identifier


def generate_otp_code(length=6):
    return ''.join(random.choices(string.digits, k=length))


def send_sms_otp(phone, otp):
    # Replace this stub with real SMS provider integration if available.
    print(f"[OTP SMS] Sending OTP {otp} to phone {phone}")


def otp_key(method, identifier):
    return f"{method}:{canonical_identifier(method, identifier)}"


def get_app_setting(key, default=None):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_app_setting(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    if conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (key,)).fetchone():
        cursor.execute("UPDATE app_settings SET value = ? WHERE key = ?", (value, key))
    else:
        cursor.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _otp_session_key(key):
    return f"otp_session:{key}"


def update_last_login(user_id):
    conn = get_db_connection()
    conn.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def store_otp(method, identifier, otp, expires_minutes=5):
    key = otp_key(method, identifier)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=expires_minutes)
    entry = {
        'code': str(otp),
        'expires_at': expires_at
    }
    otp_store[key] = entry
    session[_otp_session_key(key)] = {
        'code': str(otp),
        'expires_at': expires_at.isoformat()
    }
    session.modified = True


def verify_otp(method, identifier, code):
    key = otp_key(method, identifier)
    entry = otp_store.get(key)
    if not entry:
        saved_entry = session.get(_otp_session_key(key))
        if saved_entry:
            expires_at = saved_entry.get('expires_at')
            if isinstance(expires_at, str):
                expires_at = datetime.datetime.fromisoformat(expires_at)
            entry = {
                'code': saved_entry.get('code'),
                'expires_at': expires_at
            }
    if not entry:
        return False

    expires_at = entry.get('expires_at')
    if isinstance(expires_at, str):
        expires_at = datetime.datetime.fromisoformat(expires_at)

    if expires_at < datetime.datetime.utcnow():
        otp_store.pop(key, None)
        session.pop(_otp_session_key(key), None)
        return False
    if str(entry.get('code')) != normalize_otp_code(code):
        return False

    otp_store.pop(key, None)
    session.pop(_otp_session_key(key), None)
    return True


def send_email_code(email, otp, username=None):
    subject = 'FeedForward Registration OTP'
    recipient = email
    sender = app.config.get('MAIL_DEFAULT_SENDER') or app.config.get('MAIL_USERNAME') or recipient
    body = f"Hello {username or 'User'},\n\n" \
           f"Your FeedForward registration verification code is: {otp}\n\n" \
           f"This code will expire in 5 minutes.\n\n" \
           "If you did not request this code, please ignore this email.\n\n" \
           "Thank you,\nFeedForward Team"

    # Build message
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = recipient
    message.set_content(body)

    mail_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port = int(app.config.get('MAIL_PORT', 587))
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')
    use_tls = app.config.get('MAIL_USE_TLS', True)
    use_ssl = app.config.get('MAIL_USE_SSL', False)

    print(f"[OTP Email] SMTP config: server={mail_server}, port={mail_port}, user_set={bool(mail_user)}, tls={use_tls}, ssl={use_ssl}, sender={sender}")

    # If no SMTP credentials provided but localhost is configured, try unauthenticated send
    if not mail_user or not mail_pass:
        if mail_server in ('localhost', '127.0.0.1') or mail_port in (25,):
            try:
                with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                    server.send_message(message)
                print(f"[OTP Email] Sent (no-auth) OTP to {recipient}")
                return True
            except Exception as e:
                print(f"[OTP Email] Failed unauthenticated send to {recipient}: {e}")
                return False
        # fallback to logging the OTP for local development
        print(f"[OTP Email] SMTP credentials missing for {recipient}")
        return False

    # Perform authenticated send
    try:
        if use_ssl or mail_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(mail_server, mail_port, context=context, timeout=15) as server:
                server.ehlo()
                server.login(mail_user, mail_pass)
                server.send_message(message)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                server.ehlo()
                if use_tls and mail_port in (587, 25, None):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(mail_user, mail_pass)
                server.send_message(message)
        print(f"[OTP Email] Sent OTP to {recipient}")
        return True
    except smtplib.SMTPAuthenticationError as auth_err:
        print(f"[OTP Email] SMTP authentication failed for {recipient}: {auth_err.smtp_code} {auth_err.smtp_error}")
        print(f"[OTP Email] Check MAIL_USERNAME / MAIL_PASSWORD and any provider-specific app password or security settings.")
    except Exception as e:
        print(f"[OTP Email] Failed to send email to {recipient}: {e}")
        if hasattr(e, 'smtp_code') and hasattr(e, 'smtp_error'):
            print(f"[OTP Email] SMTP response: {e.smtp_code} {e.smtp_error}")
    return False


def create_profile_email_otp(user_id, email):
    email = normalize_email(email)
    otp = f"{secrets.randbelow(1000000):06d}"
    created_at = datetime.datetime.utcnow()
    expires_at = created_at + datetime.timedelta(minutes=5)
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE email_otps SET used = 1 WHERE user_id = ? AND used = 0",
            (user_id,)
        )
        cursor = conn.execute(
            "INSERT INTO email_otps (user_id, email, otp, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, email, otp, created_at.isoformat(), expires_at.isoformat())
        )
        conn.commit()
        return cursor.lastrowid, otp
    finally:
        conn.close()


def verify_profile_email_otp(user_id, email, entered_otp):
    email = normalize_email(email)
    entered_otp = normalize_otp_code(entered_otp)
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, otp, expires_at FROM email_otps "
            "WHERE user_id = ? AND email = ? AND used = 0 AND verified = 0 "
            "ORDER BY id DESC LIMIT 1",
            (user_id, email)
        ).fetchone()
        if not row:
            return 'invalid'

        expires_at = datetime.datetime.fromisoformat(row['expires_at'])
        if expires_at < datetime.datetime.utcnow():
            conn.execute("UPDATE email_otps SET used = 1 WHERE id = ?", (row['id'],))
            conn.commit()
            return 'expired'

        if str(row['otp']).strip() != str(entered_otp).strip():
            return 'invalid'

        conn.execute(
            "UPDATE email_otps SET verified = 1, used = 1 WHERE id = ? AND used = 0 AND verified = 0",
            (row['id'],)
        )
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
        conn.commit()
        return 'verified'
    finally:
        conn.close()


def profile_email_is_verified(user_id, email):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM email_otps WHERE user_id = ? AND email = ? AND verified = 1 AND used = 1 ORDER BY id DESC LIMIT 1",
            (user_id, normalize_email(email))
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_registered_email(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        return normalize_email(row['email']) if row and row['email'] else ''
    finally:
        conn.close()


def send_account_recovery_email(email, username, role):
    subject = 'FeedForward Username Recovery'
    sender = app.config.get('MAIL_DEFAULT_SENDER') or app.config.get('MAIL_USERNAME')
    recipient = email
    body = f"Hello {username or 'User'},\n\n" \
           f"You requested your FeedForward username for the {role.capitalize()} account.\n\n" \
           f"Your username is: {username or 'Not available'}\n\n" \
           "If you did not request this information, please ignore this email.\n\n" \
           "Thank you,\nFeedForward Team"

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = recipient
    message.set_content(body)

    mail_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port = int(app.config.get('MAIL_PORT', 587))
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')
    use_tls = app.config.get('MAIL_USE_TLS', True)
    use_ssl = app.config.get('MAIL_USE_SSL', False)

    if not mail_user or not mail_pass:
        if mail_server in ('localhost', '127.0.0.1') or mail_port in (25,):
            try:
                with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                    server.send_message(message)
                return True
            except Exception:
                return False
        return False

    try:
        if use_ssl or mail_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(mail_server, mail_port, context=context, timeout=15) as server:
                server.ehlo()
                server.login(mail_user, mail_pass)
                server.send_message(message)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP(mail_server, mail_port, timeout=15) as server:
                server.ehlo()
                if use_tls and mail_port in (587, 25, None):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(mail_user, mail_pass)
                server.send_message(message)
        return True
    except Exception:
        return False


def is_email_registered(email, role=None):
    normalized = email.strip().lower() if email else email
    conn = get_db_connection()
    try:
        if role:
            row = conn.execute(
                "SELECT 1 FROM users u WHERE LOWER(u.email) = ? AND (u.role = ? OR EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = u.id AND ur.role = ?))",
                (normalized, role, role)
            ).fetchone()
        else:
            row = conn.execute("SELECT id FROM users WHERE LOWER(email) = ?", (normalized,)).fetchone()
        return bool(row)
    finally:
        conn.close()


def is_phone_registered(phone, role=None):
    normalized = normalize_phone(phone)
    if not normalized:
        return False

    candidates = [normalized]
    if normalized.startswith('+'):
        candidates.append(normalized[1:])
    elif re.match(r'^[6789][0-9]{9}$', normalized):
        candidates.append('+91' + normalized)

    placeholder = ','.join(['?'] * len(candidates))
    conn = get_db_connection()
    try:
        tables = {
            'donor': ['donors'],
            'ngo': ['ngos'],
            'volunteer': ['volunteers'],
            'admin': ['admins'],
        }.get(role, ['users', 'donors', 'ngos', 'volunteers', 'admins'])
        for table in tables:
            if conn.execute(f"SELECT id FROM {table} WHERE phone IN ({placeholder})", tuple(candidates)).fetchone():
                return True
        return False
    finally:
        conn.close()


def is_registration_otp_verified(email):
    verified_email = session.get('registration_email_verified')
    return bool(verified_email and verified_email == normalize_email(email))


@app.route('/api/register/send-otp', methods=['POST'])
def api_register_send_otp():
    data = request.get_json() or {}
    full_name = data.get('full_name', '').strip()
    email = normalize_email(data.get('email', ''))
    phone = data.get('phone', '').strip()
    role = (data.get('role') or '').strip().lower()

    if not full_name:
        return jsonify({'success': False, 'error': 'Full Name is required to send OTP.'})
    if not email:
        return jsonify({'success': False, 'error': 'Email is required to send OTP.'})
    if not phone:
        return jsonify({'success': False, 'error': 'Phone Number is required to send OTP.'})
    if role not in {'donor', 'ngo', 'volunteer'}:
        return jsonify({'success': False, 'error': 'Please select a role before requesting an OTP.'})

    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_pattern, email):
        return jsonify({'success': False, 'error': 'Invalid email address format.'})

    phone_pattern = r'^[6789][0-9]{9}$'
    if not re.match(phone_pattern, phone):
        return jsonify({'success': False, 'error': 'Phone number must be exactly 10 digits and start with 6, 7, 8, or 9.'})

    if is_email_registered(email, role):
        return jsonify({'success': False, 'exists': True, 'error': 'Email is already registered. Please use another email or login.'})

    phone_owner = find_user_by_phone(phone)
    if phone_owner:
        return jsonify({'success': False, 'error': 'This phone number is already registered.'})

    otp = generate_otp_code()
    store_otp('register_email', email, otp, expires_minutes=5)

    email_sent = send_email_code(email, otp, username=full_name)
    if not email_sent:
        return jsonify({'success': False, 'error': 'Failed to send verification email. Please verify SMTP configuration and try again.'})

    session.pop('registration_email_verified', None)
    return jsonify({'success': True, 'message': 'Verification code sent to your email. Please enter the OTP to continue.'})


@app.route('/api/register/verify-otp', methods=['POST'])
def api_register_verify_otp():
    data = request.get_json() or {}
    email = normalize_email(data.get('email', ''))
    otp_code = normalize_otp_code(data.get('otp_code') or data.get('verification_code') or data.get('code'))

    if not email or not otp_code:
        return jsonify({'success': False, 'error': 'Email and OTP code are required.'})

    if verify_otp('register_email', email, otp_code):
        session['registration_email_verified'] = email
        return jsonify({'success': True, 'message': 'OTP verified successfully. You may complete your registration.'})

    return jsonify({'success': False, 'error': 'Invalid or expired verification code / OTP.'})

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.get(int(user_id))
    except (TypeError, ValueError):
        return None

# Context Processor to make notifications and utility variables available globally in templates
@app.context_processor
def inject_globals():
    if current_user.is_authenticated:
        conn = get_db_connection()
        unread_notifications = conn.execute(
            "SELECT id, message, created_at FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
            (current_user.id,)
        ).fetchall()

        # Get role profile id
        profile_id = None
        if current_user.role == 'donor':
            p = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
            if p: profile_id = p['id']
        elif current_user.role == 'ngo':
            p = conn.execute("SELECT id FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
            if p: profile_id = p['id']
        elif current_user.role == 'volunteer':
            p = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
            if p: profile_id = p['id']
            
        conn.close()
        return dict(unread_notifications=unread_notifications, profile_id=profile_id, datetime=datetime)
    return dict(unread_notifications=[], profile_id=None, datetime=datetime)

# --- HOME & COMMON ROUTES ---

@app.route('/')
def index():
    """Application Landing Page."""
    conn = get_db_connection()
    stats = {
         'total_donations': conn.execute(
             """SELECT (SELECT COUNT(*) FROM food_donations) +
                 (SELECT COUNT(*) FROM clothes_donations) +
                 (SELECT COUNT(*) FROM book_donations) +
                 (SELECT COUNT(*) FROM money_donations)"""
         ).fetchone()[0],
        'active_donations': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status = 'Pending'").fetchone()[0],
        'total_donors': conn.execute("SELECT COUNT(*) FROM donors").fetchone()[0],
        'total_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 1").fetchone()[0],
        'total_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 1").fetchone()[0],
    }

    recent_listings = conn.execute(
         """SELECT d.id, 'food' AS donation_type, d.food_name AS title, d.category,
                d.quantity, d.expiry_time, d.address, d.image_path, d.status,
                d.created_at, d.veg_non_veg
            FROM food_donations d
            WHERE d.status = 'Pending'
            UNION ALL
            SELECT d.id, 'clothes', d.item_type, d.category, d.quantity, NULL,
                d.address, d.image_path, d.status, d.created_at, NULL
            FROM clothes_donations d
            WHERE d.status = 'Pending'
            UNION ALL
            SELECT d.id, 'books', d.title, d.category, d.quantity, NULL,
                d.address, d.image_path, d.status, d.created_at, NULL
            FROM book_donations d
            WHERE d.status = 'Pending'
            UNION ALL
            SELECT d.id, 'money', d.purpose, 'Money Support', printf('%.2f', d.amount), NULL,
                o.address, NULL, d.status, d.created_at, NULL
            FROM money_donations d
            LEFT JOIN orphanages o ON o.id = d.orphanage_id
            WHERE d.status = 'Pending'
            ORDER BY created_at DESC
            LIMIT 3"""
    ).fetchall()
    conn.close()
    return render_template('index.html', stats=stats, recent_listings=recent_listings)


@app.route('/aboutus')
@app.route('/aboutus.html')
def aboutus():
    """Show the public About page with current platform totals."""
    conn = get_db_connection()
    try:
        donation_counts = conn.execute(
            """SELECT
                (SELECT COUNT(*) FROM food_donations) +
                (SELECT COUNT(*) FROM clothes_donations) +
                (SELECT COUNT(*) FROM book_donations) +
                (SELECT COUNT(*) FROM money_donations) AS total_donations"""
        ).fetchone()
        about_stats = {
            'total_donations': donation_counts['total_donations'],
            'community_members': sum(
                conn.execute(query).fetchone()[0]
                for query in (
                    "SELECT COUNT(*) FROM donors WHERE is_approved = 1",
                    "SELECT COUNT(*) FROM ngos WHERE is_verified = 1",
                    "SELECT COUNT(*) FROM volunteers WHERE is_approved = 1",
                )
            ),
            'successful_distributions': conn.execute(
                "SELECT COUNT(*) FROM food_distribution WHERE status IN ('Delivered', 'Completed')"
            ).fetchone()[0],
        }
    finally:
        conn.close()

    return render_template('aboutus.html', about_stats=about_stats)

@app.route('/donor/donate/clothes', methods=['GET', 'POST'])
@login_required
def donor_donate_clothes():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        flash('Please complete your donor profile before listing donations.', 'danger')
        return redirect(url_for('donor_dashboard'))

    form = ClothesDonationForm()
    if request.method == 'GET':
        form.address.data = donor['address']
        form.latitude.data = donor['latitude']
        form.longitude.data = donor['longitude']

    if form.validate_on_submit():
        selected_category = form.category.data
        if selected_category == 'Other':
            custom_category = (form.other_category.data or '').strip()
            if not custom_category:
                flash('Please specify the clothing category.', 'danger')
                conn.close()
                return redirect(url_for('donor_donate_clothes'))
            selected_category = custom_category

        image_file = request.files.get('clothes_image')
        image_relative_path = None
        if image_file and getattr(image_file, 'filename', None):
            filename = secure_filename(f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{image_file.filename}")
            filepath = os.path.join(Config.UPLOAD_FOLDER, 'food', filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            image_file.save(filepath)
            image_relative_path = f"uploads/food/{filename}"

        lat = float(form.latitude.data) if form.latitude.data else donor['latitude']
        lng = float(form.longitude.data) if form.longitude.data else donor['longitude']
        try:
            ClothesDonation.create(
                donor_id=donor['id'],
                item_type=form.item_type.data.strip(),
                category=selected_category,
                size=form.size.data.strip(),
                quantity=form.quantity.data.strip(),
                condition=form.condition.data,
                pickup_time=form.pickup_time.data.strip(),
                address=form.address.data.strip(),
                latitude=lat,
                longitude=lng,
                image_path=image_relative_path
            )
        except Exception:
            app.logger.exception('Unable to save clothes donation')
            conn.close()
            flash('Unable to add donation. Please try again.', 'danger')
            return render_template('donor_donate_clothes.html', form=form), 500
        conn.close()
        flash('clothes', 'donation_success')
        return redirect(url_for('donor_donate_clothes'))

    conn.close()
    return render_template('donor_donate_clothes.html', form=form)


@app.route('/donor/donate/books', methods=['GET', 'POST'])
@login_required
def donor_donate_books():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        flash('Please complete your donor profile before listing donations.', 'danger')
        return redirect(url_for('donor_dashboard'))

    form = BookDonationForm()
    if request.method == 'GET':
        form.address.data = donor['address']
        form.latitude.data = donor['latitude']
        form.longitude.data = donor['longitude']

    if form.validate_on_submit():
        selected_category = form.category.data
        if selected_category == 'Other':
            custom_category = (form.other_category.data or '').strip()
            if not custom_category:
                flash('Please specify the book category.', 'danger')
                conn.close()
                return redirect(url_for('donor_donate_books'))
            selected_category = custom_category

        image_file = request.files.get('book_image')
        image_relative_path = None
        if image_file and getattr(image_file, 'filename', None):
            filename = secure_filename(f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{image_file.filename}")
            filepath = os.path.join(Config.UPLOAD_FOLDER, 'food', filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            image_file.save(filepath)
            image_relative_path = f"uploads/food/{filename}"

        lat = float(form.latitude.data) if form.latitude.data else donor['latitude']
        lng = float(form.longitude.data) if form.longitude.data else donor['longitude']
        try:
            BookDonation.create(
                donor_id=donor['id'],
                title=form.title.data.strip(),
                category=selected_category,
                quantity=form.quantity.data.strip(),
                condition=form.condition.data,
                pickup_time=form.pickup_time.data.strip(),
                address=form.address.data.strip(),
                author=form.author.data.strip() if form.author.data else None,
                latitude=lat,
                longitude=lng,
                image_path=image_relative_path
            )
        except Exception:
            app.logger.exception('Unable to save book donation')
            conn.close()
            flash('Unable to add donation. Please try again.', 'danger')
            return render_template('donor_donate_books.html', form=form), 500
        conn.close()
        flash('book', 'donation_success')
        return redirect(url_for('donor_donate_books'))

    conn.close()
    return render_template('donor_donate_books.html', form=form)


def _save_donor_donation_image(upload, prefix):
    if not upload or not upload.filename:
        return None
    filename = secure_filename(f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{prefix}_{upload.filename}")
    filepath = os.path.join(Config.UPLOAD_FOLDER, 'food', filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    upload.save(filepath)
    return f"uploads/food/{filename}"


@app.route('/donor/donation/clothes/<int:donation_id>/edit', methods=['GET', 'POST'])
@login_required
def donor_edit_clothes_donation(donation_id):
    if current_user.role != 'donor':
        return redirect(url_for('index'))
    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM clothes_donations WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone() if donor else None
    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('donor_history'))
    if donation['status'] not in ('Pending', 'Cancelled'):
        conn.close()
        flash('Only pending or cancelled clothes donations can be edited.', 'warning')
        return redirect(url_for('donor_history'))
    form = ClothesDonationForm()
    if request.method == 'GET':
        for field in ('item_type', 'category', 'size', 'quantity', 'condition', 'pickup_time', 'address', 'latitude', 'longitude'):
            getattr(form, field).data = donation[field]
    if form.validate_on_submit():
        category = form.category.data
        if category == 'Other':
            category = (form.other_category.data or '').strip()
            if not category:
                flash('Please specify the clothing category.', 'danger')
                conn.close()
                return render_template('donor_donate_clothes.html', form=form, edit_mode=True, donation=donation)
        image_path = _save_donor_donation_image(request.files.get('clothes_image'), 'clothes') or donation['image_path']
        latitude = float(form.latitude.data) if form.latitude.data else donor['latitude']
        longitude = float(form.longitude.data) if form.longitude.data else donor['longitude']
        conn.execute("""UPDATE clothes_donations SET item_type=?, category=?, size=?, quantity=?, condition=?, pickup_time=?, address=?, latitude=?, longitude=?, image_path=? WHERE id=? AND donor_id=?""", (form.item_type.data.strip(), category, form.size.data.strip(), form.quantity.data.strip(), form.condition.data, form.pickup_time.data.strip(), form.address.data.strip(), latitude, longitude, image_path, donation_id, donor['id']))
        conn.commit()
        conn.close()
        flash('Your clothes donation has been updated successfully.', 'success')
        return redirect(url_for('donor_history'))
    conn.close()
    return render_template('donor_donate_clothes.html', form=form, edit_mode=True, donation=donation)


@app.route('/donor/donation/books/<int:donation_id>/edit', methods=['GET', 'POST'])
@login_required
def donor_edit_books_donation(donation_id):
    if current_user.role != 'donor':
        return redirect(url_for('index'))
    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM book_donations WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone() if donor else None
    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('donor_history'))
    if donation['status'] not in ('Pending', 'Cancelled'):
        conn.close()
        flash('Only pending or cancelled book donations can be edited.', 'warning')
        return redirect(url_for('donor_history'))
    form = BookDonationForm()
    if request.method == 'GET':
        for field in ('title', 'author', 'category', 'quantity', 'condition', 'pickup_time', 'address', 'latitude', 'longitude'):
            getattr(form, field).data = donation[field]
    if form.validate_on_submit():
        category = form.category.data
        if category == 'Other':
            category = (form.other_category.data or '').strip()
            if not category:
                flash('Please specify the book category.', 'danger')
                conn.close()
                return render_template('donor_donate_books.html', form=form, edit_mode=True, donation=donation)
        image_path = _save_donor_donation_image(request.files.get('book_image'), 'books') or donation['image_path']
        latitude = float(form.latitude.data) if form.latitude.data else donor['latitude']
        longitude = float(form.longitude.data) if form.longitude.data else donor['longitude']
        conn.execute("""UPDATE book_donations SET title=?, author=?, category=?, quantity=?, condition=?, pickup_time=?, address=?, latitude=?, longitude=?, image_path=? WHERE id=? AND donor_id=?""", (form.title.data.strip(), (form.author.data or '').strip(), category, form.quantity.data.strip(), form.condition.data, form.pickup_time.data.strip(), form.address.data.strip(), latitude, longitude, image_path, donation_id, donor['id']))
        conn.commit()
        conn.close()
        flash('Your book donation has been updated successfully.', 'success')
        return redirect(url_for('donor_history'))
    conn.close()
    return render_template('donor_donate_books.html', form=form, edit_mode=True, donation=donation)


@app.route('/donor/donate/money', methods=['GET', 'POST'])
@login_required
def donor_donate_money():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        flash('Please complete your donor profile before making a donation.', 'danger')
        return redirect(url_for('donor_dashboard'))

    conn.execute(
        """INSERT INTO orphanages (name, contact_person, phone, email, address, is_approved, created_by)
           SELECT n.ngo_name, n.contact_person, n.phone, u.email, n.address, 1, n.user_id
           FROM ngos n
           JOIN users u ON u.id = n.user_id
           WHERE n.is_verified = 1
             AND NOT EXISTS (
                 SELECT 1 FROM orphanages o WHERE o.created_by = n.user_id
             )"""
    )
    conn.commit()
    approved_orphanages = conn.execute("SELECT id, name FROM orphanages WHERE is_approved = 1 ORDER BY name").fetchall()
    form = MoneyDonationForm()
    form.orphanage_id.choices = [(str(row['id']), row['name']) for row in approved_orphanages]

    if form.validate_on_submit():
        orphanage = conn.execute("SELECT * FROM orphanages WHERE id = ? AND is_approved = 1", (int(form.orphanage_id.data),)).fetchone()
        if not orphanage:
            conn.close()
            flash('Please select an approved orphanage or organization to continue.', 'warning')
            return redirect(url_for('donor_donate_money'))

        if not Config.RAZORPAY_KEY_ID or not Config.RAZORPAY_KEY_SECRET:
            conn.close()
            flash('Online payments are not configured yet. Add RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to .env.', 'danger')
            return render_template('donor_donate_money.html', form=form, donation_type='Money for Orphans', donation_type_label='Money for Orphans', donation_path='money'), 503

        amount = float(form.amount.data)
        if amount <= 0:
            conn.close()
            flash('Donation amount must be greater than zero.', 'danger')
            return render_template('donor_donate_money.html', form=form, donation_type='Money for Orphans', donation_type_label='Money for Orphans', donation_path='money'), 400

        try:
            razorpay_client = razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))
            order = razorpay_client.order.create({
                'amount': int(round(amount * 100)),
                'currency': 'INR',
                'receipt': f'ff_money_{current_user.id}_{int(datetime.datetime.now().timestamp())}',
                'notes': {'purpose': form.purpose.data, 'orphanage_id': str(orphanage['id'])},
            })
        except Exception as exc:
            conn.close()
            app.logger.exception('Razorpay order creation failed')
            flash(f'Unable to start payment: {exc}', 'danger')
            return render_template('donor_donate_money.html', form=form, donation_type='Money for Orphans', donation_type_label='Money for Orphans', donation_path='money'), 502

        donation_id = MoneyDonation.create(
            donor_id=donor['id'],
            amount=amount,
            purpose=form.purpose.data,
            payment_method='Razorpay',
            note=form.note.data.strip() if form.note.data else None,
            orphanage_id=orphanage['id'],
            razorpay_order_id=order['id'],
        )
        conn.close()
        return render_template(
            'donor_donate_money.html',
            form=form,
            donation_type='Money for Orphans',
            donation_type_label='Money for Orphans',
            donation_path='money',
            razorpay_key_id=Config.RAZORPAY_KEY_ID,
            razorpay_order=order,
            donation_id=donation_id,
            donor_name=donor['contact_name'],
            donor_email=current_user.email,
            donor_phone=donor['phone'],
        )

    if not form.orphanage_id.choices:
        flash('No approved orphanage or organization is available yet. Please check back after admin approval.', 'warning')

    conn.close()
    return render_template('donor_donate_money.html', form=form, donation_type='Money for Orphans', donation_type_label='Money for Orphans', donation_path='money')


@app.route('/donor/donate/money/payment-success', methods=['POST'])
@login_required
def donor_money_payment_success():
    payload = request.get_json(silent=True) or request.form
    order_id = payload.get('razorpay_order_id', '')
    payment_id = payload.get('razorpay_payment_id', '')
    signature = payload.get('razorpay_signature', '')
    if not order_id or not payment_id or not signature:
        return jsonify({'success': False, 'verified': False, 'error': 'Incomplete payment response.'}), 400

    conn = get_db_connection()
    duplicate = conn.execute(
        "SELECT id FROM money_donations WHERE razorpay_payment_id = ?",
        (payment_id,),
    ).fetchone()
    if duplicate:
        conn.close()
        return jsonify({'success': True, 'verified': True, 'message': 'Payment was already verified.'})

    try:
        razorpay_client = razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })
    except Exception:
        conn.close()
        app.logger.warning('Razorpay payment signature verification failed for order %s', order_id)
        return jsonify({'success': False, 'verified': False, 'error': 'Payment signature verification failed.'}), 400

    payment_method = 'Razorpay'
    try:
        payment_details = razorpay_client.payment.fetch(payment_id)
        payment_method = str(payment_details.get('method') or 'Razorpay').replace('_', ' ').title()
    except Exception:
        app.logger.warning('Unable to fetch Razorpay payment method for payment %s', payment_id)

    donation = conn.execute(
        """SELECT d.id FROM money_donations d JOIN donors dn ON dn.id = d.donor_id
           WHERE d.razorpay_order_id = ? AND dn.user_id = ?""",
        (order_id, current_user.id),
    ).fetchone()
    if not donation:
        conn.close()
        return jsonify({'success': False, 'verified': False, 'error': 'Payment record was not found.'}), 404

    conn.execute(
        "UPDATE money_donations SET status = 'Paid', payment_status = 'Verified', donation_status = 'Verified', verified_at = CURRENT_TIMESTAMP, payment_method = ?, razorpay_payment_id = ?, razorpay_signature = ? WHERE id = ?",
        (payment_method, payment_id, signature, donation['id']),
    )
    _record_lifecycle_activity(conn, 'money', donation['id'], 'Payment Completed', 'Paid')
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'verified': True, 'message': 'Payment verified successfully.'})


@app.route('/donor/money-donation/<int:donation_id>/receipt')
@login_required
def donor_money_receipt(donation_id):
    if current_user.role != 'donor':
        return redirect(url_for('index'))
    conn = get_db_connection()
    donation = conn.execute(
        """SELECT d.*, o.name AS orphanage_name, dn.contact_name, u.email
           FROM money_donations d
           JOIN donors dn ON dn.id = d.donor_id
           JOIN users u ON u.id = dn.user_id
           LEFT JOIN orphanages o ON o.id = d.orphanage_id
           WHERE d.id = ? AND dn.user_id = ?""",
        (donation_id, current_user.id),
    ).fetchone()
    conn.close()
    if not donation:
        return 'Receipt not found', 404
    if donation['status'] != 'Paid':
        return 'Receipt is available after successful payment.', 409
    return render_template('money_receipt.html', donation=donation)


@app.route('/interactive-sandbox')
def home():
    """Innovative Showcase & Simulator Page."""
    conn = get_db_connection()
    # Stats for homepage
    stats = {
        'total_food_saved_kg': 0,
        'active_donations': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status = 'Pending'").fetchone()[0],
        'total_donors': conn.execute("SELECT COUNT(*) FROM donors").fetchone()[0],
        'total_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 1").fetchone()[0],
        'total_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 1").fetchone()[0],
    }
    
    # Calculate approximate food saved (sum quantities of Delivered)
    delivered_quantities = conn.execute("SELECT quantity FROM food_donations WHERE status = 'Delivered'").fetchall()
    total_kg = 0
    for row in delivered_quantities:
        qty_str = row['quantity'].lower()
        # Simple extraction of numbers for homepage counters
        nums = [int(s) for s in qty_str.split() if s.isdigit()]
        if nums:
            total_kg += nums[0]
        else:
            total_kg += 5  # fallback default estimate per listing
    stats['total_food_saved_kg'] = total_kg if total_kg > 0 else 450
    
    # Recent active donations
    recent_listings = conn.execute(
        """SELECT d.*, dn.company_name FROM food_donations d 
           JOIN donors dn ON d.donor_id = dn.id 
           WHERE d.status = 'Pending' ORDER BY d.created_at DESC LIMIT 3"""
    ).fetchall()

    # Latest platform feedback
    feedback_entries = conn.execute(
        """SELECT pf.*, u.username FROM platform_feedback pf
           LEFT JOIN users u ON pf.user_id = u.id
           ORDER BY pf.created_at DESC LIMIT 5"""
    ).fetchall()

    emergency_mode_active = get_app_setting('enable_emergency_mode', '1') == '1'
    active_emergencies = []
    if emergency_mode_active:
        active_emergencies = conn.execute(
            "SELECT e.*, u.username as created_by_username FROM emergencies e "
            "LEFT JOIN users u ON e.created_by = u.id "
            "WHERE e.is_active = 1 ORDER BY e.created_at DESC"
        ).fetchall()
    user_emergency_request = None
    if current_user.is_authenticated:
        user_emergency_request = EmergencyModeRequest.get_pending_by_user(current_user.id)
    conn.close()

    form = PlatformFeedbackForm()
    emergency_request_form = EmergencyModeRequestForm() if current_user.is_authenticated else None
    return render_template(
        'home.html',
        stats=stats,
        recent_listings=recent_listings,
        feedback_entries=feedback_entries,
        feedback_form=form,
        emergency_request_form=emergency_request_form,
        active_emergencies=active_emergencies,
        emergency_mode_active=emergency_mode_active,
        user_emergency_request=user_emergency_request
    )


@app.route('/home/feedback', methods=['POST'])
@login_required
def home_feedback():
    form = PlatformFeedbackForm()
    if form.validate_on_submit():
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO platform_feedback (user_id, rating, comments) VALUES (?, ?, ?)",
            (current_user.id, int(form.rating.data), form.comments.data)
        )
        conn.commit()
        conn.close()
        flash('Thank you for your platform feedback!', 'success')
    else:
        flash('Please provide a rating and comments before submitting your feedback.', 'danger')
    return redirect(url_for('home'))


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    form = ContactForm()
    if form.validate_on_submit():
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO contact_messages (user_id, name, email, phone, subject, message) VALUES (?, ?, ?, ?, ?, ?)",
            (
                current_user.id if current_user.is_authenticated else None,
                form.name.data.strip(),
                form.email.data.strip().lower(),
                form.phone.data.strip() if form.phone.data else None,
                form.subject.data.strip(),
                form.message.data.strip(),
            )
        )
        conn.commit()
        conn.close()
        flash('Your message has been sent successfully. Our team will get back to you soon.', 'success')
        return redirect(url_for('contact'))

    return render_template('contact.html', form=form)


def find_user_by_phone(phone_input):
    normalized = normalize_phone(phone_input)
    if not normalized:
        return None

    candidates = [normalized]
    if normalized.startswith('+'):
        candidates.append(normalized[1:])
    elif re.match(r'^[6789][0-9]{9}$', normalized):
        candidates.append('+91' + normalized)

    placeholder = ','.join(['?'] * len(candidates))
    conn = get_db_connection()
    try:
        # First try direct users table lookup
        row = conn.execute(f"SELECT id FROM users WHERE phone IN ({placeholder})", tuple(candidates)).fetchone()
        if row:
            return User.get(row['id'])

        # Then try role-specific tables if phone is stored there
        for table in ['donors', 'ngos', 'volunteers', 'admins']:
            row = conn.execute(f"SELECT user_id FROM {table} WHERE phone IN ({placeholder})", tuple(candidates)).fetchone()
            if row:
                return User.get(row['user_id'])
    finally:
        conn.close()

    return None


def user_has_role(user_id, role):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?",
            (user_id, role)
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def user_phone_owned_by_different_user(phone, user_id):
    normalized = normalize_phone(phone)
    if not normalized:
        return False

    candidates = [normalized]
    if normalized.startswith('+'):
        candidates.append(normalized[1:])
    elif re.match(r'^[6789][0-9]{9}$', normalized):
        candidates.append('+91' + normalized)

    placeholder = ','.join(['?'] * len(candidates))
    conn = get_db_connection()
    try:
        row = conn.execute(
            f"SELECT id FROM users WHERE phone IN ({placeholder}) AND id != ?",
            tuple(candidates) + (user_id,)
        ).fetchone()
        if row:
            return True

        for table in ['donors', 'ngos', 'volunteers', 'admins']:
            row = conn.execute(
                f"SELECT user_id FROM {table} WHERE phone IN ({placeholder}) AND user_id != ?",
                tuple(candidates) + (user_id,)
            ).fetchone()
            if row:
                return True

        return False
    finally:
        conn.close()


def _ensure_certificate_for_delivery(conn, donation_id, volunteer_id=None):
    """Ensure a certificate exists for the given donation_id. Safe to call multiple times.
    Returns True if certificate was created, False if already existed or on failure.
    """
    try:
        existing = conn.execute("SELECT id FROM certificates WHERE donation_id = ?", (donation_id,)).fetchone()
        if existing:
            return False

        donation = conn.execute(
            """SELECT d.*, dn.company_name AS donor_name, n.ngo_name AS ngo_name, fd.ngo_id, fd.volunteer_id
               FROM food_donations d
               JOIN food_distribution fd ON d.id = fd.donation_id
               JOIN donors dn ON d.donor_id = dn.id
               JOIN ngos n ON fd.ngo_id = n.id
               WHERE d.id = ?""",
            (donation_id,)
        ).fetchone()
        if not donation:
            print(f"[CERT] Donation {donation_id} not found for certificate generation.")
            return False

        # Determine volunteer to use (passed-in or from distribution record)
        vol_id = volunteer_id if volunteer_id is not None else donation.get('volunteer_id')

        # resolve volunteer name if available
        volunteer_name = ''
        if vol_id:
            vrow = conn.execute("SELECT name FROM volunteers WHERE id = ?", (vol_id,)).fetchone()
            if vrow:
                volunteer_name = vrow.get('name')
        try:
            cert_path = generate_delivery_certificate(
                donation_id,
                dict(donation),
                donation.get('donor_name') or '',
                donation.get('ngo_name') or '',
                volunteer_name
            )
        except Exception as e:
            print(f"[CERT] Error generating certificate for donation {donation_id}: {e}")
            return False

        # insert record - try to get volunteer name/id from donation if available
        v_id = vol_id
        d_id = donation.get('donor_id') if donation.get('donor_id') is not None else None
        n_id = donation.get('ngo_id') if donation.get('ngo_id') is not None else None

        conn.execute(
            "INSERT INTO certificates (donation_id, volunteer_id, donor_id, ngo_id, certificate_path) VALUES (?, ?, ?, ?, ?)",
            (donation_id, v_id, d_id, n_id, cert_path)
        )
        record_donation_activity(conn, 'food', donation_id, 'Certificate Generated', 'Delivered')
        conn.commit()
        print(f"[CERT] Created certificate for donation {donation_id}: {cert_path}")
        return True
    except Exception as e:
        print(f"[CERT] Unexpected error ensuring certificate for {donation_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _record_lifecycle_activity(conn, donation_type, donation_id, event, status=None):
    record_donation_activity(
        conn,
        donation_type,
        donation_id,
        event,
        status,
        actor_user_id=current_user.id if current_user.is_authenticated else None,
        actor_role=current_user.role if current_user.is_authenticated else None,
    )


def _ensure_non_food_certificate(conn, donation_type, donation_id, volunteer_id=None):
    """Create one certificate for a completed non-Food donation."""
    existing = conn.execute(
        "SELECT id FROM donation_certificates WHERE donation_type = ? AND donation_id = ?",
        (donation_type, donation_id),
    ).fetchone()
    if existing:
        return False

    table = {'clothes': 'clothes_donations', 'books': 'book_donations', 'money': 'money_donations'}[donation_type]
    orphanage_join = "LEFT JOIN orphanages o ON o.id = d.orphanage_id" if donation_type == 'money' else ""
    orphanage_fields = "o.name AS orphanage_name, o.address AS orphanage_address" if donation_type == 'money' else "NULL AS orphanage_name, NULL AS orphanage_address"
    donation = conn.execute(
        f"""SELECT d.*, dn.company_name AS donor_name, n.ngo_name, dd.ngo_id, dd.volunteer_id,
                   {orphanage_fields}
            FROM {table} d
            JOIN donors dn ON dn.id = d.donor_id
            LEFT JOIN donation_distribution dd ON dd.donation_type = ? AND dd.donation_id = d.id
            LEFT JOIN ngos n ON n.id = dd.ngo_id
           {orphanage_join}
            WHERE d.id = ?""",
        (donation_type, donation_id),
    ).fetchone()
    if not donation:
        return False

    donation_details = dict(donation)
    if donation_type == 'clothes':
        donation_details['food_name'] = donation_details.get('item_type') or donation_details.get('category')
    elif donation_type == 'books':
        donation_details['food_name'] = donation_details.get('title') or donation_details.get('category')
    else:
        donation_details['food_name'] = donation_details.get('purpose') or donation_details.get('orphanage_name') or 'Money donation'
        donation_details['quantity'] = donation_details.get('amount')
        donation_details['address'] = donation_details.get('orphanage_address') or donation_details.get('address')

    vol_id = volunteer_id if volunteer_id is not None else donation['volunteer_id']
    volunteer_name = ''
    if vol_id:
        volunteer = conn.execute("SELECT name FROM volunteers WHERE id = ?", (vol_id,)).fetchone()
        volunteer_name = volunteer['name'] if volunteer else ''
    if donation_type == 'money':
        volunteer_name = donation['donor_name'] or 'Donor'
    elif not volunteer_name:
        volunteer_name = 'Assigned Volunteer'
    cert_path = generate_delivery_certificate(
        f"{donation_type}_{donation_id}", donation_details, donation['donor_name'], donation['ngo_name'] or '', volunteer_name
    )
    conn.execute(
        """INSERT OR IGNORE INTO donation_certificates
           (donation_type, donation_id, volunteer_id, donor_id, ngo_id, certificate_path)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (donation_type, donation_id, vol_id, donation['donor_id'], donation['ngo_id'], cert_path),
    )
    _record_lifecycle_activity(conn, donation_type, donation_id, 'Certificate Generated', 'Completed')
    conn.commit()
    return True


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_router'))

    form = LoginForm()
    if form.validate_on_submit():
        login_input = (form.username.data or '').strip()
        login_type = (form.user_type.data or '').strip().lower()
        user = None

        if login_input:
            if '@' in login_input:
                user = User.find_by_email_and_role(login_input, login_type)
                if not user:
                    user = User.find_by_email(login_input)
            if not user:
                user = User.find_by_username_and_role(login_input, login_type)
            if not user:
                user = User.find_by_username(login_input)

        if user and login_type and not (user.role == login_type or user.has_role(login_type)):
            # Allow the user to sign in with their primary/active role when the selected login type
            # is not stored for this account, but avoid rejecting valid accounts unnecessarily.
            if not user.roles or not any((r or '').strip().lower() == login_type for r in user.roles):
                flash('Selected account type does not match your username or your account does not have that role.', 'danger')
                return render_template('login.html', form=form)

        if user and user.check_password(form.password.data):
            if getattr(user, 'status', None) == 'suspended':
                flash('Your account has been suspended by the administrator.', 'danger')
                return render_template('login.html', form=form)

            effective_role = login_type if login_type and (user.role == login_type or user.has_role(login_type)) else (user.role or (user.roles[0] if getattr(user, 'roles', None) else 'donor'))
            session['active_role'] = effective_role
            update_last_login(user.id)
            login_user(user)
            flash(f'Welcome back, {getattr(user, "full_name", None) or getattr(user, "username", "User")}!', 'success')
            return redirect(url_for('dashboard_router'))

        if login_input:
            flash('Invalid username, account type, or password.', 'danger')
        else:
            flash('Please enter your username and password.', 'danger')

    return render_template('login.html', form=form)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        logout_user()

    form = AdminLoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        user = User.find_by_username(username)

        if user and user.role == 'admin' and user.check_password(form.password.data):
            if getattr(user, 'status', None) == 'suspended':
                flash('Your admin account has been suspended.', 'danger')
                return render_template('admin_login.html', form=form)
            update_last_login(user.id)
            login_user(user)
            flash('Admin login successful.', 'success')
            return redirect(url_for('admin_dashboard'))

        flash('Invalid admin username or password.', 'danger')

    return render_template('admin_login.html', form=form)

@app.route('/api/forgot-username', methods=['POST'])
def api_forgot_username():
    from flask import jsonify, request
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    role = data.get('role', '').strip().lower()
    valid_roles = ['donor', 'ngo', 'volunteer']

    if not email:
        return jsonify({'success': False, 'error': 'Email is required.'})
    if not role:
        return jsonify({'success': False, 'error': 'User type is required.'})
    if role not in valid_roles:
        return jsonify({'success': False, 'error': 'Invalid user type selected.'})

    conn = get_db_connection()
    user_row = conn.execute(
        "SELECT username FROM users WHERE LOWER(email) = ? AND role = ? ORDER BY created_at DESC LIMIT 1",
        (email, role)
    ).fetchone()
    conn.close()

    if user_row:
        username = user_row['username']
        email_sent = send_account_recovery_email(email, username, role)
        if email_sent:
            return jsonify({'success': True, 'message': 'Your username has been sent to your email address.'})
        return jsonify({'success': True, 'message': 'Your username was found, but the email could not be sent right now. Please contact support.'})
    else:
        return jsonify({'success': False, 'error': f'No {role.capitalize()} account found with this email address.'})


@app.route('/chatbot', methods=['POST'])
def chatbot_api():
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    context = (data.get('context') or '').strip()

    if not message:
        return jsonify({'message': 'Please type a message to ask the FeedForward Assistant.'}), 400

    reply = generate_chatbot_reply(message, context=context)
    return jsonify({'response': reply, 'message': reply})

@app.route('/api/forgot-password/send-code', methods=['POST'])
def api_forgot_password_send_code():
    from flask import jsonify, request
    import re
    data = request.get_json() or {}
    email = data.get('email', '').strip()
    phone = normalize_phone(data.get('phone', '').strip())
    role = (data.get('role', '') or '').strip().lower()
    valid_roles = ['donor', 'ngo', 'volunteer']

    if not role or role not in valid_roles:
        return jsonify({'success': False, 'error': 'Please select a valid role before continuing.'})

    conn = get_db_connection()
    if email:
        if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid email format. Please enter a valid email address.'})

        user_row = conn.execute(
            "SELECT id, username, email FROM users WHERE LOWER(email) = ? AND role = ?",
            (email.lower(), role)
        ).fetchone()
        conn.close()
        if user_row:
            otp = generate_otp_code()
            store_otp('email', f"{role}:{email.lower()}", otp)
            send_email_code(email, otp, username=user_row['username'])
            return jsonify({'success': True, 'method': 'email', 'otp': otp})
        else:
            return jsonify({'success': False, 'error': f'No {role.capitalize()} account found with this email address.'})
    elif phone:
        if not re.match(r'^(?:[6789][0-9]{9}|\+91[6789][0-9]{9})$', phone):
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid phone number format. Enter 10 digits or +91 followed by 10 digits.'})

        user_row = conn.execute(
            "SELECT id, username, email FROM users WHERE role = ? AND phone = ?",
            (role, phone)
        ).fetchone()
        conn.close()

        if user_row:
            otp = generate_otp_code()
            store_otp('phone', f"{role}:{phone}", otp)
            send_sms_otp(phone, otp)
            return jsonify({'success': True, 'method': 'phone', 'otp': otp})
        else:
            return jsonify({'success': False, 'error': f'No {role.capitalize()} account found with this phone number.'})
    else:
        conn.close()
        return jsonify({'success': False, 'error': 'Please provide either an email or a phone number.'})

@app.route('/api/forgot-password/reset', methods=['POST'])
def api_forgot_password_reset():
    from flask import jsonify, request
    from werkzeug.security import generate_password_hash
    import re
    data = request.get_json() or {}
    method = data.get('method', '').strip()
    identifier = data.get('identifier', '').strip()
    role = (data.get('role', '') or '').strip().lower()
    code = data.get('code', '').strip()
    new_password = data.get('new_password', '').strip()

    if not method or not identifier or not role or not code or not new_password:
        return jsonify({'success': False, 'error': 'All fields are required.'})

    if role not in ['donor', 'ngo', 'volunteer']:
        return jsonify({'success': False, 'error': 'Please select a valid role before resetting your password.'})

    otp_identifier = f"{role}:{identifier.lower() if method == 'email' else normalize_phone(identifier)}"
    if not verify_otp(method, otp_identifier, code):
        return jsonify({'success': False, 'error': 'Invalid or expired verification code / OTP.'})

    if len(new_password) < 6:
        return jsonify({'success': False, 'error': 'New password must be at least 6 characters long.'})
    if not any(char.isdigit() for char in new_password):
        return jsonify({'success': False, 'error': 'New password must contain at least one digit.'})
    if not any(char.isalpha() for char in new_password):
        return jsonify({'success': False, 'error': 'New password must contain at least one letter.'})

    conn = get_db_connection()
    if method == 'email':
        user_row = conn.execute(
            "SELECT id FROM users WHERE LOWER(email) = ? AND role = ?",
            (identifier.lower(), role)
        ).fetchone()
    else:
        user_row = conn.execute(
            "SELECT id FROM users WHERE role = ? AND phone = ?",
            (role, normalize_phone(identifier))
        ).fetchone()

    if not user_row:
        conn.close()
        return jsonify({'success': False, 'error': 'User account could not be verified.'})

    user_id = user_row['id']
    existing_hash = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if existing_hash and check_password_hash(existing_hash['password_hash'], new_password):
        conn.close()
        return jsonify({'success': False, 'error': 'Password already exists. Please choose a different password.'})

    hashed = generate_password_hash(new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': 'Password reset successfully!'})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out successfully.', 'info')
    if current_user.is_authenticated and current_user.role == 'admin':
        return redirect(url_for('admin_login'))
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard_router():
    """Route user to their respective dashboard based on role."""
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif current_user.role == 'donor':
        return redirect(url_for('donor_dashboard'))
    elif current_user.role == 'ngo':
        return redirect(url_for('ngo_dashboard'))
    elif current_user.role == 'volunteer':
        return redirect(url_for('volunteer_dashboard'))
    return redirect(url_for('index'))


def save_profile_image(upload_file, user_id):
    if not upload_file or upload_file.filename == '':
        return None
    filename = secure_filename(upload_file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        return None
    timestamp = int(datetime.datetime.utcnow().timestamp())
    new_filename = f"profile_{user_id}_{timestamp}{ext}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], 'profile', new_filename)
    upload_file.save(save_path)
    return os.path.join('uploads', 'profile', new_filename)


def calculate_profile_completion(user, profile_data):
    fields = [
        bool(user.username),
        bool(user.full_name),
        bool(user.email),
        bool(user.phone),
        bool(user.address),
        bool(user.profile_image_path)
    ]
    if user.role == 'donor':
        fields.extend([
            bool(profile_data and profile_data.get('company_name') if isinstance(profile_data, dict) else profile_data and profile_data['company_name']),
            bool(profile_data and profile_data.get('organization_type') if isinstance(profile_data, dict) else profile_data and profile_data['organization_type'])
        ])
    elif user.role == 'ngo':
        fields.extend([
            bool(profile_data and profile_data.get('ngo_name') if isinstance(profile_data, dict) else profile_data and profile_data['ngo_name']),
            bool(profile_data and profile_data.get('registration_number') if isinstance(profile_data, dict) else profile_data and profile_data['registration_number']),
            bool(profile_data and profile_data.get('organization_type') if isinstance(profile_data, dict) else profile_data and profile_data['organization_type'])
        ])
    elif user.role == 'volunteer':
        fields.extend([
            bool(profile_data and profile_data.get('transport_type') if isinstance(profile_data, dict) else profile_data and profile_data['transport_type'])
        ])
    total = len(fields)
    completed = sum(fields)
    return int((completed / total) * 100) if total else 0


def _build_user_portfolio_data(user):
    conn = get_db_connection()
    profile = user.get_profile_data() or {}
    role = user.role or 'donor'

    if role == 'admin':
        total_donations = sum(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ('food_donations', 'clothes_donations', 'book_donations', 'money_donations')
        )
        completed_assignments = (
            conn.execute("SELECT COUNT(*) FROM food_distribution WHERE status IN ('Delivered', 'Completed')").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM donation_assignments WHERE status IN ('Delivered', 'Completed')").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM pickup_assignments WHERE status IN ('Delivered', 'Completed')").fetchone()[0]
        )
        communities_supported = (
            conn.execute("SELECT COUNT(DISTINCT ngo_id) FROM food_distribution WHERE ngo_id IS NOT NULL AND status != 'Cancelled'").fetchone()[0]
            + conn.execute("SELECT COUNT(DISTINCT ngo_id) FROM donation_assignments WHERE status != 'Cancelled'").fetchone()[0]
            + conn.execute("SELECT COUNT(DISTINCT orphanage_id) FROM money_donations WHERE orphanage_id IS NOT NULL AND status IN ('Approved', 'Paid')").fetchone()[0]
        )
        stats = {
            'total_donations': total_donations,
            'verified_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 1").fetchone()[0],
            'active_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 1").fetchone()[0],
            'completed_assignments': completed_assignments,
            'communities_supported': communities_supported,
            'successful_food_donations': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status IN ('Delivered', 'Completed')").fetchone()[0],
        }
        recent_activity = conn.execute(
            "SELECT 'Donation processed' AS activity, donation_type, created_at AS activity_at, status "
            "FROM ("
            "SELECT 'Food' AS donation_type, created_at, status FROM food_donations "
            "UNION ALL SELECT 'Clothes', created_at, status FROM clothes_donations "
            "UNION ALL SELECT 'Books', created_at, status FROM book_donations "
            "UNION ALL SELECT 'Money', created_at, status FROM money_donations"
            ") UNION ALL "
            "SELECT 'Assignment completed', 'Food distribution', delivered_at, status FROM food_distribution "
            "WHERE delivered_at IS NOT NULL AND status IN ('Delivered', 'Completed') "
            "ORDER BY activity_at DESC LIMIT 8"
        ).fetchall()
        achievements = [
            f"{stats['verified_ngos']} NGOs successfully verified",
            f"{stats['active_volunteers']} active volunteers coordinated",
            f"{stats['total_donations']} donations managed",
            f"{stats['completed_assignments']} assignments completed",
        ]
        projects = [
            {
                'title': 'Food Donation Drive',
                'description': 'Coordinating surplus food listings from donors through NGO and volunteer delivery workflows.',
                'date': 'Ongoing', 'status': 'Active', 'volunteers': stats['active_volunteers'],
                'ngos': stats['verified_ngos'], 'impact': f"{stats['successful_food_donations']} successful food donations"
            },
            {
                'title': 'NGO-Volunteer Coordination',
                'description': 'Maintaining reliable partnerships and assignment coverage across the FeedForward network.',
                'date': 'Ongoing', 'status': 'Active', 'volunteers': stats['active_volunteers'],
                'ngos': stats['verified_ngos'], 'impact': f"{stats['completed_assignments']} completed assignments"
            },
            {
                'title': 'Community Support Campaign',
                'description': 'Supporting communities through coordinated food, clothing, book, and financial contributions.',
                'date': 'Current cycle', 'status': 'Reporting', 'volunteers': stats['active_volunteers'],
                'ngos': stats['verified_ngos'], 'impact': f"{stats['communities_supported']} communities supported"
            },
            {
                'title': 'Emergency Food Distribution',
                'description': 'Monitoring urgent requests and coordinating rapid response with platform partners.',
                'date': 'As needed', 'status': 'Ready', 'volunteers': stats['active_volunteers'],
                'ngos': stats['verified_ngos'], 'impact': 'Response network maintained'
            }
        ]
        display_name = user.full_name or user.username or 'FeedForward Administrator'
        portfolio_title = f"{display_name}'s admin portfolio"
        about = 'Managing the FeedForward platform to connect donors, NGOs, volunteers, and communities while improving food and resource distribution.'
        skills = ['NGO Management', 'Volunteer Coordination', 'Donation Management', 'Community Management', 'Emergency Coordination', 'Platform Administration']
    elif role == 'donor':
        donor = Donor.get_by_user_id(user.id)
        if donor:
            stats = {
                'total_donations': sum([
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
                    for table in ('food_donations', 'clothes_donations', 'book_donations', 'money_donations')
                ]),
                'delivered': sum([
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE donor_id = ? AND status IN ({statuses})", (donor['id'],)).fetchone()[0]
                    for table, statuses in (
                        ('food_donations', "'Delivered', 'Completed'"),
                        ('clothes_donations', "'Delivered', 'Completed'"),
                        ('book_donations', "'Delivered', 'Completed'"),
                        ('money_donations', "'Paid', 'Completed'"),
                    )
                ]),
                'pending': sum([
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE donor_id = ? AND status = 'Pending'", (donor['id'],)).fetchone()[0]
                    for table in ('food_donations', 'clothes_donations', 'book_donations', 'money_donations')
                ]),
                'approved': sum([
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE donor_id = ? AND status IN ('Approved', 'Accepted')", (donor['id'],)).fetchone()[0]
                    for table in ('food_donations', 'clothes_donations', 'book_donations', 'money_donations')
                ]),
                'certificates': (
                    conn.execute("SELECT COUNT(*) FROM certificates WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
                    + conn.execute("SELECT COUNT(*) FROM donation_certificates WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
                ),
            }
            avg_rating = conn.execute(
                "SELECT AVG(f.rating) AS avg_rating FROM feedback f JOIN food_donations d ON f.donation_id = d.id WHERE d.donor_id = ?",
                (donor['id'],)
            ).fetchone()['avg_rating']
            stats['avg_rating'] = round(avg_rating, 1) if avg_rating is not None else 0
            recent_activity = conn.execute(
                """SELECT a.donation_type, a.donation_id, a.event AS status, a.created_at, NULL AS food_name,
                          CASE
                              WHEN a.event IN ('Volunteer Assigned', 'Pickup Completed') THEN COALESCE(
                                  (SELECT v.name FROM food_distribution fd JOIN volunteers v ON v.id = fd.volunteer_id
                                   WHERE a.donation_type = 'food' AND fd.donation_id = a.donation_id LIMIT 1),
                                  (SELECT v.name FROM donation_distribution dd JOIN volunteers v ON v.id = dd.volunteer_id
                                   WHERE a.donation_type = dd.donation_type AND dd.donation_id = a.donation_id LIMIT 1)
                              )
                              WHEN a.event IN ('Donation Accepted', 'Distribution Completed') THEN COALESCE(
                                  (SELECT n.ngo_name FROM food_distribution fd JOIN ngos n ON n.id = fd.ngo_id
                                   WHERE a.donation_type = 'food' AND fd.donation_id = a.donation_id LIMIT 1),
                                  (SELECT n.ngo_name FROM donation_distribution dd JOIN ngos n ON n.id = dd.ngo_id
                                   WHERE a.donation_type = dd.donation_type AND dd.donation_id = a.donation_id LIMIT 1)
                              )
                              WHEN a.event = 'Certificate Generated' THEN COALESCE(
                                  (SELECT n.ngo_name FROM certificates c JOIN ngos n ON n.id = c.ngo_id
                                   WHERE a.donation_type = 'food' AND c.donation_id = a.donation_id LIMIT 1),
                                  (SELECT n.ngo_name FROM donation_certificates dc JOIN ngos n ON n.id = dc.ngo_id
                                   WHERE a.donation_type = dc.donation_type AND dc.donation_id = a.donation_id LIMIT 1),
                                  (SELECT v.name FROM certificates c JOIN volunteers v ON v.id = c.volunteer_id
                                   WHERE a.donation_type = 'food' AND c.donation_id = a.donation_id LIMIT 1),
                                  (SELECT v.name FROM donation_certificates dc JOIN volunteers v ON v.id = dc.volunteer_id
                                   WHERE a.donation_type = dc.donation_type AND dc.donation_id = a.donation_id LIMIT 1)
                              )
                          END AS linked_with
                   FROM donation_activity a
                   WHERE (a.donation_type = 'food' AND a.donation_id IN (SELECT id FROM food_donations WHERE donor_id = ?))
                      OR (a.donation_type = 'clothes' AND a.donation_id IN (SELECT id FROM clothes_donations WHERE donor_id = ?))
                      OR (a.donation_type = 'books' AND a.donation_id IN (SELECT id FROM book_donations WHERE donor_id = ?))
                      OR (a.donation_type = 'money' AND a.donation_id IN (SELECT id FROM money_donations WHERE donor_id = ?))
                   ORDER BY a.created_at DESC LIMIT 8""",
                (donor['id'], donor['id'], donor['id'], donor['id'])
            ).fetchall()
            achievements = [
                f"{stats['delivered']} donated items delivered",
                f"{stats['certificates']} delivery certificates earned",
                f"Average feedback rating: {stats['avg_rating']}/5"
            ]
            display_name = donor['company_name'] or user.full_name or user.username
            portfolio_title = f"{display_name}'s donor portfolio"
        else:
            stats = {'total_donations': 0, 'delivered': 0, 'pending': 0, 'approved': 0, 'certificates': 0, 'avg_rating': 0}
            recent_activity = []
            achievements = ['No donation activity yet']
            display_name = user.full_name or user.username
            portfolio_title = f"{display_name}'s donor portfolio"
    elif role == 'ngo':
        ngo = NGO.get_by_user_id(user.id)
        if ngo:
            stats = {
                'accepted': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ?", (ngo['id'],)).fetchone()[0],
                'delivered': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ? AND status = 'Completed'", (ngo['id'],)).fetchone()[0],
                'active_assignments': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ? AND status NOT IN ('Completed', 'Cancelled')", (ngo['id'],)).fetchone()[0],
                'volunteers': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ? AND volunteer_id IS NOT NULL", (ngo['id'],)).fetchone()[0],
            }
            recent_activity = conn.execute(
                """SELECT a.donation_type, a.donation_id, a.event AS status, a.created_at,
                          v.name AS volunteer_name, NULL AS food_name
                   FROM donation_activity a
                   LEFT JOIN donation_distribution dd ON dd.donation_type = a.donation_type AND dd.donation_id = a.donation_id
                   LEFT JOIN volunteers v ON v.id = dd.volunteer_id
                   WHERE dd.ngo_id = ? OR (a.donation_type = 'food' AND a.donation_id IN (SELECT donation_id FROM food_distribution WHERE ngo_id = ?))
                   ORDER BY a.created_at DESC LIMIT 8""",
                (ngo['id'], ngo['id'])
            ).fetchall()
            achievements = [
                f"{stats['accepted']} donation records managed",
                f"{stats['delivered']} completed deliveries",
                f"{stats['volunteers']} volunteer partnerships"
            ]
            display_name = ngo['ngo_name'] or user.full_name or user.username
            portfolio_title = f"{display_name}'s NGO portfolio"
        else:
            stats = {'accepted': 0, 'delivered': 0, 'active_assignments': 0, 'volunteers': 0}
            recent_activity = []
            achievements = ['No NGO activity yet']
            display_name = user.full_name or user.username
            portfolio_title = f"{display_name}'s NGO portfolio"
    else:
        volunteer = Volunteer.get_by_user_id(user.id)
        if volunteer:
            stats = {
                'assignments': conn.execute("SELECT COUNT(*) FROM pickup_assignments WHERE volunteer_id = ?", (volunteer['id'],)).fetchone()[0],
                'delivered': conn.execute("SELECT COUNT(*) FROM pickup_assignments WHERE volunteer_id = ? AND status = 'Delivered'", (volunteer['id'],)).fetchone()[0],
                'active': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE volunteer_id = ? AND status IN ('Volunteer Assigned', 'Picked Up')", (volunteer['id'],)).fetchone()[0],
                'certificates': conn.execute("SELECT COUNT(*) FROM certificates WHERE volunteer_id = ?", (volunteer['id'],)).fetchone()[0],
            }
            avg_rating = conn.execute(
                "SELECT AVG(f.rating) AS avg_rating FROM feedback f JOIN food_distribution fd ON f.donation_id = fd.donation_id WHERE fd.volunteer_id = ?",
                (volunteer['id'],)
            ).fetchone()['avg_rating']
            stats['avg_rating'] = round(avg_rating, 1) if avg_rating is not None else 0
            recent_activity = conn.execute(
                """SELECT a.donation_type, a.donation_id, a.event AS status, a.created_at,
                          n.ngo_name, NULL AS food_name
                   FROM donation_activity a
                   LEFT JOIN donation_distribution dd ON dd.donation_type = a.donation_type AND dd.donation_id = a.donation_id
                   LEFT JOIN ngos n ON n.id = dd.ngo_id
                   WHERE dd.volunteer_id = ? OR (a.donation_type = 'food' AND a.donation_id IN (SELECT donation_id FROM food_distribution WHERE volunteer_id = ?))
                   ORDER BY a.created_at DESC LIMIT 8""",
                (volunteer['id'], volunteer['id'])
            ).fetchall()
            achievements = [
                f"{stats['delivered']} successful deliveries",
                f"{stats['certificates']} certificates issued",
                f"Average feedback rating: {stats['avg_rating']}/5"
            ]
            display_name = volunteer['name'] or user.full_name or user.username
            portfolio_title = f"{display_name}'s volunteer portfolio"
        else:
            stats = {'assignments': 0, 'delivered': 0, 'active': 0, 'certificates': 0, 'avg_rating': 0}
            recent_activity = []
            achievements = ['No volunteer work yet']
            display_name = user.full_name or user.username
            portfolio_title = f"{display_name}'s volunteer portfolio"

    conn.close()
    return {
        'title': portfolio_title,
        'display_name': display_name,
        'role': role,
        'email': user.email,
        'phone': user.phone,
        'address': user.address,
        'joined_on': user.created_at,
        'profile_image': user.profile_image_path,
        'stats': stats,
        'achievements': achievements,
        'activity': recent_activity,
        'profile_completion': calculate_profile_completion(user, profile)
        , 'about': about if role == 'admin' else None
        , 'skills': skills if role == 'admin' else []
        , 'projects': projects if role == 'admin' else []
    }


@app.route('/portfolio')
@login_required
def portfolio():
    if current_user.role not in {'donor', 'ngo', 'volunteer'}:
        if current_user.role != 'admin':
            return redirect(url_for('dashboard_router'))
    portfolio_data = _build_user_portfolio_data(current_user)
    return render_template('portfolio.html', portfolio=portfolio_data)


@app.route('/portfolio/pdf')
@login_required
def portfolio_pdf():
    if current_user.role not in {'donor', 'ngo', 'volunteer'}:
        if current_user.role != 'admin':
            return redirect(url_for('dashboard_router'))
    portfolio_data = _build_user_portfolio_data(current_user)
    pdf_buffer = export_user_portfolio_to_pdf(portfolio_data)
    filename = f"{(current_user.username or 'feedforward').replace(' ', '_')}_portfolio.pdf"
    return send_file(pdf_buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    form = ProfileForm()
    registered_email = get_registered_email(current_user.id)
    profile_data = current_user.get_profile_data()
    profile_completion = calculate_profile_completion(current_user, profile_data)
    profile_image_url = current_user.profile_image_path or None

    if request.method == 'GET':
        form.username.data = current_user.username or ''
        form.full_name.data = current_user.full_name or ''
        form.phone.data = current_user.phone or ''
        form.address.data = current_user.address or ''
        form.email.data = registered_email
        form.account_role.data = current_user.role.capitalize()
        if profile_data:
            if current_user.role == 'donor':
                form.company_name.data = profile_data['company_name']
                form.organization_type.data = profile_data['organization_type'] or ''
                if form.organization_type.data and form.organization_type.data.startswith('Other'):
                    form.organization_type_other.data = form.organization_type.data.replace('Other (', '').replace(')', '')
            elif current_user.role == 'ngo':
                form.company_name.data = profile_data['ngo_name']
                form.registration_number.data = profile_data['registration_number']
                form.organization_type.data = profile_data['organization_type'] or ''
                form.organization_type_other.data = form.organization_type.data.replace('Other (', '').replace(')', '') if form.organization_type.data and form.organization_type.data.startswith('Other') else ''
            elif current_user.role == 'volunteer':
                form.company_name.data = profile_data['name']
                form.transport_type.data = profile_data['transport_type'] or ''
        return render_template('profile.html', form=form, profile_data=profile_data, profile_completion=profile_completion, profile_image_url=profile_image_url)

    if form.validate_on_submit():
        submitted_email = normalize_email(form.email.data)
        if submitted_email != registered_email:
            form.email.data = registered_email
            flash('You can only use your registered email address.', 'danger')
            return render_template('profile.html', form=form, profile_data=profile_data, profile_completion=profile_completion, profile_image_url=profile_image_url)

        if form.new_password.data:
            if not form.current_password.data or not current_user.check_password(form.current_password.data):
                flash('Current password is required and must be correct to change your password.', 'danger')
                return render_template('profile.html', form=form, profile_data=profile_data, profile_completion=profile_completion, profile_image_url=profile_image_url)

        image_path = None
        if form.profile_image.data:
            image_path = save_profile_image(form.profile_image.data, current_user.id)

        conn = get_db_connection()
        cursor = conn.cursor()
        hashed_password = current_user.password_hash
        if form.new_password.data:
            hashed_password = generate_password_hash(form.new_password.data)

        cursor.execute(
            "UPDATE users SET username = ?, full_name = ?, phone = ?, address = ?, email = ?, password_hash = ?, profile_image_path = COALESCE(?, profile_image_path) WHERE id = ?",
            (
                form.username.data.strip(),
                form.full_name.data.strip(),
                form.phone.data.strip(),
                form.address.data.strip(),
                registered_email,
                hashed_password,
                image_path,
                current_user.id
            )
        )

        if current_user.role == 'donor':
            org_type = form.organization_type.data or ''
            if org_type == 'Other' and form.organization_type_other.data:
                org_type = f"Other ({form.organization_type_other.data.strip()})"
            cursor.execute(
                "INSERT OR REPLACE INTO donors (user_id, company_name, contact_name, phone, address, organization_type) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET company_name = excluded.company_name, contact_name = excluded.contact_name, phone = excluded.phone, address = excluded.address, organization_type = excluded.organization_type",
                (current_user.id, form.company_name.data.strip(), form.full_name.data.strip(), form.phone.data.strip(), form.address.data.strip(), org_type)
            )
        elif current_user.role == 'ngo':
            org_type = form.organization_type.data or ''
            if org_type == 'Other' and form.organization_type_other.data:
                org_type = f"Other ({form.organization_type_other.data.strip()})"
            cursor.execute(
                "INSERT OR REPLACE INTO ngos (user_id, ngo_name, registration_number, contact_person, phone, address, organization_type) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET ngo_name = excluded.ngo_name, registration_number = excluded.registration_number, contact_person = excluded.contact_person, phone = excluded.phone, address = excluded.address, organization_type = excluded.organization_type",
                (current_user.id, form.company_name.data.strip(), form.registration_number.data.strip(), form.full_name.data.strip(), form.phone.data.strip(), form.address.data.strip(), org_type)
            )
        elif current_user.role == 'volunteer':
            cursor.execute(
                "INSERT OR REPLACE INTO volunteers (user_id, name, phone, address, transport_type) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, phone = excluded.phone, address = excluded.address, transport_type = excluded.transport_type",
                (current_user.id, form.company_name.data.strip(), form.phone.data.strip(), form.address.data.strip(), form.transport_type.data or '')
            )
        elif current_user.role == 'admin':
            cursor.execute(
                "INSERT OR REPLACE INTO admins (user_id, name, phone) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, phone = excluded.phone",
                (current_user.id, form.full_name.data.strip(), form.phone.data.strip())
            )

        conn.commit()
        conn.close()

        flash('Your profile has been updated successfully.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html', form=form, profile_data=profile_data, profile_completion=profile_completion, profile_image_url=profile_image_url)


@app.route('/api/profile/send-email-otp', methods=['POST'])
@login_required
def api_profile_send_email_otp():
    data = request.get_json() or {}
    requested_email = normalize_email(data.get('email'))
    registered_email = get_registered_email(current_user.id)
    if not registered_email:
        return jsonify({'success': False, 'error': 'Your account does not have a registered email address.'}), 400
    if requested_email != registered_email:
        return jsonify({'success': False, 'message': 'You can only verify your registered email address.', 'error': 'You can only verify your registered email address.'}), 400

    try:
        otp_id, otp = create_profile_email_otp(current_user.id, registered_email)
        if not send_email_code(registered_email, otp, username=current_user.username):
            conn = get_db_connection()
            conn.execute("UPDATE email_otps SET used = 1 WHERE id = ?", (otp_id,))
            conn.commit()
            conn.close()
            return jsonify({'success': False, 'error': 'Unable to send the verification email. Please try again.'}), 503
    except Exception:
        app.logger.exception('Profile email OTP send failed')
        return jsonify({'success': False, 'error': 'Unable to send the verification email. Check the SMTP configuration and try again.'}), 503
    return jsonify({'success': True, 'message': 'OTP sent successfully.'})


@app.route('/api/profile/verify-email-otp', methods=['POST'])
@login_required
def api_profile_verify_email_otp():
    data = request.get_json() or {}
    requested_email = normalize_email(data.get('email'))
    code = normalize_otp_code(data.get('code') or data.get('otp_code') or data.get('verification_code'))
    registered_email = get_registered_email(current_user.id)
    if requested_email != registered_email:
        return jsonify({'success': False, 'message': 'You can only verify your registered email address.', 'error': 'You can only verify your registered email address.'}), 400
    if not code:
        return jsonify({'success': False, 'error': 'Email and OTP are required.'})
    try:
        verification_result = verify_profile_email_otp(current_user.id, registered_email, code)
    except Exception:
        app.logger.exception('Profile email OTP verification failed')
        return jsonify({'success': False, 'error': 'Unable to verify the email right now. Please try again.'}), 500
    if verification_result == 'verified':
        return jsonify({'success': True, 'message': 'OTP verified successfully.'})
    if verification_result == 'expired':
        return jsonify({'success': False, 'error': 'OTP has expired. Please request a new OTP.'})
    return jsonify({'success': False, 'error': 'Invalid OTP.'})

# --- REGISTRATION ---

@app.route('/register', methods=['GET', 'POST'])
def register_unified():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_router'))
        
    if request.method == 'GET':
        return render_template('register_unified.html')
        
    import re
    # POST handling: Server-side validation
    full_name = request.form.get('full_name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    role = request.form.get('role', '').strip().lower()
    organization_type = request.form.get('organization_type', '').strip()
    organization_type_other = request.form.get('organization_type_other', '').strip()

    errors = []

    # 1. Full Name
    if not full_name:
        errors.append("Full Name is required.")
    elif len(full_name) < 3 or len(full_name) > 50:
        errors.append("Full Name must be between 3 and 50 characters.")
    elif not re.match(r'^[a-zA-Z\s]+$', full_name):
        errors.append("Full Name can only contain letters and spaces.")

    # 2. Email Address
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not email:
        errors.append("Email Address is required.")
    elif len(email) > 100:
        errors.append("Email Address cannot exceed 100 characters.")
    elif not re.match(email_regex, email):
        errors.append("Invalid email address format.")

    # 3. Phone Number
    if not phone:
        errors.append("Phone Number is required.")
    elif not re.match(r'^[6789][0-9]{9}$', phone):
        errors.append("Phone number must be exactly 10 digits and start with 6, 7, 8, or 9.")

    # 4. Password
    if not password:
        errors.append("Password is required.")
    elif len(password) < 8 or len(password) > 20:
        errors.append("Password must be between 8 and 20 characters.")
    elif ' ' in password:
        errors.append("Password must not contain any spaces.")
    else:
        # Check components
        if not any(c.isupper() for c in password):
            errors.append("Password must contain at least one uppercase letter.")
        if not any(c.islower() for c in password):
            errors.append("Password must contain at least one lowercase letter.")
        if not any(c.isdigit() for c in password):
            errors.append("Password must contain at least one numeric digit.")
        if not any(c in '@#$%&!?_-' for c in password):
            errors.append("Password must contain at least one special character (@, #, $, %, &, !, ?, _, -).")

    # 5. Confirm Password
    if not confirm_password:
        errors.append("Confirm Password is required.")
    elif password != confirm_password:
        errors.append("Passwords do not match.")

    # 6. Role Selection
    if not role:
        errors.append("Role selection is required.")
    elif role not in ['donor', 'ngo', 'volunteer']:
        errors.append("Invalid role selected.")

    if role == 'ngo':
        if not organization_type:
            errors.append("Organization Type is required for NGO registration.")
        elif organization_type == 'Other' and not organization_type_other.strip():
            errors.append("Please specify the other organization type.")

    # If a verification code was submitted with the form, attempt to verify it here
    normalized_email = normalize_email(email)
    verification_code = normalize_otp_code(request.form.get('verification_code') or request.form.get('otp_code') or request.form.get('code'))
    if not is_registration_otp_verified(normalized_email):
        if verification_code:
            if verify_otp('register_email', normalized_email, verification_code):
                session['registration_email_verified'] = normalized_email
                session.modified = True
            else:
                errors.append('Invalid or expired verification code. Please resend and verify.')
        else:
            errors.append('Please verify your email OTP before creating an account.')

    if errors:
        for err in errors:
            flash(err, 'error')
        return render_template('register_unified.html'), 400

    conn = get_db_connection()
    try:
        existing_user = User.find_by_email_and_role(email, role)
        if existing_user:
            flash("This email address is already registered for the selected role. Please use a different email or sign in to that role account.", "error")
            conn.close()
            return render_template('register_unified.html'), 400

        # Check phone duplicate for new accounts
        if is_phone_registered(phone, role):
            flash("This phone number is already registered.", "error")
            conn.close()
            return render_template('register_unified.html'), 400

        # Create unified user
        user_id = User.create_unified(
            full_name=full_name,
            email=email,
            password=password,
            role=role,
            phone=phone,
            status='active' if role == 'donor' or role == 'volunteer' else 'pending'
        )

        if user_id:
            # Fetch the stored username so we can show it to the user
            stored_user = User.get(user_id)
            stored_username = stored_user.username if stored_user else 'unknown'
            
            # Create sub-profile with fallback defaults so the model is fully valid and matches DB schemas
            if role == 'donor':
                conn.execute(
                    "INSERT INTO donors (user_id, company_name, contact_name, phone, address, latitude, longitude, organization_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, f"{full_name}'s Business", full_name, phone, "Not Provided", 12.9716, 77.5946, "Individual")
                )
                conn.commit()
                flash(f'Donor Registration successful! Your username is: {stored_username}. Please log in.', 'success')
            elif role == 'ngo':
                org_type = organization_type
                if org_type == 'Other' and organization_type_other:
                    org_type = f"Other ({organization_type_other.strip()})"
                if not org_type:
                    org_type = 'NGO'
                conn.execute(
                    "INSERT INTO ngos (user_id, ngo_name, registration_number, contact_person, phone, address, latitude, longitude, organization_type, is_verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, f"{full_name}'s Foundation", f"PENDING-{phone}", full_name, phone, "Not Provided", 12.9716, 77.5946, org_type, 0)
                )
                conn.commit()
                # Notify admins
                admins = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
                for admin in admins:
                    Notification.create(
                        admin['id'],
                        f"New NGO registration pending verification: {full_name}'s Foundation",
                        title='New NGO Registration',
                        event_key=f"registration:ngo:{user_id}:admin:{admin['id']}"
                    )
                flash(f'NGO registration successful! Your username is: {stored_username}. Your account is pending admin verification.', 'warning')
            elif role == 'volunteer':
                conn.execute(
                    "INSERT INTO volunteers (user_id, name, phone, address, latitude, longitude, is_approved, transport_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, full_name, phone, "Not Provided", 12.9716, 77.5946, 1, "Two-Wheeler")
                )
                conn.commit()
                flash(f'Volunteer registration successful! Your username is: {stored_username}. You can now log in.', 'success')
            
            conn.close()
            return redirect(url_for('login'))
        else:
            flash('The account could not be created. The database rejected the registration attempt. Please try again in a moment.', 'error')
            conn.close()
            return render_template('register_unified.html'), 500
    except Exception as e:
        flash(f"Database error occurred: {str(e)}", "error")
        if conn:
            conn.close()
        return render_template('register_unified.html'), 500

@app.route('/register/donor', methods=['GET', 'POST'])
def register_donor():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_router'))
    form = DonorRegisterForm()
    if form.validate_on_submit():
        # Verify email OTP if provided or required
        normalized_email = normalize_email(form.email.data)
        verification_code = normalize_otp_code(request.form.get('verification_code') or request.form.get('otp_code') or request.form.get('code'))
        if not is_registration_otp_verified(normalized_email):
            if verification_code:
                if verify_otp('register_email', normalized_email, verification_code):
                    session['registration_email_verified'] = normalized_email
                    session.modified = True
                else:
                    flash('Invalid or expired verification code. Please resend and verify.', 'danger')
                    return render_template('register_donor.html', form=form), 400
            else:
                flash('Please verify your email OTP before creating an account.', 'danger')
                return render_template('register_donor.html', form=form), 400
        existing_user = User.find_by_email_and_role(form.email.data, 'donor')
        if existing_user:
            flash('This email address is already registered for the selected role. Please use a different email or sign in to that role account.', 'danger')
            return render_template('register_donor.html', form=form), 400

        try:
            user_id = User.create(
                username=form.username.data,
                email=form.email.data,
                password=form.password.data,
                role='donor',
                status='active'
            )
        except Exception as exc:
            flash(f"Registration failed: {exc}", 'danger')
            return render_template('register_donor.html', form=form), 400
        if user_id:
            # Fetch the stored username so we can show it to the user
            stored_user = User.get(user_id)
            stored_username = stored_user.username if stored_user else form.username.data
            
            # Parse fallback lat/long if not provided
            lat = float(form.latitude.data) if form.latitude.data else 12.9716
            lng = float(form.longitude.data) if form.longitude.data else 77.5946
            
            org_type = form.organization_type.data
            if org_type == 'Other' and form.organization_type_other.data:
                org_type = f"Other ({form.organization_type_other.data.strip()})"

            Donor.create(
                user_id=user_id,
                company_name=form.company_name.data,
                contact_name=form.contact_name.data,
                phone=form.phone.data,
                address=form.address.data,
                latitude=lat,
                longitude=lng,
                organization_type=org_type
            )
            flash(f'Donor Registration successful! Your username is: {stored_username}. Please log in.', 'success')
            return redirect(url_for('login'))
        else:
            flash('The account could not be created. The database rejected the registration attempt. Please try again in a moment.', 'danger')
    return render_template('register_donor.html', form=form)

@app.route('/register/ngo', methods=['GET', 'POST'])
def register_ngo():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_router'))
    form = NGORegisterForm()
    if form.validate_on_submit():
        # Verify email OTP if provided or required
        normalized_email = normalize_email(form.email.data)
        verification_code = normalize_otp_code(request.form.get('verification_code') or request.form.get('otp_code') or request.form.get('code'))
        if not is_registration_otp_verified(normalized_email):
            if verification_code:
                if verify_otp('register_email', normalized_email, verification_code):
                    session['registration_email_verified'] = normalized_email
                    session.modified = True
                else:
                    flash('Invalid or expired verification code. Please resend and verify.', 'danger')
                    return render_template('register_ngo.html', form=form), 400
            else:
                flash('Please verify your email OTP before creating an account.', 'danger')
                return render_template('register_ngo.html', form=form), 400
        existing_user = User.find_by_email_and_role(form.email.data, 'ngo')
        if existing_user:
            flash('This email address is already registered for the selected role. Please use a different email or sign in to that role account.', 'danger')
            return render_template('register_ngo.html', form=form), 400

        try:
            user_id = User.create(
                username=form.username.data,
                email=form.email.data,
                password=form.password.data,
                role='ngo',
                status='pending'  # NGOs are pending verification by admin
            )
        except Exception as exc:
            flash(f"Registration failed: {exc}", 'danger')
            return render_template('register_ngo.html', form=form), 400
        if user_id:
            stored_user = User.get(user_id)
            stored_username = stored_user.username if stored_user else form.username.data
            
            lat = float(form.latitude.data) if form.latitude.data else 12.9716
            lng = float(form.longitude.data) if form.longitude.data else 77.5946
            organization_type = form.organization_type.data
            if organization_type == 'Other' and form.organization_type_other.data:
                organization_type = f"Other ({form.organization_type_other.data.strip()})"
            
            NGO.create(
                user_id=user_id,
                ngo_name=form.ngo_name.data,
                registration_number=form.registration_number.data,
                contact_person=form.contact_person.data,
                phone=form.phone.data,
                address=form.address.data,
                latitude=lat,
                longitude=lng,
                is_verified=0,
                organization_type=organization_type
            )
            # Notify admins of a new NGO registration
            conn = get_db_connection()
            admins = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
            for admin in admins:
                Notification.create(admin['id'], f"New NGO registration pending verification: {form.ngo_name.data}")
            conn.close()
            
            flash(f'NGO Registration complete! Your username is: {stored_username}. It is currently pending admin verification.', 'warning')
            return redirect(url_for('login'))
        else:
            flash('An error occurred. Please try again.', 'danger')
    return render_template('register_ngo.html', form=form)

@app.route('/register/volunteer', methods=['GET', 'POST'])
def register_volunteer():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard_router'))
    form = VolunteerRegisterForm()
    if form.validate_on_submit():
        # Verify email OTP if provided or required
        normalized_email = normalize_email(form.email.data)
        verification_code = normalize_otp_code(request.form.get('verification_code') or request.form.get('otp_code') or request.form.get('code'))
        if not is_registration_otp_verified(normalized_email):
            if verification_code:
                if verify_otp('register_email', normalized_email, verification_code):
                    session['registration_email_verified'] = normalized_email
                    session.modified = True
                else:
                    flash('Invalid or expired verification code. Please resend and verify.', 'danger')
                    return render_template('register_volunteer.html', form=form), 400
            else:
                flash('Please verify your email OTP before creating an account.', 'danger')
                return render_template('register_volunteer.html', form=form), 400
        existing_user = User.find_by_email_and_role(form.email.data, 'volunteer')
        if existing_user:
            flash('This email address is already registered for the selected role. Please use a different email or sign in to that role account.', 'danger')
            return render_template('register_volunteer.html', form=form), 400

        try:
            user_id = User.create(
                username=form.username.data,
                email=form.email.data,
                password=form.password.data,
                role='volunteer',
                status='pending'  # Volunteers need approval
            )
        except Exception as exc:
            flash(f"Registration failed: {exc}", 'danger')
            return render_template('register_volunteer.html', form=form), 400
        if user_id:
            stored_user = User.get(user_id)
            stored_username = stored_user.username if stored_user else form.username.data
            
            lat = float(form.latitude.data) if form.latitude.data else 12.9716
            lng = float(form.longitude.data) if form.longitude.data else 77.5946
            
            Volunteer.create(
                user_id=user_id,
                name=form.name.data,
                phone=form.phone.data,
                address=form.address.data,
                latitude=lat,
                longitude=lng,
                is_approved=0,
                transport_type=form.transport_type.data
            )
            # Notify admins
            conn = get_db_connection()
            admins = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
            for admin in admins:
                Notification.create(admin['id'], f"New Volunteer registration pending approval: {form.name.data}")
            conn.close()
            
            flash(f'Volunteer registration submitted! Your username is: {stored_username}. Pending administrator approval.', 'warning')
            return redirect(url_for('login'))
        else:
            flash('The account could not be created. The database rejected the registration attempt. Please try again in a moment.', 'danger')
    return render_template('register_volunteer.html', form=form)


# --- ADMIN MODULE ---

def _get_admin_contribution_stats(conn):
    """Return participation and contribution metrics for the admin report."""
    stats = {
        'active_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 1").fetchone()[0],
        'active_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 1").fetchone()[0],
        'contributing_donors': conn.execute(
            "SELECT COUNT(DISTINCT donor_id) FROM ("
            "SELECT donor_id FROM food_donations "
            "UNION SELECT donor_id FROM clothes_donations "
            "UNION SELECT donor_id FROM book_donations "
            "UNION SELECT donor_id FROM money_donations"
            ")"
        ).fetchone()[0],
        'contributing_ngos': conn.execute(
            "SELECT COUNT(DISTINCT ngo_id) FROM ("
            "SELECT ngo_id FROM food_distribution WHERE ngo_id IS NOT NULL AND status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM donation_assignments WHERE status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM pickup_assignments WHERE status != 'Cancelled'"
            ")"
        ).fetchone()[0],
        'contributing_volunteers': conn.execute(
            "SELECT COUNT(DISTINCT volunteer_id) FROM ("
            "SELECT volunteer_id FROM food_distribution WHERE volunteer_id IS NOT NULL AND status != 'Cancelled' "
            "UNION ALL SELECT volunteer_id FROM donation_assignments WHERE status != 'Cancelled' "
            "UNION ALL SELECT volunteer_id FROM pickup_assignments WHERE status != 'Cancelled'"
            ")"
        ).fetchone()[0],
        'ngo_assignments': conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT ngo_id FROM food_distribution WHERE ngo_id IS NOT NULL AND status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM donation_assignments WHERE status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM pickup_assignments WHERE status != 'Cancelled'"
            ")"
        ).fetchone()[0],
        'volunteer_assignments': conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT volunteer_id FROM food_distribution WHERE volunteer_id IS NOT NULL AND status != 'Cancelled' "
            "UNION ALL SELECT volunteer_id FROM donation_assignments WHERE status != 'Cancelled' "
            "UNION ALL SELECT volunteer_id FROM pickup_assignments WHERE status != 'Cancelled'"
            ")"
        ).fetchone()[0],
        'total_food_donations': conn.execute("SELECT COUNT(*) FROM food_donations").fetchone()[0],
        'total_clothes_donations': conn.execute("SELECT COUNT(*) FROM clothes_donations").fetchone()[0],
        'total_book_donations': conn.execute("SELECT COUNT(*) FROM book_donations").fetchone()[0],
        'total_money_donations': conn.execute("SELECT COUNT(*) FROM money_donations").fetchone()[0],
        'total_money_received': conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM money_donations WHERE status IN ('Approved', 'Paid')"
        ).fetchone()[0],
    }

    donor_records = conn.execute(
        "SELECT d.company_name AS donor_name, COUNT(*) AS contribution_count FROM ("
        "SELECT donor_id FROM food_donations "
        "UNION ALL SELECT donor_id FROM clothes_donations "
        "UNION ALL SELECT donor_id FROM book_donations "
        "UNION ALL SELECT donor_id FROM money_donations"
        ") contributions JOIN donors d ON d.id = contributions.donor_id "
        "GROUP BY d.id, d.company_name ORDER BY d.company_name"
    ).fetchall()
    verified_ngo_records = conn.execute(
        "SELECT ngo_name, registration_number FROM ngos "
        "WHERE is_verified = 1 ORDER BY ngo_name"
    ).fetchall()
    contributing_ngo_records = conn.execute(
        "SELECT n.ngo_name, COUNT(*) AS assignment_count FROM ("
        "SELECT ngo_id FROM food_distribution WHERE ngo_id IS NOT NULL AND status != 'Cancelled' "
        "UNION ALL SELECT ngo_id FROM donation_assignments WHERE status != 'Cancelled' "
        "UNION ALL SELECT ngo_id FROM pickup_assignments WHERE status != 'Cancelled'"
        ") assignments JOIN ngos n ON n.id = assignments.ngo_id "
        "GROUP BY assignments.ngo_id, n.ngo_name ORDER BY assignment_count DESC"
    ).fetchall()
    approved_volunteer_records = conn.execute(
        "SELECT name, transport_type FROM volunteers "
        "WHERE is_approved = 1 ORDER BY name"
    ).fetchall()
    contributing_volunteer_records = conn.execute(
        "SELECT v.name, COUNT(*) AS assignment_count FROM ("
        "SELECT volunteer_id FROM food_distribution WHERE volunteer_id IS NOT NULL AND status != 'Cancelled' "
        "UNION ALL SELECT volunteer_id FROM donation_assignments WHERE status != 'Cancelled' "
        "UNION ALL SELECT volunteer_id FROM pickup_assignments WHERE status != 'Cancelled'"
        ") assignments JOIN volunteers v ON v.id = assignments.volunteer_id "
        "GROUP BY assignments.volunteer_id, v.name ORDER BY assignment_count DESC"
    ).fetchall()
    assignment_records = conn.execute(
        "SELECT * FROM ("
        "SELECT 'Food distribution' AS assignment_type, fd.id AS assignment_id, "
        "COALESCE(n.ngo_name, 'Unassigned') AS ngo_name, COALESCE(v.name, 'Unassigned') AS volunteer_name, fd.status "
        "FROM food_distribution fd LEFT JOIN ngos n ON n.id = fd.ngo_id LEFT JOIN volunteers v ON v.id = fd.volunteer_id "
        "UNION ALL SELECT da.donation_type || ' donation', da.id, n.ngo_name, v.name, da.status "
        "FROM donation_assignments da JOIN ngos n ON n.id = da.ngo_id JOIN volunteers v ON v.id = da.volunteer_id "
        "UNION ALL SELECT 'Food pickup', pa.id, n.ngo_name, v.name, pa.status "
        "FROM pickup_assignments pa JOIN ngos n ON n.id = pa.ngo_id JOIN volunteers v ON v.id = pa.volunteer_id"
        ") WHERE status != 'Cancelled' ORDER BY assignment_id DESC"
    ).fetchall()

    stats['details'] = {
        'donors': {
            'title': 'Contributing Donors',
            'description': 'Donors with at least one food, clothes, book, or money contribution.',
            'records': [
                {
                    'label': f"{row['donor_name']} - {row['contribution_count']} contributions",
                    'status': 'Contributing'
                }
                for row in donor_records
            ]
        },
        'verified-ngos': {
            'title': 'Verified NGOs',
            'description': 'NGO partners currently verified by the administrator.',
            'records': [
                {
                    'label': f"{row['ngo_name']} - Registration {row['registration_number']}",
                    'status': 'Verified'
                }
                for row in verified_ngo_records
            ]
        },
        'contributing-ngos': {
            'title': 'Contributing NGOs',
            'description': 'NGO partners linked to at least one active assignment.',
            'records': [
                {
                    'label': f"{row['ngo_name']} - {row['assignment_count']} assignments",
                    'status': 'Contributing'
                }
                for row in contributing_ngo_records
            ]
        },
        'approved-volunteers': {
            'title': 'Approved Volunteers',
            'description': 'Volunteers approved to support pickup, inspection, and delivery workflows.',
            'records': [
                {
                    'label': f"{row['name']} - {row['transport_type'] or 'Transport not specified'}",
                    'status': 'Approved'
                }
                for row in approved_volunteer_records
            ]
        },
        'contributing-volunteers': {
            'title': 'Contributing Volunteers',
            'description': 'Volunteers with at least one non-cancelled assignment.',
            'records': [
                {
                    'label': f"{row['name']} - {row['assignment_count']} assignments",
                    'status': 'Contributing'
                }
                for row in contributing_volunteer_records
            ]
        },
        'ngo-assignments': {
            'title': 'NGO Assignments',
            'description': 'Non-cancelled assignments linked to NGO partners.',
            'records': [
                {
                    'label': f"{row['assignment_type']} #{row['assignment_id']} - {row['ngo_name']}",
                    'status': row['status']
                }
                for row in assignment_records
            ]
        },
        'volunteer-assignments': {
            'title': 'Volunteer Assignments',
            'description': 'Non-cancelled assignments linked to volunteers.',
            'records': [
                {
                    'label': f"{row['assignment_type']} #{row['assignment_id']} - {row['volunteer_name']}",
                    'status': row['status']
                }
                for row in assignment_records
            ]
        }
    }
    return stats


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))
        
    conn = get_db_connection()
    ensure_food_distribution_columns()

    stats = {
        'total_users': conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'pending_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 0").fetchone()[0],
        'pending_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 0").fetchone()[0],
        'pending_orphanages': conn.execute("SELECT COUNT(*) FROM orphanages WHERE is_approved = 0").fetchone()[0],
        'active_donations': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status IN ('Pending', 'Accepted', 'Volunteer Assigned')").fetchone()[0],
        'completed_distributions': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE status = 'Completed'").fetchone()[0],
        'total_donations': conn.execute("SELECT COUNT(*) FROM food_donations").fetchone()[0],
        'total_clothes_donations': conn.execute("SELECT COUNT(*) FROM clothes_donations").fetchone()[0],
        'total_book_donations': conn.execute("SELECT COUNT(*) FROM book_donations").fetchone()[0],
        'total_money_donations': conn.execute("SELECT COUNT(*) FROM money_donations").fetchone()[0],
        'meals_rescued': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status = 'Delivered'").fetchone()[0],
        'active_donors': conn.execute("SELECT COUNT(*) FROM donors WHERE is_approved = 1").fetchone()[0],
        'active_ngos': conn.execute("SELECT COUNT(*) FROM ngos WHERE is_verified = 1").fetchone()[0],
        'contributing_ngos': conn.execute(
            "SELECT COUNT(DISTINCT ngo_id) FROM ("
            "SELECT ngo_id FROM food_distribution WHERE ngo_id IS NOT NULL AND status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM donation_assignments WHERE status != 'Cancelled' "
            "UNION ALL SELECT ngo_id FROM pickup_assignments WHERE status != 'Cancelled'"
            ")"
        ).fetchone()[0],
        'active_orphanages': conn.execute("SELECT COUNT(*) FROM orphanages WHERE is_approved = 1").fetchone()[0],
        'active_volunteers': conn.execute("SELECT COUNT(*) FROM volunteers WHERE is_approved = 1").fetchone()[0],
        'emergency_distributions': conn.execute("SELECT COUNT(*) FROM food_distribution WHERE status IN ('Delivered', 'Completed')").fetchone()[0],
        'total_money_received': conn.execute("SELECT COALESCE(SUM(amount), 0) FROM money_donations WHERE status IN ('Approved', 'Paid')").fetchone()[0],
    }

    total_distribution_records = conn.execute("SELECT COUNT(*) FROM food_distribution WHERE status NOT IN ('Cancelled')").fetchone()[0]
    successful_distributions = conn.execute("SELECT COUNT(*) FROM food_distribution WHERE status IN ('Delivered', 'Completed')").fetchone()[0]
    stats['delivery_success_rate'] = round((successful_distributions / total_distribution_records) * 100, 1) if total_distribution_records else 0

    # Monthly donation and delivery trends
    monthly_donations = conn.execute(
        "SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS cnt FROM ("
        "SELECT created_at FROM food_donations "
        "UNION ALL SELECT created_at FROM clothes_donations "
        "UNION ALL SELECT created_at FROM book_donations "
        "UNION ALL SELECT created_at FROM money_donations"
        ") WHERE created_at IS NOT NULL GROUP BY month ORDER BY month"
    ).fetchall()

    delivery_columns = [row[1] for row in conn.execute("PRAGMA table_info(food_distribution)").fetchall()]
    if 'delivered_at' in delivery_columns:
        monthly_deliveries = conn.execute(
            "SELECT strftime('%Y-%m', delivered_at) AS month, COUNT(*) AS cnt FROM food_distribution "
            "WHERE delivered_at IS NOT NULL GROUP BY month ORDER BY month"
        ).fetchall()
    else:
        monthly_deliveries = []

    trend_months = sorted({row['month'] for row in monthly_donations if row['month']} | {row['month'] for row in monthly_deliveries if row['month']})
    donations_by_month = [next((row['cnt'] for row in monthly_donations if row['month'] == month), 0) for month in trend_months]
    deliveries_by_month = [next((row['cnt'] for row in monthly_deliveries if row['month'] == month), 0) for month in trend_months]

    # Recent users list
    recent_users = conn.execute(
        "SELECT u.*, COALESCE(a.name, d.company_name, n.ngo_name, v.name) as display_name FROM users u "
        "LEFT JOIN admins a ON u.id = a.user_id "
        "LEFT JOIN donors d ON u.id = d.user_id "
        "LEFT JOIN ngos n ON u.id = n.user_id "
        "LEFT JOIN volunteers v ON u.id = v.user_id "
        "ORDER BY u.created_at DESC LIMIT 5"
    ).fetchall()
    
    # Donation type distribution for the admin analytics chart
    donation_type_counts = conn.execute(
        "SELECT donation_type, COUNT(*) AS cnt FROM ("
        "SELECT 'Food' AS donation_type FROM food_donations "
        "UNION ALL SELECT 'Clothes' FROM clothes_donations "
        "UNION ALL SELECT 'Books' FROM book_donations "
        "UNION ALL SELECT 'Money' FROM money_donations"
        ") GROUP BY donation_type ORDER BY donation_type"
    ).fetchall()
    donation_types_json = {row['donation_type']: row['cnt'] for row in donation_type_counts}
    
    # Status Distribution
    status_counts = conn.execute("SELECT status, COUNT(*) as cnt FROM food_donations GROUP BY status").fetchall()
    status_json = {s['status']: s['cnt'] for s in status_counts}

    # Active emergency alerts count and latest entries
    emergency_counts = conn.execute("SELECT COUNT(*) FROM emergencies WHERE is_active = 1").fetchone()[0]
    emergency_entries = conn.execute(
        "SELECT e.*, u.username as created_by_username FROM emergencies e "
        "LEFT JOIN users u ON e.created_by = u.id "
        "ORDER BY e.is_active DESC, e.created_at DESC LIMIT 5"
    ).fetchall()

    # Recent platform feedback
    feedback_entries = conn.execute(
        "SELECT pf.id, pf.rating, pf.comments, pf.created_at, u.username "
        "FROM platform_feedback pf "
        "LEFT JOIN users u ON pf.user_id = u.id "
        "ORDER BY pf.created_at DESC LIMIT 10"
    ).fetchall()

    orphanages = conn.execute(
        "SELECT * FROM orphanages ORDER BY is_approved ASC, created_at DESC LIMIT 8"
    ).fetchall()

    conn.close()
    return render_template(
        'admin_dashboard.html',
        stats=stats,
        recent_users=recent_users,
        donation_types_json=donation_types_json,
        status_json=status_json,
        emergency_count=emergency_counts,
        emergency_entries=emergency_entries,
        feedback_entries=feedback_entries,
        orphanages=orphanages,
        successful_distributions=successful_distributions,
        trend_months=[m for m in trend_months],
        donations_by_month=[int(v) for v in donations_by_month],
        deliveries_by_month=[int(v) for v in deliveries_by_month]
    )


@app.route('/admin/statistics')
@login_required
def admin_statistics():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    stats = _get_admin_contribution_stats(conn)
    conn.close()
    return render_template('admin_statistics.html', stats=stats)


@app.route('/admin/platform-feedback')
@login_required
def admin_platform_feedback():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    feedback_entries = conn.execute(
        "SELECT pf.id, pf.rating, pf.comments, pf.created_at, u.username "
        "FROM platform_feedback pf "
        "LEFT JOIN users u ON pf.user_id = u.id "
        "ORDER BY pf.created_at DESC"
    ).fetchall()
    # Compute totals/average using SQL to handle NULLs and avoid Python-level division errors
    stats_row = conn.execute("SELECT COUNT(*) AS total, AVG(rating) AS avg_rating FROM platform_feedback").fetchone()
    total_feedback = stats_row['total'] or 0
    avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    conn.close()

    return render_template(
        'platform_feedback.html',
        feedback_entries=feedback_entries,
        total_feedback=total_feedback,
        avg_rating=avg_rating
    )

@app.route('/admin/contact-messages')
@login_required
def admin_contact_messages():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    messages = conn.execute(
        "SELECT cm.*, u.username AS user_username FROM contact_messages cm "
        "LEFT JOIN users u ON cm.user_id = u.id "
        "ORDER BY cm.created_at DESC"
    ).fetchall()
    unread_count = conn.execute("SELECT COUNT(*) AS count FROM contact_messages WHERE status = 'Unread'").fetchone()['count']
    conn.close()

    return render_template('admin_contact_messages.html', messages=messages, unread_count=unread_count)

@app.route('/admin/contact-message/<int:message_id>/reply', methods=['POST'])
@login_required
def admin_reply_contact_message(message_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    reply_text = (request.form.get('reply_message') or '').strip()
    if not reply_text:
        flash('Reply message cannot be empty.', 'danger')
        return redirect(url_for('admin_contact_messages'))
    if len(reply_text) > 20000:
        flash('Reply message is too long.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    conn = get_db_connection()
    try:
        inbox_message = conn.execute(
            "SELECT id, name, email, subject FROM contact_messages WHERE id = ?",
            (message_id,)
        ).fetchone()
    finally:
        conn.close()

    if not inbox_message:
        flash('Inbox message not found.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    try:
        recipient = validate_email(inbox_message['email'], check_deliverability=False).normalized
    except (EmailNotValidError, TypeError):
        flash('The sender email address is invalid. Reply could not be sent.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    original_subject = ' '.join((inbox_message['subject'] or '').split())
    reply_subject = original_subject if original_subject.lower().startswith('re:') else f'Re: {original_subject}'
    sender_name = (inbox_message['name'] or '').strip() or 'there'
    email_body = (
        f'Hello {sender_name},\n\n'
        'Thank you for contacting FeedForward.\n\n'
        f'{reply_text}\n\n'
        'Regards,\n'
        'FeedForward Team'
    )

    email_sent = send_notification_email(recipient, reply_subject, email_body)
    if not email_sent:
        app.logger.error('SMTP reply delivery failed for contact message id %s', message_id)
        try:
            MessageReply.create(
                contact_message_id=message_id,
                admin_user_id=current_user.id,
                recipient_email=recipient,
                subject=reply_subject,
                reply_message=reply_text,
                status='failed',
                error='SMTP delivery failed'
            )
        except Exception:
            app.logger.exception('Unable to record failed reply for contact message id %s', message_id)
        flash('Unable to send reply. Please try again.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    try:
        MessageReply.create(
            contact_message_id=message_id,
            admin_user_id=current_user.id,
            recipient_email=recipient,
            subject=reply_subject,
            reply_message=reply_text,
            status='sent'
        )
    except Exception:
        app.logger.exception('Email sent but reply record could not be saved for contact message id %s', message_id)
        flash('Email was sent, but its reply record could not be saved. Check the sender account before retrying.', 'warning')
        return redirect(url_for('admin_contact_messages'))

    flash('Reply Sent Successfully', 'success')
    return redirect(url_for('admin_contact_messages'))

@app.route('/admin/contact-message/<int:message_id>/toggle-read', methods=['POST'])
@login_required
def toggle_contact_message(message_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    message = conn.execute("SELECT status FROM contact_messages WHERE id = ?", (message_id,)).fetchone()
    if message:
        new_status = 'Read' if message['status'] != 'Read' else 'Unread'
        conn.execute("UPDATE contact_messages SET status = ? WHERE id = ?", (new_status, message_id))
        conn.commit()
        flash(f"Message marked as {new_status}.", 'success')
    conn.close()
    return redirect(url_for('admin_contact_messages'))

@app.route('/admin/contact-message/<int:message_id>/delete', methods=['POST'])
@login_required
def delete_contact_message(message_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    conn.execute("DELETE FROM contact_messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()
    flash('Contact message deleted successfully.', 'success')
    return redirect(url_for('admin_contact_messages'))

@app.route('/admin/user-feedback')
@login_required
def admin_user_feedback():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    conn = get_db_connection()
    feedback_entries = conn.execute(
        "SELECT f.id, f.rating, f.comments, f.created_at, "
        "reviewer.username AS reviewer_name, target.username AS target_name "
        "FROM feedback f "
        "LEFT JOIN users reviewer ON f.user_id = reviewer.id "
        "LEFT JOIN food_donations fd ON f.donation_id = fd.id "
        "LEFT JOIN users target ON fd.donor_id = target.id "
        "ORDER BY f.created_at DESC"
    ).fetchall()
    # Use SQL aggregate to compute average safely
    stats_row = conn.execute("SELECT COUNT(*) AS total, AVG(rating) AS avg_rating FROM feedback").fetchone()
    avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    conn.close()

    return render_template(
        'user_feedback.html',
        feedback_entries=feedback_entries,
        avg_rating=avg_rating
    )

@app.route('/admin/emergencies', methods=['GET', 'POST'])
@login_required
def admin_emergencies():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    form = EmergencyForm()
    conn = get_db_connection()
    emergencies = conn.execute(
        "SELECT e.*, u.username as created_by_username FROM emergencies e "
        "LEFT JOIN users u ON e.created_by = u.id "
        "ORDER BY e.is_active DESC, e.created_at DESC"
    ).fetchall()

    if form.validate_on_submit():
        Emergency.create(
            title=form.title.data,
            disaster_type=form.disaster_type.data,
            location=form.location.data,
            severity=form.severity.data,
            details=form.details.data,
            start_date=form.start_date.data,
            end_date=form.end_date.data or None,
            is_active=int(form.is_active.data),
            created_by=current_user.id
        )
        conn.close()

        conn_notify = get_db_connection()
        recipients = conn_notify.execute(
            "SELECT id FROM users WHERE role IN ('donor', 'ngo', 'volunteer')"
        ).fetchall()
        for recipient in recipients:
            Notification.create(recipient['id'], f"Emergency Alert: {form.title.data} in {form.location.data}. Severity: {form.severity.data}.")
        conn_notify.close()

        flash('Emergency alert published successfully.', 'success')
        return redirect(url_for('admin_emergencies'))

    conn.close()
    return render_template('admin_emergencies.html', form=form, emergencies=emergencies)

@app.route('/admin/emergencies/<int:emergency_id>/toggle', methods=['POST'])
@login_required
def admin_toggle_emergency(emergency_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    # Fetch emergency details so we can include them in notifications
    conn = get_db_connection()
    emergency = conn.execute("SELECT * FROM emergencies WHERE id = ?", (emergency_id,)).fetchone()
    conn.close()

    if not emergency:
        flash('Emergency not found.', 'danger')
        return redirect(url_for('admin_emergencies'))

    prev_state = emergency['is_active']
    # Toggle the active state
    Emergency.toggle_active(emergency_id)
    new_state = 0 if prev_state else 1

    # Notify all users about activation/deactivation
    conn_notify = get_db_connection()
    recipients = conn_notify.execute("SELECT id FROM users").fetchall()
    for recipient in recipients:
        if new_state:
            Notification.create(recipient['id'], f"Emergency Alert Activated: {emergency['title']} in {emergency['location']}. Severity: {emergency['severity']}.")
        else:
            Notification.create(recipient['id'], f"Emergency Alert Deactivated: {emergency['title']} in {emergency['location']}.")
    conn_notify.close()

    flash('Emergency alert status updated and users notified.', 'success')
    return redirect(url_for('admin_emergencies'))

@app.route('/admin/emergencies/<int:emergency_id>/delete', methods=['POST'])
@login_required
def admin_delete_emergency(emergency_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    Emergency.delete(emergency_id)
    flash('Emergency alert removed.', 'success')
    return redirect(url_for('admin_emergencies'))


@app.route('/admin/emergency-mode-requests')
@login_required
def admin_emergency_mode_requests():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    requests = EmergencyModeRequest.get_pending()
    return render_template('admin_emergency_mode_requests.html', requests=requests)


@app.route('/admin/emergency-mode-requests/<int:request_id>/review', methods=['POST'])
@login_required
def admin_review_emergency_mode_request(request_id):
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    action = request.form.get('action')
    if action not in ['approve', 'reject']:
        flash('Invalid request action.', 'danger')
        return redirect(url_for('admin_emergency_mode_requests'))

    status = 'Approved' if action == 'approve' else 'Rejected'
    EmergencyModeRequest.update_status(request_id, status, current_user.id)
    if action == 'approve':
        set_app_setting('enable_emergency_mode', '1')
        Notification.create(current_user.id, 'Emergency Disaster Mode has been enabled.')
    flash(f'Emergency mode request {status.lower()}.', 'success')
    return redirect(url_for('admin_emergency_mode_requests'))


@app.route('/request-emergency-mode', methods=['POST'])
@login_required
def request_emergency_mode():
    form = EmergencyModeRequestForm()
    if not form.validate_on_submit():
        flash('Please provide a reason for the emergency mode request.', 'danger')
        return redirect(url_for('home'))

    if EmergencyModeRequest.get_pending_by_user(current_user.id):
        flash('You already have a pending emergency mode request.', 'warning')
        return redirect(url_for('home'))

    EmergencyModeRequest.create(current_user.id, form.note.data)
    Notification.create(current_user.id, 'Your emergency mode request has been submitted and is pending admin approval.')
    conn = get_db_connection()
    admins = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
    conn.close()
    for admin in admins:
        Notification.create(admin['id'], f"Emergency mode request submitted by {current_user.username}.")

    flash('Your emergency mode request has been submitted for admin review.', 'success')
    return redirect(url_for('home'))


@app.route('/report-emergency', methods=['GET', 'POST'])
@login_required
def report_emergency():
    form = EmergencyForm()
    # Ensure the user-submitted report defaults to inactive and has a start date
    if request.method == 'GET':
        if not form.start_date.data:
            form.start_date.data = datetime.date.today().isoformat()
        form.is_active.data = '0'
    if form.validate_on_submit():
        # Create emergency record but mark inactive until admin approves
        Emergency.create(
            title=form.title.data,
            disaster_type=form.disaster_type.data,
            location=form.location.data,
            severity=form.severity.data,
            details=form.details.data,
            start_date=form.start_date.data,
            end_date=form.end_date.data or None,
            is_active=0,
            created_by=current_user.id
        )

        # Notify admins for review
        conn = get_db_connection()
        admins = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
        conn.close()
        for admin in admins:
            Notification.create(admin['id'], f"New emergency reported by {current_user.username}: {form.title.data}")

        flash('Thank you — your report has been submitted for admin review.', 'success')
        return redirect(url_for('home'))

    return render_template('report_emergency.html', form=form)

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    users = conn.execute(
        "SELECT u.*, COALESCE(a.name, d.company_name, n.ngo_name, v.name) as display_name, "
        "COALESCE(a.phone, d.phone, n.phone, v.phone) as phone, "
        "n.id as ngo_id, n.organization_type as organization_type, v.id as volunteer_id "
        "FROM users u "
        "LEFT JOIN admins a ON u.id = a.user_id "
        "LEFT JOIN donors d ON u.id = d.user_id "
        "LEFT JOIN ngos n ON u.id = n.user_id "
        "LEFT JOIN volunteers v ON u.id = v.user_id "
        "ORDER BY u.created_at DESC"
    ).fetchall()
    conn.close()
    return render_template('admin_users.html', users=users)

@app.route('/admin/verify-ngo/<int:ngo_id>', methods=['POST'])
@login_required
def verify_ngo(ngo_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    conn = get_db_connection()
    ngo = conn.execute("SELECT * FROM ngos WHERE id = ?", (ngo_id,)).fetchone()
    if ngo:
        conn.execute("UPDATE ngos SET is_verified = 1 WHERE id = ?", (ngo_id,))
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (ngo['user_id'],))
        conn.commit()
        Notification.create(ngo['user_id'], "Congratulations! Your beneficiary organization profile has been verified by the administrator. You can now accept donations.")
        flash('Beneficiary organization verified successfully!', 'success')
    conn.close()
    return redirect(url_for('admin_users'))

@app.route('/admin/approve-volunteer/<int:volunteer_id>', methods=['POST'])
@login_required
def approve_volunteer(volunteer_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    conn = get_db_connection()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE id = ?", (volunteer_id,)).fetchone()
    if volunteer:
        conn.execute("UPDATE volunteers SET is_approved = 1 WHERE id = ?", (volunteer_id,))
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (volunteer['user_id'],))
        conn.commit()
        Notification.create(volunteer['user_id'], "Congratulations! Your Volunteer registration has been approved. You can now start picking up and delivering donations.")
        flash('Volunteer approved successfully!', 'success')
    conn.close()
    return redirect(url_for('admin_users'))

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    if user_id == current_user.id:
        flash('You cannot delete your own admin account.', 'danger')
        return redirect(url_for('admin_users'))
        
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash('User deleted successfully.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/donations')
@login_required
def admin_donations():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    donations = conn.execute(
           """SELECT * FROM (
              SELECT d.id, d.food_name AS item_name, 'Food' AS donation_type,
                  d.category, d.veg_non_veg AS details, d.quantity, d.status,
                  d.created_at, dn.company_name,
                  COALESCE(n.ngo_name, 'Unassigned') AS assigned_ngo,
                  COALESCE(v.name, 'Unassigned') AS assigned_volunteer
              FROM food_donations d
              JOIN donors dn ON d.donor_id = dn.id
              LEFT JOIN food_distribution fd ON d.id = fd.donation_id
              LEFT JOIN ngos n ON fd.ngo_id = n.id
              LEFT JOIN volunteers v ON fd.volunteer_id = v.id
              UNION ALL
              SELECT c.id, c.item_type, 'Clothes', c.category, c.condition, c.quantity,
                  c.status, c.created_at, dn.company_name,
                  COALESCE(n.ngo_name, 'Unassigned'), COALESCE(v.name, 'Unassigned')
              FROM clothes_donations c
              JOIN donors dn ON c.donor_id = dn.id
              LEFT JOIN donation_assignments da ON da.donation_type = 'clothes' AND da.donation_id = c.id
              LEFT JOIN ngos n ON da.ngo_id = n.id
              LEFT JOIN volunteers v ON da.volunteer_id = v.id
              UNION ALL
              SELECT b.id, b.title, 'Books', b.category, b.condition, b.quantity,
                  b.status, b.created_at, dn.company_name,
                  COALESCE(n.ngo_name, 'Unassigned'), COALESCE(v.name, 'Unassigned')
              FROM book_donations b
              JOIN donors dn ON b.donor_id = dn.id
              LEFT JOIN donation_assignments da ON da.donation_type = 'books' AND da.donation_id = b.id
              LEFT JOIN ngos n ON da.ngo_id = n.id
              LEFT JOIN volunteers v ON da.volunteer_id = v.id
              UNION ALL
              SELECT m.id, COALESCE(m.purpose, 'Money donation'), 'Money',
                  'Money', m.payment_method, CAST(m.amount AS TEXT), m.status,
                  m.created_at, dn.company_name, 'Unassigned', 'Unassigned'
              FROM money_donations m
              JOIN donors dn ON m.donor_id = dn.id
            ) ORDER BY created_at DESC"""
    ).fetchall()
    conn.close()
    return render_template('admin_donations.html', donations=donations)

@app.route('/admin/orphanages')
@login_required
def admin_orphanages():
    if current_user.role != 'admin':
        return redirect(url_for('index'))

    conn = get_db_connection()
    orphanages = conn.execute(
        """SELECT 
            n.id, 
            n.ngo_name as name, 
            n.contact_person, 
            n.phone, 
            u.email, 
            n.address, 
            n.is_verified as is_approved,
            u.created_at,
            u.id as created_by
        FROM ngos n
        JOIN users u ON n.user_id = u.id
        WHERE u.role = 'ngo'
        ORDER BY n.is_verified ASC, u.created_at DESC"""
    ).fetchall()
    conn.close()
    return render_template('admin_orphanages.html', orphanages=orphanages)

@app.route('/admin/approve-orphanage/<int:orphanage_id>', methods=['POST'])
@login_required
def approve_orphanage(orphanage_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    conn = get_db_connection()
    orphanage = conn.execute(
        "SELECT n.id, n.ngo_name as name, n.user_id FROM ngos n WHERE n.id = ?", 
        (orphanage_id,)
    ).fetchone()
    if orphanage:
        conn.execute("UPDATE ngos SET is_verified = 1 WHERE id = ?", (orphanage_id,))
        conn.commit()
        if orphanage['user_id']:
            Notification.create(
                orphanage['user_id'], 
                f"Your NGO/Orphanage '{orphanage['name']}' has been approved and can now receive donations."
            )
        flash('Orphanage approved successfully!', 'success')
    conn.close()
    return redirect(url_for('admin_orphanages'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    form = SystemSettingsForm()
    if request.method == 'GET':
        form.system_name.data = get_app_setting('system_name', 'FeedForward Donation System')
        form.contact_email.data = get_app_setting('contact_email', 'contact@feedforward.com')
        form.enable_notifications.data = get_app_setting('enable_notifications', '1')
        form.enable_emergency_mode.data = get_app_setting('enable_emergency_mode', '1')
        
    if form.validate_on_submit():
        set_app_setting('system_name', form.system_name.data)
        set_app_setting('contact_email', form.contact_email.data)
        set_app_setting('enable_notifications', form.enable_notifications.data)
        # detect previous value to notify users on deactivation
        prev_emergency = get_app_setting('enable_emergency_mode', '1')
        set_app_setting('enable_emergency_mode', form.enable_emergency_mode.data)
        # If admin turned emergency mode OFF, notify all users
        if prev_emergency == '1' and form.enable_emergency_mode.data == '0':
            conn_notify = get_db_connection()
            recipients = conn_notify.execute("SELECT id FROM users").fetchall()
            conn_notify.close()
            for recipient in recipients:
                Notification.create(recipient['id'], 'Emergency Disaster Mode has been disabled by an administrator.')
        flash('System settings saved successfully.', 'success')
        return redirect(url_for('admin_dashboard'))
        
    return render_template('admin_settings.html', form=form)


@app.route('/admin/test-smtp')
@login_required
def admin_test_smtp():
    if current_user.role != 'admin':
        flash('Unauthorized Access.', 'danger')
        return redirect(url_for('index'))

    recipient = app.config.get('MAIL_USERNAME') or current_user.email
    if not recipient:
        flash('No SMTP recipient configured.', 'danger')
        return redirect(url_for('admin_settings'))

    otp = '000000'
    success = send_email_code(recipient, otp, username='SMTP Test')
    if success:
        flash(f'SMTP test email sent to {recipient}. Check your inbox.', 'success')
    else:
        flash('SMTP test failed. Check server logs for details.', 'danger')
    return redirect(url_for('admin_settings'))


# --- DONOR MODULE ---

@app.route('/donor/dashboard')
@login_required
def donor_dashboard():
    if current_user.role != 'donor':
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    
    # Donation statistics
    food_count = conn.execute("SELECT COUNT(*) FROM food_donations WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
    clothes_count = conn.execute("SELECT COUNT(*) FROM clothes_donations WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
    books_count = conn.execute("SELECT COUNT(*) FROM book_donations WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
    money_count = conn.execute("SELECT COUNT(*) FROM money_donations WHERE donor_id = ?", (donor['id'],)).fetchone()[0]
    stats = {
        'food_count': food_count,
        'clothes_count': clothes_count,
        'books_count': books_count,
        'money_count': money_count,
        'total_donated': food_count + clothes_count + books_count + money_count,
        'delivered': sum([
            conn.execute("SELECT COUNT(*) FROM food_donations WHERE donor_id = ? AND status IN ('Delivered', 'Completed')", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM clothes_donations WHERE donor_id = ? AND status IN ('Delivered', 'Completed')", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM book_donations WHERE donor_id = ? AND status IN ('Delivered', 'Completed')", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM money_donations WHERE donor_id = ? AND status IN ('Paid', 'Completed')", (donor['id'],)).fetchone()[0],
        ]),
        'pending': sum([
            conn.execute("SELECT COUNT(*) FROM food_donations WHERE donor_id = ? AND status = 'Pending'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM clothes_donations WHERE donor_id = ? AND status = 'Pending'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM book_donations WHERE donor_id = ? AND status = 'Pending'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM money_donations WHERE donor_id = ? AND status = 'Pending'", (donor['id'],)).fetchone()[0],
        ]),
        'cancelled': sum([
            conn.execute("SELECT COUNT(*) FROM food_donations WHERE donor_id = ? AND status = 'Cancelled'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM clothes_donations WHERE donor_id = ? AND status = 'Cancelled'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM book_donations WHERE donor_id = ? AND status = 'Cancelled'", (donor['id'],)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM money_donations WHERE donor_id = ? AND status = 'Cancelled'", (donor['id'],)).fetchone()[0],
        ]),
    }
    
    top_donors = conn.execute(
        """SELECT dn.company_name, COUNT(d.id) AS delivered_count
           FROM food_donations d
           JOIN donors dn ON d.donor_id = dn.id
           WHERE d.status = 'Delivered'
           GROUP BY dn.id
           ORDER BY delivered_count DESC
           LIMIT 10"""
    ).fetchall()
    top_volunteers = conn.execute(
        """SELECT v.name, COUNT(pa.id) AS deliveries
           FROM pickup_assignments pa
           JOIN volunteers v ON pa.volunteer_id = v.id
           WHERE pa.status = 'Delivered'
           GROUP BY v.id
           ORDER BY deliveries DESC
           LIMIT 10"""
    ).fetchall()
    
    # List of recent donations with volunteer assignment info
    recent_donations = conn.execute(
        """SELECT fd.id, fd.donor_id, fd.food_name, fd.category, fd.veg_non_veg,
              fd.quantity, fd.expiry_time, fd.pickup_time, fd.address, fd.image_path, fd.status,
                  fd.inspection_status, fd.inspection_result, fd.inspection_completed_at,
                  fd.created_at, v.name as assigned_volunteer_name
           FROM food_donations fd
           LEFT JOIN pickup_assignments pa ON fd.id = pa.donation_id
           LEFT JOIN volunteers v ON pa.volunteer_id = v.id
           WHERE fd.donor_id = ?
           ORDER BY fd.created_at DESC
           LIMIT 5""",
        (donor['id'],)
    ).fetchall()
    # Recent certificates for this donor
    certificates = conn.execute(
        """SELECT 'food' AS donation_type, c.id, c.donation_id, c.volunteer_id, c.donor_id, c.ngo_id,
                  c.certificate_path, c.issued_at, d.food_name, d.created_at as donated_at
           FROM certificates c
           JOIN food_donations d ON c.donation_id = d.id
           WHERE c.donor_id = ?
           UNION ALL
           SELECT dc.donation_type, dc.id, dc.donation_id, dc.volunteer_id, dc.donor_id, dc.ngo_id,
                  dc.certificate_path, dc.issued_at,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END AS food_name,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.created_at ELSE bd.created_at END AS donated_at
           FROM donation_certificates dc
           LEFT JOIN clothes_donations cd ON dc.donation_type = 'clothes' AND dc.donation_id = cd.id
           LEFT JOIN book_donations bd ON dc.donation_type = 'books' AND dc.donation_id = bd.id
           WHERE dc.donor_id = ?
           ORDER BY issued_at DESC
           LIMIT 5""",
        (donor['id'], donor['id'])
    ).fetchall()

    feedback_entries = conn.execute(
        """SELECT f.id, f.rating, f.comments, f.created_at, f.food_rating, f.delivery_rating,
                   u.username AS reviewer_name, d.food_name
           FROM feedback f
           JOIN food_donations d ON f.donation_id = d.id
           LEFT JOIN users u ON f.user_id = u.id
           WHERE d.donor_id = ?
           ORDER BY f.created_at DESC
           LIMIT 5""",
        (donor['id'],)
    ).fetchall()
    stats_row = conn.execute(
        "SELECT COUNT(*) AS total, AVG(f.rating) AS avg_rating "
        "FROM feedback f "
        "JOIN food_donations d ON f.donation_id = d.id "
        "WHERE d.donor_id = ?",
        (donor['id'],)
    ).fetchone()
    feedback_avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    
    conn.close()
    return render_template(
        'donor_dashboard.html',
        donor=donor,
        stats=stats,
        recent_donations=recent_donations,
        top_donors=top_donors,
        top_volunteers=top_volunteers,
        certificates=certificates,
        feedback_entries=feedback_entries,
        feedback_avg_rating=feedback_avg_rating,
        feedback_count=len(feedback_entries)
    )

@app.route('/donor/feedback')
@login_required
def donor_feedback():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT id, company_name FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        return redirect(url_for('index'))

    feedback_entries = conn.execute(
        """SELECT f.id, f.rating, f.comments, f.created_at, f.food_rating, f.delivery_rating,
                   u.username AS reviewer_name, d.food_name
           FROM feedback f
           JOIN food_donations d ON f.donation_id = d.id
           LEFT JOIN users u ON f.user_id = u.id
           WHERE d.donor_id = ?
           ORDER BY f.created_at DESC""",
        (donor['id'],)
    ).fetchall()
    stats_row = conn.execute(
        "SELECT COUNT(*) AS total, AVG(f.rating) AS avg_rating "
        "FROM feedback f "
        "JOIN food_donations d ON f.donation_id = d.id "
        "WHERE d.donor_id = ?",
        (donor['id'],)
    ).fetchone()
    feedback_avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    conn.close()

    return render_template('donor_feedback.html', donor=donor, feedback_entries=feedback_entries, feedback_avg_rating=feedback_avg_rating)

@app.route('/donor/certificates')
@login_required
def donor_certificates():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT id, company_name FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        return redirect(url_for('index'))

    certs = conn.execute(
        """SELECT 'food' AS donation_type, c.donation_id, c.certificate_path, c.issued_at, d.food_name, d.created_at as donated_at
           FROM certificates c
           JOIN food_donations d ON c.donation_id = d.id
           WHERE c.donor_id = ?
           UNION ALL
           SELECT dc.donation_type, dc.donation_id, dc.certificate_path, dc.issued_at,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.item_type
                       WHEN dc.donation_type = 'books' THEN bd.title
                       ELSE COALESCE(CAST(md.amount AS TEXT), 'Money donation') END,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.created_at
                       WHEN dc.donation_type = 'books' THEN bd.created_at
                       ELSE md.created_at END
           FROM donation_certificates dc
           LEFT JOIN clothes_donations cd ON dc.donation_type = 'clothes' AND dc.donation_id = cd.id
           LEFT JOIN book_donations bd ON dc.donation_type = 'books' AND dc.donation_id = bd.id
           LEFT JOIN money_donations md ON dc.donation_type = 'money' AND dc.donation_id = md.id
           WHERE dc.donor_id = ?
           ORDER BY issued_at DESC""",
        (donor['id'], donor['id'])
    ).fetchall()
    conn.close()
    return render_template('donor_certificates.html', donor=donor, certificates=certs)

@app.route('/donor/volunteers')
@login_required
def donor_volunteers():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT id, company_name FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        return redirect(url_for('index'))

        # Show all approved volunteers, with assignments scoped to this donor's donations.
    volunteers = conn.execute(
        """SELECT DISTINCT v.id, v.name, v.phone, v.address,
              COUNT(DISTINCT CASE WHEN fd.id IS NOT NULL THEN pa.id END) as total_assignments,
              SUM(CASE WHEN fd.id IS NOT NULL AND pa.status = 'Delivered' THEN 1 ELSE 0 END) as deliveries_completed
            FROM volunteers v
            LEFT JOIN pickup_assignments pa ON v.id = pa.volunteer_id
            LEFT JOIN food_donations fd ON fd.id = pa.donation_id AND fd.donor_id = ?
            WHERE v.is_approved = 1
           GROUP BY v.id
           ORDER BY deliveries_completed DESC""",
        (donor['id'],)
    ).fetchall()
    
    conn.close()
    return render_template('donor_volunteers.html', donor=donor, volunteers=volunteers)

@app.route('/donor/verified-ngos')
@login_required
def donor_verified_ngos():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    search_query = request.args.get('q', '').strip()
    conn = get_db_connection()
    query = """
        SELECT n.id, n.ngo_name, n.address, n.organization_type, n.phone
        FROM ngos n
        JOIN users u ON u.id = n.user_id
        WHERE n.is_verified = 1 AND u.id IS NOT NULL
    """
    params = []
    if search_query:
        query += " AND (LOWER(n.ngo_name) LIKE ? OR LOWER(n.address) LIKE ? OR LOWER(COALESCE(n.organization_type, '')) LIKE ?)"
        search_pattern = f"%{search_query.lower()}%"
        params.extend([search_pattern, search_pattern, search_pattern])
    query += " ORDER BY LOWER(n.ngo_name)"
    verified_ngos = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('donor_verified_ngos.html', verified_ngos=verified_ngos, search_query=search_query)

@app.route('/donor/verified-ngos/<int:ngo_id>')
@login_required
def donor_verified_ngo_details(ngo_id):
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    ngo = conn.execute(
        """SELECT n.id, n.ngo_name, n.address, n.organization_type, n.phone
           FROM ngos n
           JOIN users u ON u.id = n.user_id
           WHERE n.id = ? AND n.is_verified = 1 AND u.id IS NOT NULL""",
        (ngo_id,)
    ).fetchone()
    conn.close()
    if not ngo:
        return redirect(url_for('donor_verified_ngos'))
    return render_template('donor_verified_ngo_details.html', ngo=ngo)

@app.route('/donor/donate', methods=['GET'])
@login_required
def donor_donate():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    donation_choices = [
        {
            'type': 'food',
            'title': 'Food Donation',
            'icon': 'fa-utensils',
            'description': 'Share surplus food and help fight hunger.',
            'url': url_for('donor_donate_food')
        },
        {
            'type': 'clothes',
            'title': 'Clothes Donation',
            'icon': 'fa-shirt',
            'description': 'Donate clothes and bring warmth and dignity.',
            'url': url_for('donor_donate_clothes')
        },
        {
            'type': 'books',
            'title': 'Book Donation',
            'icon': 'fa-book-open',
            'description': 'Share books and spread the light of education.',
            'url': url_for('donor_donate_books')
        },
        {
            'type': 'money',
            'title': 'Money Donation<br>for Orphans',
            'icon': 'fa-hand-holding-heart',
            'description': 'Your contribution can secure their future and dreams.',
            'url': url_for('donor_donate_money')
        }
    ]
    return render_template('donor_donate_select.html', donation_choices=donation_choices)


@app.route('/donor/donate/food', methods=['GET', 'POST'])
@login_required
def donor_donate_food():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()

    form = DonationForm()
    if request.method == 'GET':
        form.address.data = donor['address']

    if form.validate_on_submit():
        selected_category = form.category.data
        if selected_category == 'Other':
            custom_category = (form.other_category.data or '').strip()
            if not custom_category:
                flash('Please specify the food category.', 'danger')
                conn.close()
                return render_template(
                    'donor_donate.html',
                    form=form,
                    edit_mode=False,
                    form_action=url_for('donor_donate_food'),
                    submit_label='Submit Food Donation'
                ), 400
            selected_category = custom_category

        image_file = request.files.get('food_image')
        image_relative_path = None
        if not image_file or not getattr(image_file, 'filename', None):
            flash('Please upload a food photo before submitting the donation.', 'danger')
            conn.close()
            return render_template(
                'donor_donate.html',
                form=form,
                edit_mode=False,
                form_action=url_for('donor_donate_food'),
                submit_label='Submit Food Donation'
            ), 400

        filename = secure_filename(f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{image_file.filename}")
        filepath = os.path.join(Config.UPLOAD_FOLDER, 'food', filename)
        image_file.save(filepath)
        image_relative_path = f"uploads/food/{filename}"

        lat = float(form.latitude.data) if form.latitude.data is not None else None
        lng = float(form.longitude.data) if form.longitude.data is not None else None

        try:
            donation_id = FoodDonation.create(
                donor_id=donor['id'],
                food_name=form.food_name.data,
                category=selected_category,
                veg_non_veg=form.veg_non_veg.data,
                quantity=form.quantity.data,
                expiry_time=form.expiry_time.data,
                pickup_time=form.pickup_time.data,
                address=form.address.data,
                latitude=lat,
                longitude=lng,
                image_path=image_relative_path
            )
        except Exception:
            app.logger.exception('Unable to save food donation')
            conn.close()
            flash('Unable to add donation. Please try again.', 'danger')
            return render_template(
                'donor_donate.html',
                form=form,
                edit_mode=False,
                form_action=url_for('donor_donate_food'),
                submit_label='Submit Food Donation'
            ), 500

        qr_path = generate_qr_code(donation_id, form.food_name.data)
        conn.execute("UPDATE food_donations SET qr_code_path = ? WHERE id = ?", (qr_path, donation_id))
        conn.commit()

        ngos = conn.execute("SELECT user_id FROM ngos WHERE is_verified = 1").fetchall()
        for ngo in ngos:
            Notification.create(ngo['user_id'], f"New surplus food donation available near you: {form.food_name.data} ({form.quantity.data})")

        flash('food', 'donation_success')
        conn.close()
        return redirect(url_for('donor_donate_food'))

    conn.close()
    return render_template(
        'donor_donate.html',
        form=form,
        edit_mode=False,
        form_action=url_for('donor_donate_food'),
        submit_label='Submit Food Donation',
        donation_type='Food',
        donation_type_label='Food',
        donation_path='food'
    )

@app.route('/donor/donation/<int:donation_id>/edit', methods=['GET', 'POST'])
@login_required
def donor_edit_donation(donation_id):
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT * FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute(
        "SELECT * FROM food_donations WHERE id = ? AND donor_id = ?",
        (donation_id, donor['id'])
    ).fetchone()

    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('donor_dashboard'))

    if donation['status'] in ['Delivered', 'Cancelled']:
        conn.close()
        flash('Cannot edit a completed or cancelled donation.', 'danger')
        return redirect(url_for('donor_dashboard'))

    form = DonationForm()
    if request.method == 'GET':
        form.food_name.data = donation['food_name']
        form.category.data = donation['category']
        form.veg_non_veg.data = donation['veg_non_veg']
        form.quantity.data = donation['quantity']
        form.expiry_time.data = donation['expiry_time']
        form.pickup_time.data = donation['pickup_time']
        form.address.data = donation['address']
        form.latitude.data = donation['latitude']
        form.longitude.data = donation['longitude']

    if form.validate_on_submit():
        image_file = request.files.get('food_image')
        image_relative_path = donation['image_path']
        if image_file and getattr(image_file, 'filename', None):
            filename = secure_filename(f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{image_file.filename}")
            filepath = os.path.join(Config.UPLOAD_FOLDER, 'food', filename)
            image_file.save(filepath)
            image_relative_path = f"uploads/food/{filename}"

        latitude = float(form.latitude.data) if form.latitude.data else donor['latitude']
        longitude = float(form.longitude.data) if form.longitude.data else donor['longitude']

        conn.execute(
            """UPDATE food_donations
               SET food_name = ?, category = ?, veg_non_veg = ?, quantity = ?, expiry_time = ?, pickup_time = ?,
                   address = ?, latitude = ?, longitude = ?, image_path = ?
               WHERE id = ?""",
            (
                form.food_name.data,
                form.category.data,
                form.veg_non_veg.data,
                form.quantity.data,
                form.expiry_time.data,
                form.pickup_time.data,
                form.address.data,
                latitude,
                longitude,
                image_relative_path,
                donation_id
            )
        )

        if not donation['qr_code_path']:
            qr_path = generate_qr_code(donation_id, form.food_name.data)
            conn.execute("UPDATE food_donations SET qr_code_path = ? WHERE id = ?", (qr_path, donation_id))

        conn.commit()
        conn.close()

        flash('Your donation has been updated successfully.', 'success')
        return redirect(url_for('donor_dashboard'))

    conn.close()
    return render_template(
        'donor_donate.html',
        form=form,
        edit_mode=True,
        donation=donation,
        form_action=url_for('donor_edit_donation', donation_id=donation_id),
        submit_label='Update donation'
    )

@app.route('/donor/donation/<int:donation_id>/delete', methods=['POST'])
@login_required
def donor_delete_donation(donation_id):
    if current_user.role != 'donor':
        return jsonify({'error': 'Unauthorized'}), 403

    conn = get_db_connection()
    donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone()

    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('donor_history'))

    if donation['status'] not in ['Pending', 'Cancelled']:
        conn.close()
        flash('Only pending or cancelled donations can be deleted. Active deliveries cannot be removed.', 'warning')
        return redirect(url_for('donor_history'))

    # Remove associated distribution records before deleting the donation.
    conn.execute("DELETE FROM food_distribution WHERE donation_id = ?", (donation_id,))
    conn.execute("DELETE FROM pickup_assignments WHERE donation_id = ?", (donation_id,))
    conn.execute("DELETE FROM food_donations WHERE id = ?", (donation_id,))
    conn.commit()
    conn.close()

    flash('Donation deleted successfully.', 'success')
    return redirect(url_for('donor_history'))


@app.route('/donor/donation/<string:donation_type>/<int:donation_id>/delete', methods=['POST'])
@login_required
def donor_delete_typed_donation(donation_type, donation_id):
    if current_user.role != 'donor' or donation_type not in ('clothes', 'books', 'money'):
        return jsonify({'error': 'Unauthorized'}), 403
    table = {'clothes': 'clothes_donations', 'books': 'book_donations', 'money': 'money_donations'}[donation_type]
    conn = get_db_connection()
    donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone()
    if donation and donation['status'] in ('Pending', 'Cancelled'):
        conn.execute("DELETE FROM donation_distribution WHERE donation_type = ? AND donation_id = ?", (donation_type, donation_id))
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (donation_id,))
        conn.commit()
        flash('Donation deleted successfully.', 'success')
    else:
        flash('Only pending or cancelled donations can be deleted.', 'warning')
    conn.close()
    return redirect(url_for('donor_history'))

@app.route('/donor/cancel/<int:donation_id>', methods=['POST'])
@login_required
def donor_cancel_donation(donation_id):
    if current_user.role != 'donor':
        return jsonify({'error': 'Unauthorized'}), 403
        
    conn = get_db_connection()
    donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone()
    
    if donation:
        if donation['status'] in ['Delivered', 'Cancelled']:
            flash('Cannot cancel a completed or already cancelled donation.', 'danger')
        else:
            conn.execute("UPDATE food_donations SET status = 'Cancelled' WHERE id = ?", (donation_id,))
            conn.execute("UPDATE food_distribution SET status = 'Cancelled' WHERE donation_id = ?", (donation_id,))
            conn.commit()
            
            # Notify NGO/Volunteer if they were assigned
            dist = conn.execute("SELECT ngo_id, volunteer_id FROM food_distribution WHERE donation_id = ?", (donation_id,)).fetchone()
            if dist:
                if dist['ngo_id']:
                    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (dist['ngo_id'],)).fetchone()
                    if ngo_user:
                        Notification.create(ngo_user['user_id'], f"The donor has cancelled donation ID {donation_id} ({donation['food_name']}).")
                if dist['volunteer_id']:
                    vol_user = conn.execute("SELECT user_id FROM volunteers WHERE id = ?", (dist['volunteer_id'],)).fetchone()
                    if vol_user:
                        Notification.create(vol_user['user_id'], f"The assigned donation ID {donation_id} has been cancelled by the donor.")
                        
            flash('Donation cancelled successfully.', 'warning')
    conn.close()
    return redirect(url_for('donor_dashboard'))


@app.route('/donor/cancel/<string:donation_type>/<int:donation_id>', methods=['POST'])
@login_required
def donor_cancel_typed_donation(donation_type, donation_id):
    if current_user.role != 'donor' or donation_type not in ('clothes', 'books', 'money'):
        return jsonify({'error': 'Unauthorized'}), 403
    table = {'clothes': 'clothes_donations', 'books': 'book_donations', 'money': 'money_donations'}[donation_type]
    conn = get_db_connection()
    donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND donor_id = ?", (donation_id, donor['id'])).fetchone()
    if donation and donation['status'] not in ('Delivered', 'Cancelled'):
        conn.execute(f"UPDATE {table} SET status = 'Cancelled' WHERE id = ?", (donation_id,))
        conn.execute("UPDATE donation_distribution SET status = 'Cancelled' WHERE donation_type = ? AND donation_id = ?", (donation_type, donation_id))
        _record_lifecycle_activity(conn, donation_type, donation_id, 'Donation Cancelled', 'Cancelled')
        conn.commit()
        flash('Donation cancelled successfully.', 'warning')
    else:
        flash('Donation not found or already completed.', 'warning')
    conn.close()
    return redirect(url_for('donor_history'))

@app.route('/donor/history')
@login_required
def donor_history():
    if current_user.role != 'donor':
        return redirect(url_for('index'))

    conn = get_db_connection()
    donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
    if not donor:
        conn.close()
        return redirect(url_for('donor_dashboard'))

    food_donations = conn.execute(
        """SELECT d.id, 'Food' AS donation_type, d.food_name AS title, d.category, d.quantity, d.status,
               d.inspection_status, d.inspection_result, d.inspected_by, d.inspection_completed_at,
               d.created_at, d.address, d.pickup_time, d.expiry_time, d.image_path,
                   fd.delivery_proof_path, f.rating, f.food_rating, f.delivery_rating, f.comments,
                   c.certificate_path, NULL AS orphanage_name, NULL AS amount, NULL AS purpose, NULL AS payment_method, NULL AS razorpay_payment_id
           FROM food_donations d
           LEFT JOIN food_distribution fd ON fd.donation_id = d.id
           LEFT JOIN feedback f ON f.donation_id = d.id
           LEFT JOIN certificates c ON c.donation_id = d.id
           WHERE d.donor_id = ?""",
        (donor['id'],)
    ).fetchall()

    clothes_donations = conn.execute(
        """SELECT d.id, 'Clothes' AS donation_type, d.item_type AS title, d.category, d.quantity, d.status,
               NULL AS inspection_status, NULL AS inspection_result, NULL AS inspected_by, NULL AS inspection_completed_at,
                   d.created_at, d.address, d.pickup_time, NULL AS expiry_time, d.image_path,
                   NULL AS delivery_proof_path, NULL AS rating, NULL AS food_rating, NULL AS delivery_rating,
                   NULL AS comments, NULL AS certificate_path,
                   NULL AS orphanage_name, NULL AS amount, NULL AS purpose, NULL AS payment_method, NULL AS razorpay_payment_id
           FROM clothes_donations d
           WHERE d.donor_id = ?""",
        (donor['id'],)
    ).fetchall()

    books_donations = conn.execute(
        """SELECT d.id, 'Books' AS donation_type, d.title AS title, d.category, d.quantity, d.status,
               NULL AS inspection_status, NULL AS inspection_result, NULL AS inspected_by, NULL AS inspection_completed_at,
                   d.created_at, d.address, d.pickup_time, NULL AS expiry_time, d.image_path,
                   NULL AS delivery_proof_path, NULL AS rating, NULL AS food_rating, NULL AS delivery_rating,
                   NULL AS comments, NULL AS certificate_path,
                   NULL AS orphanage_name, NULL AS amount, NULL AS purpose, NULL AS payment_method, NULL AS razorpay_payment_id
           FROM book_donations d
           WHERE d.donor_id = ?""",
        (donor['id'],)
    ).fetchall()

    money_donations = conn.execute(
        """SELECT d.id, 'Money' AS donation_type, o.name AS title, 'Orphan Support' AS category, CAST(d.amount AS TEXT) AS quantity,
                   d.status, NULL AS inspection_status, NULL AS inspection_result, NULL AS inspected_by, NULL AS inspection_completed_at,
                   d.created_at, o.address AS address, NULL AS pickup_time, NULL AS expiry_time,
                   NULL AS image_path, NULL AS delivery_proof_path, NULL AS rating, NULL AS food_rating,
                   NULL AS delivery_rating, d.note AS comments, NULL AS certificate_path,
                   o.name AS orphanage_name, d.amount AS amount, d.purpose AS purpose, d.payment_method AS payment_method,
                   d.razorpay_payment_id AS razorpay_payment_id
           FROM money_donations d
           LEFT JOIN orphanages o ON o.id = d.orphanage_id
           WHERE d.donor_id = ?""",
        (donor['id'],)
    ).fetchall()

    combined = list(food_donations) + list(clothes_donations) + list(books_donations) + list(money_donations)
    combined.sort(key=lambda item: item['created_at'], reverse=True)
    conn.close()
    return render_template('donor_history.html', donations=combined)


# --- NGO MODULE ---

@app.route('/ngo/dashboard')
@login_required
def ngo_dashboard():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    category_counts = _get_ngo_category_counts()
    return render_template('ngo_dashboard.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers, category_counts=category_counts)


def _load_ngo_dashboard_data():
    conn = get_db_connection()
    ngo = conn.execute("SELECT * FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
    if not ngo:
        conn.close()
        return None, None, None, None

    if not ngo['is_verified']:
        conn.close()
        return ngo, None, None, None

    my_assignments = conn.execute(
        """SELECT fd.*, d.food_name, d.quantity, d.address, d.expiry_time, d.image_path,
                 d.inspection_status, d.inspection_result, d.inspection_completed_at,
                 v.name as volunteer_name, fi.inspected_at AS inspection_at, fi.notes AS inspection_notes, fi.rejection_reason
           FROM food_distribution fd
           JOIN food_donations d ON fd.donation_id = d.id
           LEFT JOIN volunteers v ON fd.volunteer_id = v.id
           LEFT JOIN food_inspections fi ON d.id = fi.donation_id AND fi.volunteer_id = fd.volunteer_id
           WHERE fd.ngo_id = ? AND fd.status NOT IN ('Completed', 'Cancelled')""",
        (ngo['id'],)
    ).fetchall()

    # Fetch all types of available donations (food, clothes, books, money)
    available_donations = conn.execute(
        """
        SELECT f.id, f.donor_id, f.food_name, f.category, f.veg_non_veg, f.quantity, f.expiry_time, f.pickup_time, f.address, f.latitude, f.longitude, f.image_path, f.qr_code_path, f.status, f.inspection_status, f.inspection_result, f.created_at,
               dn.company_name, dn.phone as donor_phone, 'food' as donation_type
        FROM food_donations f
        JOIN donors dn ON f.donor_id = dn.id
        WHERE f.status = 'Pending' AND f.id NOT IN (SELECT donation_id FROM food_distribution WHERE ngo_id IS NOT NULL)
        
        UNION ALL
        
        SELECT c.id, c.donor_id, c.item_type as food_name, c.category, c.size as veg_non_veg, c.quantity, NULL as expiry_time, c.pickup_time, c.address, c.latitude, c.longitude, c.image_path, NULL as qr_code_path, c.status, NULL as inspection_status, NULL as inspection_result, c.created_at,
               dn.company_name, dn.phone as donor_phone, 'clothes' as donation_type
        FROM clothes_donations c
        JOIN donors dn ON c.donor_id = dn.id
        WHERE c.status = 'Pending'
        
        UNION ALL
        
        SELECT b.id, b.donor_id, b.title as food_name, b.category, b.condition as veg_non_veg, b.quantity, NULL as expiry_time, b.pickup_time, b.address, b.latitude, b.longitude, b.image_path, NULL as qr_code_path, b.status, NULL as inspection_status, NULL as inspection_result, b.created_at,
               dn.company_name, dn.phone as donor_phone, 'books' as donation_type
        FROM book_donations b
        JOIN donors dn ON b.donor_id = dn.id
        WHERE b.status = 'Pending'
        
        UNION ALL
        
        SELECT m.id, m.donor_id, CAST(m.amount AS TEXT) as food_name, 'money' as category, m.payment_method as veg_non_veg, m.purpose as quantity, NULL as expiry_time, NULL as pickup_time, '' as address, NULL as latitude, NULL as longitude, NULL as image_path, NULL as qr_code_path, m.status, NULL as inspection_status, NULL as inspection_result, m.created_at,
               dn.company_name, dn.phone as donor_phone, 'money' as donation_type
        FROM money_donations m
        JOIN donors dn ON m.donor_id = dn.id
        WHERE m.status IN ('Pending', 'Paid')
        
        ORDER BY created_at DESC
        """
    ).fetchall()

    volunteers = conn.execute("SELECT * FROM volunteers WHERE is_approved = 1").fetchall()
    conn.close()
    return ngo, my_assignments, available_donations, volunteers


def _get_ngo_category_counts(conn=None):
    close_conn = conn is None
    if close_conn:
        conn = get_db_connection()

    counts = {
        'food': conn.execute("SELECT COUNT(*) FROM food_donations WHERE status = 'Pending' AND id NOT IN (SELECT donation_id FROM food_distribution WHERE ngo_id IS NOT NULL)").fetchone()[0],
        'clothes': conn.execute("SELECT COUNT(*) FROM clothes_donations WHERE status = 'Pending'").fetchone()[0],
        'books': conn.execute("SELECT COUNT(*) FROM book_donations WHERE status = 'Pending'").fetchone()[0],
        'money': conn.execute("SELECT COUNT(*) FROM money_donations WHERE status IN ('Pending', 'Paid')").fetchone()[0],
    }

    if close_conn:
        conn.close()
    return counts


def _get_ngo_category_donations(category, conn=None):
    close_conn = conn is None
    if close_conn:
        conn = get_db_connection()

    if category == 'food':
        query = """
            SELECT d.id, 'Food' AS donation_type, d.food_name AS title, d.category, d.quantity,
                   d.address, d.created_at, d.expiry_time, d.image_path, d.status,
                   d.inspection_status, d.inspection_result,
                   dn.company_name AS donor_name, d.food_name AS description
            FROM food_donations d
            JOIN donors dn ON d.donor_id = dn.id
            WHERE d.status = 'Pending' AND d.id NOT IN (SELECT donation_id FROM food_distribution WHERE ngo_id IS NOT NULL)
            ORDER BY d.created_at DESC
        """
    elif category == 'clothes':
        query = """
            SELECT d.id, 'Clothes' AS donation_type, d.item_type AS title, d.category,
                   d.quantity, d.address, d.created_at, d.pickup_time, d.image_path, d.status,
                   NULL AS inspection_status, NULL AS inspection_result,
                   dn.company_name AS donor_name, d.condition AS description
            FROM clothes_donations d
            JOIN donors dn ON d.donor_id = dn.id
            WHERE d.status = 'Pending'
            ORDER BY d.created_at DESC
        """
    elif category == 'books':
        query = """
            SELECT d.id, 'Books' AS donation_type, d.title AS title, d.category,
                   d.quantity, d.address, d.created_at, d.pickup_time, d.image_path, d.status,
                   NULL AS inspection_status, NULL AS inspection_result,
                   dn.company_name AS donor_name, d.condition AS description
            FROM book_donations d
            JOIN donors dn ON d.donor_id = dn.id
            WHERE d.status = 'Pending'
            ORDER BY d.created_at DESC
        """
    elif category == 'money':
        query = """
            SELECT d.id, 'Money' AS donation_type, COALESCE(o.name, 'Support Fund') AS title,
                   'Money' AS category, CAST(d.amount AS TEXT) AS quantity, d.note AS description,
                   d.created_at, NULL AS pickup_time, NULL AS image_path, d.status,
                   dn.company_name AS donor_name, o.address AS address
            FROM money_donations d
            JOIN donors dn ON d.donor_id = dn.id
            LEFT JOIN orphanages o ON o.id = d.orphanage_id
            WHERE d.status IN ('Pending', 'Paid')
            ORDER BY d.created_at DESC
        """
    else:
        if close_conn:
            conn.close()
        return []

    donations = conn.execute(query).fetchall()
    if close_conn:
        conn.close()
    return donations


@app.route('/ngo/donations')
@login_required
def ngo_donations():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    return render_template('ngo_donations.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers)


@app.route('/ngo/donations/food')
@login_required
def ngo_food_donations():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    donations = _get_ngo_category_donations('food')
    return render_template('ngo_category_donations.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers, donations=donations, category='food', category_label='Food Donation')


@app.route('/ngo/donations/clothes')
@login_required
def ngo_clothes_donations():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    donations = _get_ngo_category_donations('clothes')
    return render_template('ngo_category_donations.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers, donations=donations, category='clothes', category_label='Clothes Donation')


@app.route('/ngo/donations/books')
@login_required
def ngo_books_donations():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    donations = _get_ngo_category_donations('books')
    return render_template('ngo_category_donations.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers, donations=donations, category='books', category_label='Books Donation')


@app.route('/ngo/donations/money')
@login_required
def ngo_money_donations():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    donations = _get_ngo_category_donations('money')
    return render_template('ngo_category_donations.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers, donations=donations, category='money', category_label='Money Donation')


@app.route('/ngo/analytics')
@login_required
def ngo_analytics():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo = current_user.ngo if hasattr(current_user, 'ngo') else None
    if not ngo:
        conn = get_db_connection()
        ngo = conn.execute("SELECT * FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
        conn.close()

    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    conn = get_db_connection()
    
    # Total donations processed
    total_donations = conn.execute(
        "SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ?", 
        (ngo['id'],)
    ).fetchone()[0]
    
    # Completed distributions
    completed_distributions = conn.execute(
        "SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ? AND status = 'Completed'", 
        (ngo['id'],)
    ).fetchone()[0]
    
    # Total food quantity distributed
    total_quantity = conn.execute(
        "SELECT COUNT(*) FROM food_distribution fd JOIN food_donations d ON fd.donation_id = d.id WHERE fd.ngo_id = ? AND fd.status IN ('Delivered', 'Completed')",
        (ngo['id'],)
    ).fetchone()[0]
    
    # Active assignments
    active_assignments = conn.execute(
        "SELECT COUNT(*) FROM food_distribution WHERE ngo_id = ? AND status NOT IN ('Completed', 'Cancelled')",
        (ngo['id'],)
    ).fetchone()[0]
    
    # Monthly distribution trend (last 6 months)
    monthly_trend = conn.execute(
        """SELECT strftime('%Y-%m', assigned_at) as month, COUNT(*) as count 
           FROM food_distribution 
           WHERE ngo_id = ? AND assigned_at IS NOT NULL
           GROUP BY strftime('%Y-%m', assigned_at)
           ORDER BY month DESC LIMIT 6""",
        (ngo['id'],)
    ).fetchall()
    
    # Distribution by status
    status_breakdown = conn.execute(
        """SELECT status, COUNT(*) as count FROM food_distribution 
           WHERE ngo_id = ? 
           GROUP BY status""",
        (ngo['id'],)
    ).fetchall()
    
    # Top donor partners
    top_donors = conn.execute(
        """SELECT d.company_name, COUNT(*) as donation_count
           FROM food_distribution fd
           JOIN food_donations fd2 ON fd.donation_id = fd2.id
           JOIN donors d ON fd2.donor_id = d.id
           WHERE fd.ngo_id = ?
           GROUP BY d.company_name
           ORDER BY donation_count DESC LIMIT 5""",
        (ngo['id'],)
    ).fetchall()
    
    # Average completion time (in days)
    avg_completion_time = conn.execute(
        """SELECT AVG(CAST((julianday(delivered_at) - julianday(assigned_at)) AS FLOAT))
           FROM food_distribution
           WHERE ngo_id = ? AND delivered_at IS NOT NULL AND assigned_at IS NOT NULL""",
        (ngo['id'],)
    ).fetchone()[0]
    
    conn.close()
    
    analytics = {
        'total_donations': total_donations,
        'completed_distributions': completed_distributions,
        'total_quantity': total_quantity,
        'active_assignments': active_assignments,
        'monthly_trend': list(monthly_trend) if monthly_trend else [],
        'status_breakdown': list(status_breakdown) if status_breakdown else [],
        'top_donors': list(top_donors) if top_donors else [],
        'avg_completion_time': round(avg_completion_time, 1) if avg_completion_time else 0
    }
    
    return render_template('ngo_analytics.html', ngo=ngo, analytics=analytics)


@app.route('/ngo/assignments')
@login_required
def ngo_assignments():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    return render_template('ngo_assignments.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers)


@app.route('/ngo/volunteers')
@login_required
def ngo_volunteers():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    ngo, my_assignments, available_donations, volunteers = _load_ngo_dashboard_data()
    if not ngo or not ngo['is_verified']:
        return render_template('ngo_pending.html', ngo=ngo)

    return render_template('ngo_volunteers.html', ngo=ngo, my_assignments=my_assignments, available_donations=available_donations, volunteers=volunteers)

@app.route('/ngo/accept/<int:donation_id>', methods=['POST'])
@login_required
def ngo_accept_donation(donation_id):
    if current_user.role != 'ngo':
        return jsonify({'error': 'Unauthorized'}), 403

    donation_type = request.args.get('donation_type') or request.form.get('donation_type')

    conn = get_db_connection()
    ngo = conn.execute("SELECT * FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()

    if not ngo['is_verified']:
        flash('Verify your account to perform transactions.', 'danger')
        conn.close()
        return redirect(url_for('ngo_dashboard'))

    # Determine donation type and fetch donation.
    # IDs are not globally unique across donation tables, so we must respect the explicit type when available.
    donation = None

    if donation_type in ('food', 'clothes', 'books', 'money'):
        table_map = {
            'food': 'food_donations',
            'clothes': 'clothes_donations',
            'books': 'book_donations',
            'money': 'money_donations',
        }
        row = conn.execute(f"SELECT * FROM {table_map[donation_type]} WHERE id = ?", (donation_id,)).fetchone()
        if row:
            donation = dict(row)
    else:
        for table_name, type_name in [
            ('food_donations', 'food'),
            ('clothes_donations', 'clothes'),
            ('book_donations', 'books'),
            ('money_donations', 'money'),
        ]:
            row = conn.execute(f"SELECT * FROM {table_name} WHERE id = ?", (donation_id,)).fetchone()
            if row:
                donation = dict(row)
                donation_type = type_name
                break

    if donation is None:
        flash('Donation not found.', 'danger')
        conn.close()
        return redirect(url_for('ngo_dashboard'))

    if donation_type == 'money':
        flash('Money donations use the payment workflow and cannot be claimed or assigned to volunteers.', 'info')
        conn.close()
        return redirect(url_for('ngo_money_donations'))

    status = str(donation.get('status') or '').strip()
    status_lower = status.lower()

    if status_lower == 'expired':
        flash(f'This {donation_type} donation has expired and cannot be accepted.', 'warning')
        conn.close()
        return redirect(url_for('ngo_dashboard'))

    # Non-food donations are marked as Approved after a successful claim. Some older records may still
    # contain Accepted or Approved values, so we treat those as already claimed rather than showing the generic error.
    if status_lower in ('approved', 'scheduled', 'accepted'):
        flash(f'This {donation_type} donation has already been claimed and is no longer available for acceptance.', 'warning')
        conn.close()
        return redirect(url_for('ngo_dashboard'))

    if status_lower not in ('pending', 'paid'):
        flash('This donation is no longer available for acceptance.', 'warning')
        conn.close()
        return redirect(url_for('ngo_dashboard'))

    # For food donations, check expiry
    if donation_type == 'food':
        expiry_dt = _parse_best_before_datetime(donation['expiry_time'])
        if expiry_dt and datetime.datetime.now() >= expiry_dt:
            conn.execute("UPDATE food_donations SET status = 'Expired' WHERE id = ?", (donation_id,))
            conn.execute("UPDATE food_distribution SET status = 'Expired' WHERE donation_id = ? AND status NOT IN ('Completed', 'Cancelled', 'Delivered', 'Expired')", (donation_id,))
            conn.commit()
            flash('This food donation has expired and cannot be accepted.', 'warning')
            conn.close()
            return redirect(url_for('ngo_dashboard'))

        # Update food donation and distribution record
        conn.execute("UPDATE food_donations SET status = 'Accepted' WHERE id = ?", (donation_id,))
        
        # Check if record exists in food_distribution
        dist = conn.execute("SELECT * FROM food_distribution WHERE donation_id = ?", (donation_id,)).fetchone()
        if dist:
            conn.execute(
                "UPDATE food_distribution SET ngo_id = ?, status = 'Accepted', assigned_at = ? WHERE donation_id = ?",
                (ngo['id'], datetime.datetime.now(), donation_id)
            )
        else:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO food_distribution (donation_id, ngo_id, status, assigned_at) VALUES (?, ?, 'Accepted', ?)",
                (donation_id, ngo['id'], datetime.datetime.now())
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "UPDATE food_distribution SET ngo_id = ?, status = 'Accepted', assigned_at = ? WHERE donation_id = ?",
                    (ngo['id'], datetime.datetime.now(), donation_id)
                )
    elif donation_type == 'clothes':
        conn.execute("UPDATE clothes_donations SET status = 'Approved' WHERE id = ?", (donation_id,))
    elif donation_type == 'books':
        conn.execute("UPDATE book_donations SET status = 'Approved' WHERE id = ?", (donation_id,))
    _record_lifecycle_activity(conn, donation_type, donation_id, 'Donation Accepted', 'Accepted' if donation_type == 'food' else 'Approved')
    conn.commit()
    
    # Notify donor
    donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
    donation_name = donation.get('food_name') or donation.get('item_type') or donation.get('title') or f'{donation.get("amount", 0)} donation'
    Notification.create(donor_user['user_id'], f"NGO '{ngo['ngo_name']}' has accepted your {donation_type} donation: {donation_name}.")

    if donation_type in ('food', 'clothes', 'books'):
        volunteers = conn.execute("SELECT id, name, phone FROM volunteers WHERE is_approved = 1 ORDER BY name").fetchall()
        flash(f'{donation_type.capitalize()} donation accepted. Please select a volunteer for pickup.', 'success')
        conn.close()
        return render_template(
            'ngo_select_volunteer.html',
            donation=donation,
            donation_type=donation_type,
            volunteers=volunteers,
        )

    flash(f'{donation_type.capitalize()} donation accepted successfully!', 'success')
    conn.close()
    return redirect(url_for('ngo_dashboard'))

@app.route('/ngo/assign/<int:donation_id>', methods=['POST'])
@login_required
def ngo_assign_volunteer(donation_id):
    if current_user.role != 'ngo':
        return jsonify({'error': 'Unauthorized'}), 403
        
    volunteer_id = request.form.get('volunteer_id')
    if not volunteer_id:
        flash('Please select a volunteer.', 'danger')
        return redirect(url_for('ngo_dashboard'))
        
    conn = get_db_connection()
    ngo = conn.execute("SELECT id FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
    if not ngo or not donation:
        conn.close()
        flash('The donation or NGO assignment was not found.', 'danger')
        return redirect(url_for('ngo_dashboard'))

    distribution = conn.execute(
        "SELECT volunteer_id, status FROM food_distribution WHERE donation_id = ? AND ngo_id = ?",
        (donation_id, ngo['id'])
    ).fetchone()
    if not distribution or distribution['status'] not in ('Accepted', 'Volunteer Assigned'):
        conn.close()
        flash('Only an accepted donation can be assigned to a volunteer.', 'danger')
        return redirect(url_for('ngo_dashboard'))
    if distribution['volunteer_id'] and distribution['status'] == 'Volunteer Assigned':
        conn.close()
        flash('This donation already has an active volunteer assignment.', 'warning')
        return redirect(url_for('ngo_dashboard'))

    volunteer = conn.execute(
        "SELECT id, user_id, name FROM volunteers WHERE id = ? AND is_approved = 1",
        (volunteer_id,)
    ).fetchone()
    if not volunteer:
        conn.close()
        flash('The selected volunteer is not available.', 'danger')
        return redirect(url_for('ngo_dashboard'))
    
    if donation:
        if donation['status'] == 'Expired':
            flash('This donation has expired. It cannot be assigned to a volunteer or picked up.', 'warning')
            conn.close()
            return redirect(url_for('ngo_dashboard'))
        # Assign volunteer in food_distribution and create pickup assignment tracker
        conn.execute(
            "UPDATE food_distribution SET volunteer_id = ?, status = 'Volunteer Assigned' WHERE donation_id = ? AND ngo_id = ?",
            (volunteer_id, donation_id, ngo['id'])
        )
        conn.execute("UPDATE food_donations SET status = 'Volunteer Assigned' WHERE id = ?", (donation_id,))
        
        # Insert pickup history tracker
        conn.execute(
            "INSERT INTO pickup_assignments (donation_id, ngo_id, volunteer_id, status) VALUES (?, ?, ?, 'Assigned')",
            (donation_id, ngo['id'], volunteer_id)
        )
        _record_lifecycle_activity(conn, 'food', donation_id, 'Volunteer Assigned', 'Volunteer Assigned')
        conn.commit()
        
        # Notify Volunteer
        volunteer_user = volunteer
        Notification.create(volunteer_user['user_id'], f"You have been assigned a new food pickup: {donation['food_name']} at {donation['address']}.", title='New Donation Delivery Assignment', donation_id=donation_id, donation_type='Food', status='Volunteer Assigned')
        
        flash(f"Donation assigned to volunteer '{volunteer_user['name']}' successfully.", 'success')
        
    conn.close()
    return redirect(url_for('ngo_dashboard'))


@app.route('/ngo/assign/<donation_type>/<int:donation_id>', methods=['POST'])
@login_required
def ngo_assign_non_food_volunteer(donation_type, donation_id):
    if current_user.role != 'ngo' or donation_type not in ('clothes', 'books'):
        return jsonify({'error': 'Unauthorized'}), 403

    volunteer_id = request.form.get('volunteer_id', type=int)
    conn = get_db_connection()
    ngo = conn.execute("SELECT id FROM ngos WHERE user_id = ? AND is_verified = 1", (current_user.id,)).fetchone()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE id = ? AND is_approved = 1", (volunteer_id,)).fetchone()
    table = 'clothes_donations' if donation_type == 'clothes' else 'book_donations'
    donation = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND status = 'Approved'", (donation_id,)).fetchone()

    if not ngo or not volunteer or not donation:
        conn.close()
        flash('The donation or selected volunteer is no longer available.', 'danger')
        return redirect(url_for('ngo_dashboard'))

    existing_assignment = conn.execute(
        "SELECT volunteer_id, status FROM donation_distribution WHERE donation_type = ? AND donation_id = ?",
        (donation_type, donation_id)
    ).fetchone()
    if existing_assignment and existing_assignment['status'] not in ('Cancelled', 'Completed'):
        conn.close()
        flash('This donation already has an active volunteer assignment.', 'warning')
        return redirect(url_for('ngo_dashboard'))

    conn.execute(
        "INSERT OR REPLACE INTO donation_assignments (donation_type, donation_id, ngo_id, volunteer_id, status) VALUES (?, ?, ?, ?, 'Assigned')",
        (donation_type, donation_id, ngo['id'], volunteer['id'])
    )
    conn.execute(
        """INSERT OR REPLACE INTO donation_distribution
           (donation_type, donation_id, ngo_id, volunteer_id, status, assigned_at)
           VALUES (?, ?, ?, ?, 'Assigned', CURRENT_TIMESTAMP)""",
        (donation_type, donation_id, ngo['id'], volunteer['id'])
    )
    conn.execute(f"UPDATE {table} SET status = 'Scheduled' WHERE id = ?", (donation_id,))
    _record_lifecycle_activity(conn, donation_type, donation_id, 'Volunteer Assigned', 'Scheduled')
    conn.commit()
    Notification.create(volunteer['user_id'], f"You have been assigned a new {donation_type} donation pickup.", title='New Donation Delivery Assignment', donation_id=donation_id, donation_type=donation_type.capitalize(), status='Assigned')
    conn.close()
    flash(f"Donation assigned to volunteer '{volunteer['name']}' successfully.", 'success')
    return redirect(url_for('ngo_dashboard'))

@app.route('/ngo/history')
@login_required
def ngo_history():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    ngo = conn.execute("SELECT id FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
    history = conn.execute(
        """SELECT fd.*, d.created_at, d.food_name, d.quantity, d.veg_non_veg, d.category, d.address, d.image_path, v.name as volunteer_name, n.organization_type,
                  f.rating, f.food_rating, f.delivery_rating, f.comments
           FROM food_distribution fd
           JOIN food_donations d ON fd.donation_id = d.id
           LEFT JOIN volunteers v ON fd.volunteer_id = v.id
           LEFT JOIN ngos n ON fd.ngo_id = n.id
           LEFT JOIN feedback f ON f.donation_id = d.id
           WHERE fd.ngo_id = ? ORDER BY fd.assigned_at DESC""",
        (ngo['id'],)
    ).fetchall()
    non_food_history = conn.execute(
        """SELECT dd.*, d.created_at, d.item_type AS food_name, d.quantity, d.category,
                  d.address, d.image_path, v.name AS volunteer_name, n.organization_type,
                  NULL AS veg_non_veg
           FROM donation_distribution dd
           JOIN clothes_donations d ON dd.donation_type = 'clothes' AND dd.donation_id = d.id
           LEFT JOIN volunteers v ON v.id = dd.volunteer_id
           LEFT JOIN ngos n ON n.id = dd.ngo_id
           WHERE dd.ngo_id = ?
           UNION ALL
           SELECT dd.*, d.created_at, d.title AS food_name, d.quantity, d.category,
                  d.address, d.image_path, v.name AS volunteer_name, n.organization_type,
                  NULL AS veg_non_veg
           FROM donation_distribution dd
           JOIN book_donations d ON dd.donation_type = 'books' AND dd.donation_id = d.id
           LEFT JOIN volunteers v ON v.id = dd.volunteer_id
           LEFT JOIN ngos n ON n.id = dd.ngo_id
           WHERE dd.ngo_id = ?
           UNION ALL
           SELECT dd.*, d.created_at, COALESCE(o.name, 'Money donation') AS food_name,
                  CAST(d.amount AS TEXT) AS quantity, 'Money' AS category, o.address,
                  NULL AS image_path, v.name AS volunteer_name, n.organization_type,
                  NULL AS veg_non_veg
           FROM donation_distribution dd
           JOIN money_donations d ON dd.donation_type = 'money' AND dd.donation_id = d.id
           LEFT JOIN orphanages o ON o.id = d.orphanage_id
           LEFT JOIN volunteers v ON v.id = dd.volunteer_id
           LEFT JOIN ngos n ON n.id = dd.ngo_id
           WHERE dd.ngo_id = ?""",
        (ngo['id'], ngo['id'], ngo['id'])
    ).fetchall()
    legacy_non_food_history = conn.execute(
        """SELECT a.donation_type, a.donation_id, a.status, a.assigned_at,
                  d.created_at, d.item_type AS food_name, d.quantity, d.category,
                  d.address, d.image_path, v.name AS volunteer_name, n.organization_type,
                  NULL AS veg_non_veg, NULL AS picked_up_at, NULL AS delivered_at,
                  NULL AS delivery_proof_path
           FROM donation_assignments a
           JOIN clothes_donations d ON a.donation_type = 'clothes' AND a.donation_id = d.id
           LEFT JOIN volunteers v ON v.id = a.volunteer_id
           LEFT JOIN ngos n ON n.id = a.ngo_id
           WHERE a.ngo_id = ? AND NOT EXISTS (
               SELECT 1 FROM donation_distribution dd
               WHERE dd.donation_type = a.donation_type AND dd.donation_id = a.donation_id
           )
           UNION ALL
           SELECT a.donation_type, a.donation_id, a.status, a.assigned_at,
                  d.created_at, d.title AS food_name, d.quantity, d.category,
                  d.address, d.image_path, v.name AS volunteer_name, n.organization_type,
                  NULL AS veg_non_veg, NULL AS picked_up_at, NULL AS delivered_at,
                  NULL AS delivery_proof_path
           FROM donation_assignments a
           JOIN book_donations d ON a.donation_type = 'books' AND a.donation_id = d.id
           LEFT JOIN volunteers v ON v.id = a.volunteer_id
           LEFT JOIN ngos n ON n.id = a.ngo_id
           WHERE a.ngo_id = ? AND NOT EXISTS (
               SELECT 1 FROM donation_distribution dd
               WHERE dd.donation_type = a.donation_type AND dd.donation_id = a.donation_id
           )""",
        (ngo['id'], ngo['id'])
    ).fetchall()
    history = list(history) + list(non_food_history) + list(legacy_non_food_history)
    history.sort(key=lambda item: item['created_at'] or '', reverse=True)
    conn.close()
    return render_template('ngo_history.html', history=history)

@app.route('/ngo/feedback/<int:donation_id>', methods=['GET', 'POST'])
@login_required
def ngo_feedback(donation_id):
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    conn = get_db_connection()
    ngo = conn.execute(
        "SELECT id FROM ngos WHERE user_id = ?",
        (current_user.id,)
    ).fetchone()
    eligible_donation = None
    if ngo:
        eligible_donation = conn.execute(
            """SELECT 1
               FROM food_distribution fd
               JOIN food_donations d ON d.id = fd.donation_id
               WHERE fd.donation_id = ? AND fd.ngo_id = ? AND fd.status = 'Completed'""",
            (donation_id, ngo['id'])
        ).fetchone()

    if not eligible_donation:
        conn.close()
        flash('Feedback is only available for completed food donations assigned to your NGO.', 'warning')
        return redirect(url_for('ngo_history'))

    form = FeedbackForm()
    if form.validate_on_submit():
        overall_rating = int((int(form.food_rating.data) + int(form.delivery_rating.data)) / 2)
        conn.execute(
            "INSERT INTO feedback (user_id, donation_id, rating, food_rating, delivery_rating, comments) VALUES (?, ?, ?, ?, ?, ?)",
            (current_user.id, donation_id, overall_rating, int(form.food_rating.data), int(form.delivery_rating.data), form.comments.data)
        )
        conn.commit()
        conn.close()
        flash('Feedback submitted successfully. Thank you!', 'success')
        return redirect(url_for('ngo_history'))

    conn.close()
    return render_template('ngo_feedback.html', form=form, donation_id=donation_id)


# --- VOLUNTEER MODULE ---

@app.route('/volunteer/dashboard')
@login_required
def volunteer_dashboard():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))
        
    conn = get_db_connection()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    
    if not volunteer['is_approved']:
        conn.close()
        return render_template('volunteer_pending.html', volunteer=volunteer)
        
    # Active Assigned pickups (include latest inspection if any)
    food_pickups = conn.execute(
        """SELECT fd.donation_id, 'food' AS donation_type, fd.status, d.created_at, d.food_name, d.quantity, d.expiry_time, d.address, d.qr_code_path, d.image_path,
                 n.ngo_name, n.organization_type, dn.company_name as donor_name, dn.phone as donor_phone,
                 d.inspection_status, d.inspection_result, d.inspected_by, d.inspection_completed_at,
                 fi.inspected_at AS inspection_at, fi.notes AS inspection_notes, fi.rejection_reason
           FROM food_distribution fd
           JOIN food_donations d ON fd.donation_id = d.id
           JOIN ngos n ON fd.ngo_id = n.id
           JOIN donors dn ON d.donor_id = dn.id
           LEFT JOIN food_inspections fi ON d.id = fi.donation_id AND fi.volunteer_id = ?
           WHERE fd.volunteer_id = ? AND fd.status IN ('Volunteer Assigned', 'Picked Up', 'Cancelled')""",
        (volunteer['id'], volunteer['id'])
    ).fetchall()
    non_food_pickups = conn.execute(
         """SELECT dd.donation_id, dd.donation_type, dd.status, c.created_at,
                c.item_type AS food_name, c.quantity, NULL AS expiry_time, c.address,
                NULL AS qr_code_path, c.image_path, n.ngo_name, n.organization_type,
                dn.company_name AS donor_name, dn.phone AS donor_phone,
                NULL AS inspection_status, NULL AS inspection_result, NULL AS inspected_by, NULL AS inspection_completed_at, NULL AS inspection_at, NULL AS inspection_notes, NULL AS rejection_reason
            FROM donation_distribution dd
            JOIN clothes_donations c ON c.id = dd.donation_id AND dd.donation_type = 'clothes'
            JOIN donors dn ON dn.id = c.donor_id JOIN ngos n ON n.id = dd.ngo_id
            WHERE dd.volunteer_id = ? AND dd.status IN ('Assigned', 'Picked Up')
            UNION ALL
            SELECT dd.donation_id, dd.donation_type, dd.status, b.created_at,
                b.title AS food_name, b.quantity, NULL AS expiry_time, b.address,
                NULL AS qr_code_path, b.image_path, n.ngo_name, n.organization_type,
                dn.company_name AS donor_name, dn.phone AS donor_phone,
                NULL AS inspection_status, NULL AS inspection_result, NULL AS inspected_by, NULL AS inspection_completed_at, NULL AS inspection_at, NULL AS inspection_notes, NULL AS rejection_reason
            FROM donation_distribution dd
            JOIN book_donations b ON b.id = dd.donation_id AND dd.donation_type = 'books'
            JOIN donors dn ON dn.id = b.donor_id JOIN ngos n ON n.id = dd.ngo_id
               WHERE dd.volunteer_id = ? AND dd.status IN ('Assigned', 'Picked Up')""",
         (volunteer['id'], volunteer['id'])
    ).fetchall()
    assigned_pickups = [dict(row) for row in food_pickups] + [dict(row) for row in non_food_pickups]
    
    top_donors = conn.execute(
        """SELECT dn.company_name, COUNT(d.id) AS delivered_count
           FROM food_donations d
           JOIN donors dn ON d.donor_id = dn.id
           WHERE d.status = 'Delivered'
           GROUP BY dn.id
           ORDER BY delivered_count DESC
           LIMIT 10"""
    ).fetchall()
    top_volunteers = conn.execute(
        """SELECT v.name, COUNT(pa.id) AS deliveries
           FROM pickup_assignments pa
           JOIN volunteers v ON pa.volunteer_id = v.id
           WHERE pa.status = 'Delivered'
           GROUP BY v.id
           ORDER BY deliveries DESC
           LIMIT 10"""
    ).fetchall()
    # Recent certificates for this volunteer
    certificates = conn.execute(
        """SELECT 'food' AS donation_type, c.id, c.donation_id, c.volunteer_id, c.donor_id, c.ngo_id,
                  c.certificate_path, c.issued_at, d.food_name, d.created_at as donated_at
           FROM certificates c
           JOIN food_donations d ON c.donation_id = d.id
           WHERE c.volunteer_id = ?
           UNION ALL
           SELECT dc.donation_type, dc.id, dc.donation_id, dc.volunteer_id, dc.donor_id, dc.ngo_id,
                  dc.certificate_path, dc.issued_at,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END AS food_name,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.created_at ELSE bd.created_at END AS donated_at
           FROM donation_certificates dc
           LEFT JOIN clothes_donations cd ON dc.donation_type = 'clothes' AND dc.donation_id = cd.id
           LEFT JOIN book_donations bd ON dc.donation_type = 'books' AND dc.donation_id = bd.id
           WHERE dc.volunteer_id = ?
           ORDER BY issued_at DESC
           LIMIT 5""",
        (volunteer['id'], volunteer['id'])
    ).fetchall()
    active_emergencies = conn.execute(
        "SELECT * FROM emergencies WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    feedback_entries = conn.execute(
        """SELECT f.id, f.rating, f.comments, f.created_at, f.food_rating, f.delivery_rating,
                   u.username AS reviewer_name, d.food_name
           FROM feedback f
           JOIN food_donations d ON f.donation_id = d.id
           JOIN food_distribution fd ON fd.donation_id = d.id
           LEFT JOIN users u ON f.user_id = u.id
           WHERE fd.volunteer_id = ?
           ORDER BY f.created_at DESC
           LIMIT 5""",
        (volunteer['id'],)
    ).fetchall()
    stats_row = conn.execute(
        "SELECT COUNT(*) AS total, AVG(f.rating) AS avg_rating "
        "FROM feedback f "
        "JOIN food_donations d ON f.donation_id = d.id "
        "JOIN food_distribution fd ON fd.donation_id = d.id "
        "WHERE fd.volunteer_id = ?",
        (volunteer['id'],)
    ).fetchone()
    feedback_avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    conn.close()
    return render_template('volunteer_dashboard.html', volunteer=volunteer, assigned_pickups=assigned_pickups, top_donors=top_donors, top_volunteers=top_volunteers, certificates=certificates, active_emergencies=active_emergencies, feedback_entries=feedback_entries, feedback_avg_rating=feedback_avg_rating, feedback_count=len(feedback_entries))

@app.route('/volunteer/feedback')
@login_required
def volunteer_feedback():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))

    conn = get_db_connection()
    volunteer = conn.execute("SELECT id, name FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    if not volunteer:
        conn.close()
        return redirect(url_for('index'))

    feedback_entries = conn.execute(
        """SELECT f.id, f.rating, f.comments, f.created_at, f.food_rating, f.delivery_rating,
                   u.username AS reviewer_name, d.food_name
           FROM feedback f
           JOIN food_donations d ON f.donation_id = d.id
           JOIN food_distribution fd ON fd.donation_id = d.id
           LEFT JOIN users u ON f.user_id = u.id
           WHERE fd.volunteer_id = ?
           ORDER BY f.created_at DESC""",
        (volunteer['id'],)
    ).fetchall()
    stats_row = conn.execute(
        "SELECT COUNT(*) AS total, AVG(f.rating) AS avg_rating "
        "FROM feedback f "
        "JOIN food_donations d ON f.donation_id = d.id "
        "JOIN food_distribution fd ON fd.donation_id = d.id "
        "WHERE fd.volunteer_id = ?",
        (volunteer['id'],)
    ).fetchone()
    avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    conn.close()

    return render_template('volunteer_feedback.html', volunteer=volunteer, feedback_entries=feedback_entries, avg_rating=avg_rating)

@app.route('/ngo/feedback')
@login_required
def ngo_feedback_view():
    if current_user.role != 'ngo':
        return redirect(url_for('index'))

    conn = get_db_connection()
    ngo = conn.execute("SELECT id, ngo_name FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
    if not ngo:
        conn.close()
        return redirect(url_for('index'))

    feedback_entries = conn.execute(
        """SELECT f.id, f.rating, f.comments, f.created_at, f.food_rating, f.delivery_rating,
                   u.username AS reviewer_name, d.food_name
           FROM feedback f
           JOIN food_donations d ON f.donation_id = d.id
           JOIN food_distribution fd ON fd.donation_id = d.id
           LEFT JOIN users u ON f.user_id = u.id
           WHERE fd.ngo_id = ?
           ORDER BY f.created_at DESC""",
        (ngo['id'],)
    ).fetchall()
    stats_row = conn.execute(
        "SELECT COUNT(*) AS total, AVG(f.rating) AS avg_rating "
        "FROM feedback f "
        "JOIN food_donations d ON f.donation_id = d.id "
        "JOIN food_distribution fd ON fd.donation_id = d.id "
        "WHERE fd.ngo_id = ?",
        (ngo['id'],)
    ).fetchone()
    avg_rating = round(stats_row['avg_rating'], 1) if stats_row['avg_rating'] is not None else 0
    feedback_count = stats_row['total'] if stats_row['total'] is not None else 0
    conn.close()

    return render_template('ngo_feedback_view.html', ngo=ngo, feedback_entries=feedback_entries, avg_rating=avg_rating, feedback_count=feedback_count)

@app.route('/volunteer/summary')
@login_required
def volunteer_dashboard_summary():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))

    conn = get_db_connection()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    assigned_pickups = conn.execute(
        """SELECT fd.*, d.created_at, d.food_name, d.quantity, d.expiry_time, d.address, n.ngo_name, n.organization_type, dn.company_name as donor_name, dn.phone as donor_phone
           FROM food_distribution fd
           JOIN food_donations d ON fd.donation_id = d.id
           JOIN ngos n ON fd.ngo_id = n.id
           JOIN donors dn ON d.donor_id = dn.id
           WHERE fd.volunteer_id = ? AND fd.status IN ('Volunteer Assigned', 'Picked Up')""",
        (volunteer['id'],)
    ).fetchall()
    certificates = conn.execute(
          """SELECT 'food' AS donation_type, c.*, d.food_name, d.created_at as donated_at
           FROM certificates c
           JOIN food_donations d ON c.donation_id = d.id
           WHERE c.volunteer_id = ?
              UNION ALL
              SELECT dc.donation_type, dc.id, dc.donation_id, dc.volunteer_id, dc.donor_id, dc.ngo_id,
                        dc.certificate_path, dc.issued_at,
                        CASE WHEN dc.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END,
                        CASE WHEN dc.donation_type = 'clothes' THEN cd.created_at ELSE bd.created_at END
              FROM donation_certificates dc
              LEFT JOIN clothes_donations cd ON dc.donation_type = 'clothes' AND dc.donation_id = cd.id
              LEFT JOIN book_donations bd ON dc.donation_type = 'books' AND dc.donation_id = bd.id
              WHERE dc.volunteer_id = ?
              ORDER BY issued_at DESC""",
          (volunteer['id'], volunteer['id'])
    ).fetchall()
    top_donors = conn.execute(
        """SELECT dn.company_name, COUNT(d.id) AS delivered_count
           FROM food_donations d
           JOIN donors dn ON d.donor_id = dn.id
           WHERE d.status = 'Delivered'
           GROUP BY dn.id
           ORDER BY delivered_count DESC
           LIMIT 5"""
    ).fetchall()
    top_volunteers = conn.execute(
        """SELECT v.name, COUNT(pa.id) AS deliveries
           FROM pickup_assignments pa
           JOIN volunteers v ON pa.volunteer_id = v.id
           WHERE pa.status = 'Delivered'
           GROUP BY v.id
           ORDER BY deliveries DESC
           LIMIT 5"""
    ).fetchall()
    active_emergencies = conn.execute(
        "SELECT * FROM emergencies WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return render_template('volunteer_dashboard_summary.html', volunteer=volunteer, assigned_pickups=assigned_pickups, certificates=certificates, top_donors=top_donors, top_volunteers=top_volunteers, active_emergencies=active_emergencies)

@app.route('/volunteer/certificates')
@login_required
def volunteer_certificates():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))

    conn = get_db_connection()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    certificates = conn.execute(
        """SELECT 'food' AS donation_type, c.id, c.donation_id, c.volunteer_id, c.donor_id, c.ngo_id,
                  c.certificate_path, c.issued_at, d.food_name, d.created_at as donated_at
           FROM certificates c
           JOIN food_donations d ON c.donation_id = d.id
           WHERE c.volunteer_id = ?
           UNION ALL
           SELECT dc.donation_type, dc.id, dc.donation_id, dc.volunteer_id, dc.donor_id, dc.ngo_id,
                  dc.certificate_path, dc.issued_at,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.item_type ELSE bd.title END AS food_name,
                  CASE WHEN dc.donation_type = 'clothes' THEN cd.created_at ELSE bd.created_at END AS donated_at
           FROM donation_certificates dc
           LEFT JOIN clothes_donations cd ON dc.donation_type = 'clothes' AND dc.donation_id = cd.id
           LEFT JOIN book_donations bd ON dc.donation_type = 'books' AND dc.donation_id = bd.id
           WHERE dc.volunteer_id = ?
           ORDER BY issued_at DESC""",
        (volunteer['id'], volunteer['id'])
    ).fetchall()
    active_emergencies = conn.execute(
        "SELECT * FROM emergencies WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return render_template('volunteer_certificates.html', volunteer=volunteer, certificates=certificates, active_emergencies=active_emergencies)

@app.route('/volunteer/emergencies')
@login_required
def volunteer_emergencies():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))

    conn = get_db_connection()
    volunteer = conn.execute("SELECT * FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    active_emergencies = conn.execute(
        "SELECT * FROM emergencies WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return render_template('volunteer_emergencies.html', volunteer=volunteer, active_emergencies=active_emergencies)

@app.route('/volunteer/pickup/<string:donation_type>/<int:donation_id>', methods=['POST'])
@login_required
def volunteer_non_food_pickup(donation_type, donation_id):
    if current_user.role != 'volunteer' or donation_type not in ('clothes', 'books'):
        return jsonify({'error': 'Unauthorized'}), 403
    table = 'clothes_donations' if donation_type == 'clothes' else 'book_donations'
    conn = get_db_connection()
    volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND status IN ('Scheduled', 'Picked Up')", (donation_id,)
    ).fetchone()
    distribution = conn.execute(
        "SELECT * FROM donation_distribution WHERE donation_type = ? AND donation_id = ? AND volunteer_id = ?",
        (donation_type, donation_id, volunteer['id']),
    ).fetchone() if volunteer else None
    if not donation or not distribution:
        conn.close()
        flash('This donation is not assigned to you.', 'danger')
        return redirect(url_for('volunteer_dashboard'))
    conn.execute("UPDATE donation_distribution SET status = 'Picked Up', picked_up_at = CURRENT_TIMESTAMP WHERE id = ?", (distribution['id'],))
    conn.execute(f"UPDATE {table} SET status = 'Picked Up' WHERE id = ?", (donation_id,))
    _record_lifecycle_activity(conn, donation_type, donation_id, 'Pickup Completed', 'Picked Up')
    donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (distribution['ngo_id'],)).fetchone()
    conn.commit()
    conn.close()
    if donor_user:
        Notification.create(donor_user['user_id'], f"Your {donation_type} donation has been picked up by the volunteer.", title='Donation Picked Up', donation_id=donation_id, donation_type=donation_type.capitalize(), status='Picked Up')
    if ngo_user:
        Notification.create(ngo_user['user_id'], f"Your assigned volunteer has picked up the {donation_type} donation.", title='Donation Picked Up', donation_id=donation_id, donation_type=donation_type.capitalize(), status='Picked Up')
    flash(f'{donation_type.capitalize()} donation pickup confirmed.', 'success')
    return redirect(url_for('volunteer_dashboard'))


@app.route('/volunteer/deliver/<string:donation_type>/<int:donation_id>', methods=['POST'])
@login_required
def volunteer_non_food_deliver(donation_type, donation_id):
    if current_user.role != 'volunteer' or donation_type not in ('clothes', 'books'):
        return jsonify({'error': 'Unauthorized'}), 403
    table = 'clothes_donations' if donation_type == 'clothes' else 'book_donations'
    conn = get_db_connection()
    volunteer = conn.execute("SELECT id, name FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    distribution = conn.execute(
        "SELECT * FROM donation_distribution WHERE donation_type = ? AND donation_id = ? AND volunteer_id = ? AND status = 'Picked Up'",
        (donation_type, donation_id, volunteer['id']),
    ).fetchone() if volunteer else None
    donation = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (donation_id,)).fetchone()
    proof_file = request.files.get('delivery_proof')
    if not distribution or not donation or not proof_file or not proof_file.filename:
        conn.close()
        flash('A valid assigned donation and delivery proof are required.', 'danger')
        return redirect(url_for('volunteer_dashboard'))
    filename = secure_filename(f"proof_{donation_type}_{donation_id}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{proof_file.filename}")
    filepath = os.path.join(Config.UPLOAD_FOLDER, 'proofs', filename)
    proof_file.save(filepath)
    proof_path = f"uploads/proofs/{filename}"
    conn.execute(
        "UPDATE donation_distribution SET status = 'Completed', delivered_at = CURRENT_TIMESTAMP, delivery_proof_path = ? WHERE id = ?",
        (proof_path, distribution['id']),
    )
    conn.execute(f"UPDATE {table} SET status = 'Delivered' WHERE id = ?", (donation_id,))
    _record_lifecycle_activity(conn, donation_type, donation_id, 'Distribution Completed', 'Delivered')
    _ensure_non_food_certificate(conn, donation_type, donation_id, volunteer['id'])
    donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (distribution['ngo_id'],)).fetchone()
    conn.commit()
    conn.close()
    if donor_user:
        Notification.create(donor_user['user_id'], f"Your {donation_type} donation has been delivered.", title='Donation Delivered', donation_id=donation_id, donation_type=donation_type.capitalize(), status='Delivered')
    if ngo_user:
        Notification.create(ngo_user['user_id'], f"The {donation_type} donation has been delivered by the volunteer.", title='Donation Delivered', donation_id=donation_id, donation_type=donation_type.capitalize(), status='Delivered')
    flash(f'{donation_type.capitalize()} delivery completed and certificate issued.', 'success')
    return redirect(url_for('volunteer_dashboard'))


@app.route('/volunteer/pickup/<int:donation_id>', methods=['POST'])
@login_required
def volunteer_pickup(donation_id):
    if current_user.role != 'volunteer':
        return jsonify({'error': 'Unauthorized'}), 403
        
    conn = get_db_connection()
    volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('volunteer_dashboard'))

    if donation['status'] == 'Expired':
        conn.close()
        flash('This donation has expired. Pickup and delivery are not allowed.', 'warning')
        return redirect(url_for('volunteer_dashboard'))

    # Pickup is allowed only after this volunteer completed an approved inspection.
    inspection = conn.execute("SELECT * FROM food_inspections WHERE donation_id = ? AND volunteer_id = ? ORDER BY inspected_at DESC LIMIT 1", (donation_id, volunteer['id'])).fetchone()
    if not inspection or inspection['status'] != 'Approved' or donation['inspection_status'] != 'Inspection Completed' or donation['inspection_result'] != 'Approved':
        conn.close()
        flash('You must perform and approve a food safety inspection before picking up this donation.', 'warning')
        return redirect(url_for('volunteer_inspect', donation_id=donation_id))

    # Update state - approved inspection exists, proceed to pickup
    conn.execute(
        "UPDATE food_distribution SET status = 'Picked Up', picked_up_at = ? WHERE donation_id = ? AND volunteer_id = ?",
        (datetime.datetime.now(), donation_id, volunteer['id'])
    )
    conn.execute("UPDATE food_donations SET status = 'Picked Up' WHERE id = ?", (donation_id,))
    conn.execute("UPDATE pickup_assignments SET status = 'Picked Up' WHERE donation_id = ? AND volunteer_id = ?", (donation_id, volunteer['id']))
    _record_lifecycle_activity(conn, 'food', donation_id, 'Pickup Completed', 'Picked Up')
    conn.commit()
    
    # Notify Donor and NGO
    donation = conn.execute("SELECT d.food_name, d.donor_id, fd.ngo_id FROM food_donations d JOIN food_distribution fd ON d.id = fd.donation_id WHERE d.id = ?", (donation_id,)).fetchone()
    donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (donation['ngo_id'],)).fetchone()
    
    Notification.create(donor_user['user_id'], f"Volunteer has picked up your surplus food donation: {donation['food_name']}.")
    Notification.create(ngo_user['user_id'], f"Volunteer has successfully picked up donation {donation_id} from the donor.")
    
    conn.close()
    flash('Donation marked as Picked Up! Keep moving towards the destination NGO.', 'success')
    return redirect(url_for('volunteer_dashboard'))


@app.route('/volunteer/inspect/<int:donation_id>', methods=['GET', 'POST'])
@login_required
def volunteer_inspect(donation_id):
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))

    conn = get_db_connection()
    volunteer = conn.execute("SELECT id, name FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    # Verify assignment
    dist = conn.execute("SELECT * FROM food_distribution WHERE donation_id = ? AND volunteer_id = ?", (donation_id, volunteer['id'])).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
    if not dist or not donation:
        conn.close()
        flash('This donation is not assigned to you or does not exist.', 'danger')
        return redirect(url_for('volunteer_dashboard'))

    if donation['status'] == 'Expired':
        conn.close()
        flash('This donation has expired. Pickup and delivery are not allowed.', 'warning')
        return redirect(url_for('volunteer_dashboard'))

    if donation['inspection_status'] == 'Inspection Completed':
        conn.close()
        flash(f"Inspection already completed: {donation['inspection_result']}.", 'info')
        return redirect(url_for('volunteer_dashboard'))

    if request.method == 'POST':
        # Read checklist
        appearance = 1 if request.form.get('appearance') == 'yes' else 0
        packaging = 1 if request.form.get('packaging') == 'yes' else 0
        within_expiry = 1 if request.form.get('within_expiry') == 'yes' else 0
        storage_ok = 1 if request.form.get('storage_ok') == 'yes' else 0
        suitable = 1 if request.form.get('suitable') == 'yes' else 0
        notes = request.form.get('notes')
        requested_decision = request.form.get('decision')
        decision = 'Approved' if requested_decision == 'Approved' and all((appearance, packaging, within_expiry, storage_ok, suitable)) else 'Rejected'
        rejection_reason = request.form.get('rejection_reason')
        if decision == 'Rejected' and not rejection_reason:
            rejection_reason = 'One or more food-safety checks failed.'

        conn.execute(
            "INSERT INTO food_inspections (donation_id, volunteer_id, appearance_ok, packaging_intact, within_expiry, storage_ok, suitable, notes, status, rejection_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (donation_id, volunteer['id'], appearance, packaging, within_expiry, storage_ok, suitable, notes, decision, rejection_reason)
        )
        conn.execute(
            "UPDATE food_donations SET inspection_status = 'Inspection Completed', inspection_result = ?, inspected_by = ?, inspection_completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (decision, volunteer['id'], donation_id)
        )
        conn.commit()

        donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
        ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (dist['ngo_id'],)).fetchone()
        admin_users = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchall()
        result_message = f"{decision}."
        if donor_user:
            Notification.create(donor_user['user_id'], f"Food inspection completed for {donation['food_name']}. Result: {result_message}", title='Food Inspection Completed', donation_id=donation_id, donation_type='Food', status=f'Inspection Completed - {decision}', event_key=f'inspection:donor:{donation_id}')
        if ngo_user:
            Notification.create(ngo_user['user_id'], f"Food inspection completed for {donation['food_name']}. Result: {result_message}", title='Food Inspection Completed', donation_id=donation_id, donation_type='Food', status=f'Inspection Completed - {decision}', event_key=f'inspection:ngo:{donation_id}')
        Notification.create(current_user.id, f"Inspection completed for {donation['food_name']}. Result: {result_message}", title='Food Inspection Completed', donation_id=donation_id, donation_type='Food', status=f'Inspection Completed - {decision}', event_key=f'inspection:volunteer:{donation_id}')
        for admin_user in admin_users:
            Notification.create(admin_user['id'], f"Inspection completed for {donation['food_name']} by {volunteer['name']}. Result: {result_message}", title='Food Inspection Completed', donation_id=donation_id, donation_type='Food', status=f'Inspection Completed - {decision}', event_key=f'inspection:admin:{admin_user["id"]}:{donation_id}')

        # If approved, keep the donation in the assigned state until the volunteer explicitly confirms pickup.
        if decision == 'Approved':
            conn.close()
            flash('Inspection approved. The donation remains assigned until pickup is confirmed.', 'success')
            return redirect(url_for('volunteer_dashboard'))

        # If rejected, cancel assignment and notify donor/NGO
        else:
            conn.execute("UPDATE food_distribution SET status = 'Cancelled' WHERE donation_id = ?", (donation_id,))
            conn.execute("UPDATE food_donations SET status = 'Cancelled' WHERE id = ?", (donation_id,))
            conn.execute("UPDATE pickup_assignments SET status = 'Cancelled' WHERE donation_id = ? AND volunteer_id = ?", (donation_id, volunteer['id']))
            conn.commit()

            conn.close()
            flash('Inspection marked as Rejected. Donor and NGO notified.', 'warning')
            return redirect(url_for('volunteer_dashboard'))

    # For GET, show inspection form
    conn.close()
    return render_template('volunteer_inspect.html', donation=donation)

@app.route('/volunteer/deliver/<int:donation_id>', methods=['POST'])
@login_required
def volunteer_deliver(donation_id):
    if current_user.role != 'volunteer':
        return jsonify({'error': 'Unauthorized'}), 403
        
    conn = get_db_connection()
    volunteer = conn.execute("SELECT id, name FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
    if not donation:
        conn.close()
        flash('Donation not found.', 'danger')
        return redirect(url_for('volunteer_dashboard'))

    if donation['status'] == 'Expired':
        conn.close()
        flash('This donation has expired. Pickup and delivery are not allowed.', 'warning')
        return redirect(url_for('volunteer_dashboard'))
    
    # Handle delivery proof file upload
    proof_file = request.files.get('delivery_proof')
    proof_relative_path = None
    # Enforce delivery proof is provided
    if not proof_file or not getattr(proof_file, 'filename', None):
        conn.close()
        flash('Please upload a delivery proof image before confirming delivery.', 'danger')
        return redirect(url_for('volunteer_dashboard'))

    if proof_file and proof_file.filename:
        filename = secure_filename(f"proof_{donation_id}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{proof_file.filename}")
        filepath = os.path.join(Config.UPLOAD_FOLDER, 'proofs', filename)
        proof_file.save(filepath)
        proof_relative_path = f"uploads/proofs/{filename}"
        
    distribution = conn.execute(
        "SELECT status FROM food_distribution WHERE donation_id = ? AND volunteer_id = ?",
        (donation_id, volunteer['id'])
    ).fetchone()
    if not distribution or distribution['status'] != 'Picked Up' or donation['inspection_status'] != 'Inspection Completed' or donation['inspection_result'] != 'Approved':
        conn.close()
        flash('Delivery is allowed only after an approved inspection and pickup.', 'warning')
        return redirect(url_for('volunteer_dashboard'))

    # Update states only after pickup and approved inspection.
    conn.execute(
        "UPDATE food_distribution SET status = 'Completed', delivered_at = ?, delivery_proof_path = ? WHERE donation_id = ? AND volunteer_id = ?",
        (datetime.datetime.now(), proof_relative_path, donation_id, volunteer['id'])
    )
    conn.execute("UPDATE food_donations SET status = 'Delivered' WHERE id = ?", (donation_id,))
    conn.execute(
        "UPDATE pickup_assignments SET status = 'Delivered', completed_at = ? WHERE donation_id = ? AND volunteer_id = ?",
        (datetime.datetime.now(), donation_id, volunteer['id'])
    )
    _record_lifecycle_activity(conn, 'food', donation_id, 'Distribution Completed', 'Delivered')
    conn.commit()
    
    donation = conn.execute(
        """SELECT d.food_name, d.quantity, d.address, d.donor_id, fd.ngo_id,
                  dn.company_name AS donor_name, n.ngo_name AS ngo_name
           FROM food_donations d
           JOIN food_distribution fd ON d.id = fd.donation_id
           JOIN donors dn ON d.donor_id = dn.id
           JOIN ngos n ON fd.ngo_id = n.id
           WHERE d.id = ?""",
        (donation_id,)
    ).fetchone()

    # Generate an official delivery certificate once per donation (safe helper).
    try:
        created = _ensure_certificate_for_delivery(conn, donation_id, volunteer_id=volunteer['id'])
        if created:
            print(f"[VOL_DELIVER] Certificate created for donation {donation_id}")
    except Exception as e:
        print(f"[VOL_DELIVER] Failed to create certificate for donation {donation_id}: {e}")

    donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
    ngo_user = conn.execute("SELECT user_id FROM ngos WHERE id = ?", (donation['ngo_id'],)).fetchone()
    
    Notification.create(donor_user['user_id'], f"Your surplus food donation: '{donation['food_name']}' has been delivered to the NGO!")
    Notification.create(ngo_user['user_id'], f"Volunteer has successfully delivered the donation '{donation['food_name']}' to your premises. Deliver proof uploaded!")
    
    conn.close()
    flash('Congratulations! You successfully delivered the food. Delivery proof uploaded.', 'success')
    return redirect(url_for('volunteer_dashboard'))

@app.route('/volunteer/history')
@login_required
def volunteer_history():
    if current_user.role != 'volunteer':
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
    history = conn.execute(
        """SELECT fd.*, d.created_at, d.food_name, d.quantity, d.address, d.image_path, n.ngo_name, n.organization_type,
              d.inspection_status, d.inspection_result, d.inspection_completed_at,
              f.rating, f.food_rating, f.delivery_rating, f.comments, c.certificate_path
               , fi.inspected_at AS inspection_at, fi.notes AS inspection_notes, fi.rejection_reason
           FROM food_distribution fd
           JOIN food_donations d ON fd.donation_id = d.id
           JOIN ngos n ON fd.ngo_id = n.id
           LEFT JOIN food_inspections fi ON fd.donation_id = fi.donation_id AND fi.volunteer_id = ?
           LEFT JOIN feedback f ON f.donation_id = fd.donation_id
           LEFT JOIN certificates c ON c.donation_id = fd.donation_id
           WHERE fd.volunteer_id = ? ORDER BY fd.delivered_at DESC""",
        (volunteer['id'], volunteer['id'])
    ).fetchall()
    conn.close()
    return render_template('volunteer_history.html', history=history)


# --- API ENDPOINTS, QR SCANNER & MAPS ---

@app.route('/api/nearby-donations')
def api_nearby_donations():
    """Returns JSON coordinates of active pending donations for Map rendering."""
    conn = get_db_connection()
    donations = conn.execute(
        """SELECT d.id, d.food_name, d.category, d.quantity, d.latitude, d.longitude, d.address, dn.company_name
           FROM food_donations d
           JOIN donors dn ON d.donor_id = dn.id
           WHERE d.status = 'Pending'"""
    ).fetchall()
    conn.close()
    
    results = []
    for d in donations:
        results.append({
            'id': d['id'],
            'food_name': d['food_name'],
            'category': d['category'],
            'quantity': d['quantity'],
            'latitude': d['latitude'] if d['latitude'] else 12.9716,
            'longitude': d['longitude'] if d['longitude'] else 77.5946,
            'address': d['address'],
            'donor': d['company_name']
        })
    return jsonify(results)


@app.route('/api/leaderboard')
def api_leaderboard():
    """Public leaderboard: top donors and top volunteers (no auth required)."""
    conn = get_db_connection()
    try:
        top_donors = conn.execute(
            """SELECT dn.company_name AS name, COUNT(d.id) AS delivered_count
               FROM food_donations d
               JOIN donors dn ON d.donor_id = dn.id
               WHERE d.status = 'Delivered'
               GROUP BY dn.id
               ORDER BY delivered_count DESC
               LIMIT 10"""
        ).fetchall()

        top_volunteers = conn.execute(
            """SELECT v.name AS name, COUNT(pa.id) AS deliveries
               FROM pickup_assignments pa
               JOIN volunteers v ON pa.volunteer_id = v.id
               WHERE pa.status = 'Delivered'
               GROUP BY v.id
               ORDER BY deliveries DESC
               LIMIT 10"""
        ).fetchall()

        donors = [dict(row) for row in top_donors]
        volunteers = [dict(row) for row in top_volunteers]
        return jsonify({'success': True, 'top_donors': donors, 'top_volunteers': volunteers})
    finally:
        conn.close()

@app.route('/api/scan-qr', methods=['POST'])
@login_required
def api_scan_qr():
    """Scans and parses the donation verification QR code."""
    data = request.json
    qr_content = data.get('qr_content')
    
    if not qr_content or not qr_content.startswith("FEEDFORWARD_DONATION_ID:"):
        return jsonify({'error': 'Invalid QR Code Format.'}), 400
        
    try:
        donation_id = int(qr_content.split(":")[1])
    except (IndexError, ValueError):
        return jsonify({'error': 'Malformed QR Code Content.'}), 400
        
    conn = get_db_connection()
    donation = conn.execute("SELECT * FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
    
    if not donation:
        conn.close()
        return jsonify({'error': 'Donation record not found.'}), 404
        
    # Check assignment matching current volunteer
    if current_user.role == 'volunteer':
        volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
        dist = conn.execute("SELECT * FROM food_distribution WHERE donation_id = ? AND volunteer_id = ?", (donation_id, volunteer['id'])).fetchone()
        
        if not dist:
            conn.close()
            return jsonify({'error': 'This donation is not assigned to you.'}), 403
            
        # Perform action depending on current state
        if dist['status'] == 'Volunteer Assigned':
            # Check for approved inspection
            volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
            inspection = conn.execute("SELECT * FROM food_inspections WHERE donation_id = ? AND volunteer_id = ? ORDER BY inspected_at DESC LIMIT 1", (donation_id, volunteer['id'])).fetchone()
            if not inspection or inspection['status'] != 'Approved' or donation['inspection_status'] != 'Inspection Completed' or donation['inspection_result'] != 'Approved':
                conn.close()
                return jsonify({'error': 'Inspection required and must be Approved before pickup. Visit the Inspect Food page.'}), 403

            # Perform pickup
            conn.execute("UPDATE food_distribution SET status = 'Picked Up', picked_up_at = ? WHERE donation_id = ?", (datetime.datetime.now(), donation_id))
            conn.execute("UPDATE food_donations SET status = 'Picked Up' WHERE id = ?", (donation_id,))
            conn.execute("UPDATE pickup_assignments SET status = 'Picked Up' WHERE donation_id = ?", (donation_id,))
            conn.commit()

            # Notify
            donor_user = conn.execute("SELECT user_id FROM donors WHERE id = ?", (donation['donor_id'],)).fetchone()
            Notification.create(donor_user['user_id'], f"Volunteer has scanned and picked up your surplus food donation: {donation['food_name']}.")
            conn.close()
            return jsonify({'success': True, 'message': 'QR code scanned! Food pickup verified successfully.'})
            
        elif dist['status'] == 'Picked Up':
            conn.close()
            return jsonify({'success': True, 'message': 'Food is already picked up. Proceed to target NGO for delivery!'})
            
    conn.close()
    return jsonify({'error': 'Method not applicable for your current user role.'}), 400


# --- REPORTS & EXPORTS ---

@app.route('/reports/export/<string:file_format>')
@login_required
def export_reports(file_format):
    """Generates PDF or Excel format reports for the authenticated role."""
    conn = get_db_connection()
    
    # Query data depending on user role
    if current_user.role == 'admin':
        # All donations
        donations = conn.execute(
            """SELECT d.*, dn.company_name as food_name, d.food_name as food_name FROM food_donations d
               JOIN donors dn ON d.donor_id = dn.id ORDER BY d.created_at DESC"""
        ).fetchall()
    elif current_user.role == 'donor':
        donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
        donations = conn.execute("SELECT * FROM food_donations WHERE donor_id = ? ORDER BY created_at DESC", (donor['id'],)).fetchall()
    elif current_user.role == 'ngo':
        ngo = conn.execute("SELECT id FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
        donations = conn.execute(
            """SELECT d.* FROM food_donations d 
               JOIN food_distribution fd ON d.id = fd.donation_id 
               WHERE fd.ngo_id = ? ORDER BY d.created_at DESC""",
            (ngo['id'],)
        ).fetchall()
    else:
        # Volunteer
        volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
        donations = conn.execute(
            """SELECT d.* FROM food_donations d 
               JOIN food_distribution fd ON d.id = fd.donation_id 
               WHERE fd.volunteer_id = ? ORDER BY d.created_at DESC""",
            (volunteer['id'],)
        ).fetchall()
        
    conn.close()
    
    # Convert Row objects to raw dicts for utils
    donations_list = [dict(row) for row in donations]
    
    if file_format == 'excel':
        buffer = export_donations_to_excel(donations_list)
        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"FeedForward_Report_{datetime.date.today()}.xlsx"
        )
    elif file_format == 'pdf':
        buffer = export_donations_to_pdf(donations_list)
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"FeedForward_Report_{datetime.date.today()}.pdf"
        )
        
    flash('Invalid report format selected.', 'danger')
    return redirect(url_for('dashboard_router'))

@app.route('/certificate/download/<int:donation_id>')
@login_required
def download_certificate(donation_id):
    donation_type = (request.args.get('donation_type') or 'food').lower()
    conn = get_db_connection()
    if donation_type in ('clothes', 'books', 'money'):
        cert = conn.execute(
            "SELECT * FROM donation_certificates WHERE donation_type = ? AND donation_id = ?",
            (donation_type, donation_id),
        ).fetchone()
    else:
        cert = conn.execute("SELECT * FROM certificates WHERE donation_id = ?", (donation_id,)).fetchone()
    if not cert:
        conn.close()
        flash('Certificate not found for this delivery.', 'danger')
        return redirect(url_for('dashboard_router'))

    authorized = False
    if current_user.role == 'donor':
        donor = conn.execute("SELECT id FROM donors WHERE user_id = ?", (current_user.id,)).fetchone()
        authorized = bool(donor and cert['donor_id'] == donor['id'])
    elif current_user.role == 'volunteer':
        volunteer = conn.execute("SELECT id FROM volunteers WHERE user_id = ?", (current_user.id,)).fetchone()
        authorized = bool(volunteer and cert['volunteer_id'] == volunteer['id'])
    elif current_user.role == 'ngo':
        ngo = conn.execute("SELECT id FROM ngos WHERE user_id = ?", (current_user.id,)).fetchone()
        authorized = bool(ngo and cert['ngo_id'] == ngo['id'])
    elif current_user.role == 'admin':
        authorized = True

    if not authorized:
        conn.close()
        flash('You are not authorized to download this certificate.', 'danger')
        return redirect(url_for('dashboard_router'))

    certificate_path = cert['certificate_path']
    if donation_type in ('clothes', 'books', 'money'):
        # Regenerate stored non-Food certificates so older N/A placeholders are corrected.
        conn.execute(
            "DELETE FROM donation_certificates WHERE donation_type = ? AND donation_id = ?",
            (donation_type, donation_id),
        )
        _ensure_non_food_certificate(conn, donation_type, donation_id)
        refreshed_cert = conn.execute(
            "SELECT certificate_path FROM donation_certificates WHERE donation_type = ? AND donation_id = ?",
            (donation_type, donation_id),
        ).fetchone()
        if refreshed_cert:
            certificate_path = refreshed_cert['certificate_path']
        certificate_file = certificate_path.replace('uploads/', '')
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], certificate_file)
        conn.close()
        if not os.path.exists(file_path):
            flash('Certificate file is missing from the server.', 'danger')
            return redirect(url_for('dashboard_router'))
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))
    try:
        donation = conn.execute(
            """SELECT d.*, dn.company_name AS donor_name, n.ngo_name, v.name AS volunteer_name
               FROM food_donations d
               JOIN donors dn ON d.donor_id = dn.id
               LEFT JOIN food_distribution fd ON d.id = fd.donation_id
               LEFT JOIN ngos n ON fd.ngo_id = n.id
               LEFT JOIN volunteers v ON fd.volunteer_id = v.id
               WHERE d.id = ?""",
            (donation_id,),
        ).fetchone()
        if donation:
            certificate_path = generate_delivery_certificate(
                donation_id,
                dict(donation),
                donation['donor_name'] or '',
                donation['ngo_name'] or '',
                donation['volunteer_name'] or '',
            )
            conn.execute(
                "UPDATE certificates SET certificate_path = ? WHERE donation_id = ?",
                (certificate_path, donation_id),
            )
            conn.commit()
    except Exception:
        app.logger.exception('Unable to regenerate certificate %s; using existing file.', donation_id)

    certificate_file = certificate_path.replace('uploads/', '')
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], certificate_file)
    conn.close()
    if not os.path.exists(file_path):
        flash('Certificate file is missing from the server.', 'danger')
        return redirect(url_for('dashboard_router'))

    return send_file(
        file_path,
        as_attachment=True,
        download_name=os.path.basename(file_path)
    )


# --- NOTIFICATIONS SYSTEM ---

@app.route('/notifications')
@login_required
def view_notifications():
    conn = get_db_connection()
    notifications = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC", 
        (current_user.id,)
    ).fetchall()
    conn.close()
    return render_template('notifications.html', notifications=notifications)

@app.route('/notifications/read-all', methods=['POST'])
@login_required
def notifications_read_all():
    Notification.mark_all_read(current_user.id)
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('view_notifications'))


# --- SEARCH API ENDPOINTS ---

@app.route('/api/search/donor', methods=['GET'])
@login_required
def api_search_donor():
    """Role-based search for Donor Dashboard."""
    if current_user.role != 'donor':
        return jsonify({'error': 'Unauthorized'}), 403
    
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    status = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    
    if not query:
        return jsonify({'error': 'Search query required', 'results': []})
    
    results = SearchHelper.donor_search(current_user.id, query, category, status, date_from, date_to)
    results_list = [dict(row) for row in results]
    
    return jsonify({
        'success': True,
        'query': query,
        'count': len(results_list),
        'results': results_list
    })


@app.route('/api/search/ngo', methods=['GET'])
@login_required
def api_search_ngo():
    """Role-based search for NGO Dashboard."""
    if current_user.role != 'ngo':
        return jsonify({'error': 'Unauthorized'}), 403
    
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    status = request.args.get('status', '').strip()
    veg_non_veg = request.args.get('veg_non_veg', '').strip()
    donation_type = request.args.get('donation_type', '').strip()
    
    if not query:
        return jsonify({'error': 'Search query required', 'results': []})
    
    results = SearchHelper.ngo_search(current_user.id, query, category, status, veg_non_veg)
    results_list = [dict(row) for row in results]
    
    return jsonify({
        'success': True,
        'query': query,
        'count': len(results_list),
        'results': results_list
    })


@app.route('/api/search/volunteer', methods=['GET'])
@login_required
def api_search_volunteer():
    """Role-based search for Volunteer Dashboard."""
    if current_user.role != 'volunteer':
        return jsonify({'error': 'Unauthorized'}), 403
    
    query = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    veg_non_veg = request.args.get('veg_non_veg', '').strip()
    
    if not query:
        return jsonify({'error': 'Search query required', 'results': []})
    
    results = SearchHelper.volunteer_search(current_user.id, query, status, veg_non_veg)
    results_list = [dict(row) for row in results]
    
    return jsonify({
        'success': True,
        'query': query,
        'count': len(results_list),
        'results': results_list
    })


@app.route('/api/search/admin', methods=['GET'])
@login_required
def api_search_admin():
    """Global search for Admin Dashboard."""
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    query = request.args.get('q', '').strip()
    search_type = request.args.get('type', 'donations').strip()
    
    if search_type not in ['users', 'donors', 'ngos', 'volunteers', 'donations', 'emergencies', 'feedback', 'notifications']:
        return jsonify({'error': 'Invalid search type'}), 400
    
    # Build filters dictionary for admin search
    filters = {}
    category = request.args.get('category', '').strip()
    status = request.args.get('status', '').strip()
    veg_non_veg = request.args.get('veg_non_veg', '').strip()
    donation_type = request.args.get('donation_type', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    
    if category:
        filters['category'] = category
    if status:
        filters['status'] = status
    if veg_non_veg:
        filters['veg_non_veg'] = veg_non_veg
    if donation_type:
        filters['donation_type'] = donation_type
    if date_from:
        filters['date_from'] = date_from
    if date_to:
        filters['date_to'] = date_to

    if not query and not (search_type == 'donations' and filters):
        return jsonify({'error': 'Enter a query or select a donation filter.', 'results': []})
    
    results = SearchHelper.admin_search(search_type, query, filters if filters else None)
    results_list = [dict(row) for row in results]
    
    return jsonify({
        'success': True,
        'search_type': search_type,
        'query': query,
        'count': len(results_list),
        'results': results_list
    })


# --- RUN APPLICATION ---

if __name__ == '__main__':
    # Initialize DB Schema on launch
    init_db()
    # Port must be 3000 to bind externally in this sandbox environment
    app.run(host='0.0.0.0', port=3000, debug=True)
