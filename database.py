import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from config import Config

def get_db_connection():
    """Establish a connection to the SQLite database and return it with Row factory enabled."""
    conn = sqlite3.connect(Config.DATABASE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Enable Foreign Key constraints
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def ensure_food_distribution_status_constraint():
    """Ensure food_distribution accepts the Expired state used by expiry logic."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='food_distribution'").fetchone()
        if row is None or row['sql'] is None:
            return

        table_sql = row['sql']
        if "'Expired'" in table_sql:
            return

        new_sql = table_sql.replace(
            "status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Accepted', 'Volunteer Assigned', 'Picked Up', 'Delivered', 'Completed', 'Cancelled'))",
            "status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Accepted', 'Volunteer Assigned', 'Picked Up', 'Delivered', 'Completed', 'Cancelled', 'Expired'))"
        )

        if new_sql == table_sql:
            return

        cursor = conn.cursor()
        cursor.execute("ALTER TABLE food_distribution RENAME TO food_distribution_old")
        cursor.execute(new_sql.replace("CREATE TABLE food_distribution", "CREATE TABLE food_distribution"))
        cursor.execute("INSERT INTO food_distribution SELECT * FROM food_distribution_old")
        cursor.execute("DROP TABLE food_distribution_old")
        conn.commit()
        print("Updated food_distribution status constraint to include 'Expired'.")
    finally:
        conn.close()


def ensure_food_distribution_columns():
    """Ensure the food_distribution table exists and contains required timestamp columns."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='food_distribution'")
        if not cursor.fetchone():
            init_db()
            return

        cursor.execute("PRAGMA table_info(food_distribution)")
        distribution_cols = [col[1] for col in cursor.fetchall()]
        if 'assigned_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN assigned_at TIMESTAMP")
            conn.commit()
            print("Added 'assigned_at' column to 'food_distribution' table.")
        if 'picked_up_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN picked_up_at TIMESTAMP")
            conn.commit()
            print("Added 'picked_up_at' column to 'food_distribution' table.")
        if 'delivered_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN delivered_at TIMESTAMP")
            conn.commit()
            print("Added 'delivered_at' column to 'food_distribution' table.")
    finally:
        conn.close()

    ensure_food_distribution_status_constraint()


def init_db():
    """Initialize the SQLite database schema if not already initialized."""
    schema = """
    -- Users Table
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        full_name TEXT,
        email TEXT NOT NULL,
        email_verified INTEGER NOT NULL DEFAULT 0,
        phone TEXT,
        address TEXT,
        profile_image_path TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'donor', 'ngo', 'volunteer')),
        status TEXT NOT NULL DEFAULT 'active', -- active, pending, verified, approved, suspended
        last_login TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- User Roles Table
    CREATE TABLE IF NOT EXISTS user_roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'donor', 'ngo', 'volunteer')),
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, role),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS email_otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        otp TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        used INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_email_otps_user_email ON email_otps(user_id, email, used, verified);

    -- Admins Table
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Donors Table
    CREATE TABLE IF NOT EXISTS donors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        company_name TEXT NOT NULL,
        contact_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        organization_type TEXT,
        is_approved INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- NGOs Table
    CREATE TABLE IF NOT EXISTS ngos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        ngo_name TEXT NOT NULL,
        registration_number TEXT NOT NULL UNIQUE,
        contact_person TEXT NOT NULL,
        phone TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        organization_type TEXT,
        is_verified INTEGER DEFAULT 0, -- 0 = Pending/No, 1 = Verified/Yes
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Volunteers Table
    CREATE TABLE IF NOT EXISTS volunteers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        is_approved INTEGER DEFAULT 0, -- 0 = Pending/No, 1 = Approved/Yes
        transport_type TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Food Donations Table
    CREATE TABLE IF NOT EXISTS food_donations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donor_id INTEGER NOT NULL,
        food_name TEXT NOT NULL,
        category TEXT NOT NULL, -- Cooked Food, Packaged Food, Raw Ingredients, Groceries
        veg_non_veg TEXT NOT NULL CHECK(veg_non_veg IN ('Veg', 'Non-Veg')),
        quantity TEXT NOT NULL,
        expiry_time TEXT NOT NULL,
        pickup_time TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        image_path TEXT,
        qr_code_path TEXT,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Accepted', 'Volunteer Assigned', 'Picked Up', 'Delivered', 'Cancelled', 'Expired')),
        inspection_status TEXT NOT NULL DEFAULT 'Pending Inspection' CHECK(inspection_status IN ('Pending Inspection', 'Inspection Completed')),
        inspection_result TEXT CHECK(inspection_result IN ('Approved', 'Rejected')),
        inspected_by INTEGER,
        inspection_completed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE,
        FOREIGN KEY (inspected_by) REFERENCES volunteers(id) ON DELETE SET NULL
    );

    -- Orphanage Support Table
    CREATE TABLE IF NOT EXISTS orphanages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact_person TEXT,
        phone TEXT,
        email TEXT,
        address TEXT,
        location TEXT,
        is_approved INTEGER NOT NULL DEFAULT 0,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
    );

    -- Clothes Donations Table
    CREATE TABLE IF NOT EXISTS clothes_donations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donor_id INTEGER NOT NULL,
        item_type TEXT NOT NULL,
        category TEXT NOT NULL,
        size TEXT NOT NULL,
        quantity TEXT NOT NULL,
        condition TEXT NOT NULL,
        pickup_time TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        image_path TEXT,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Scheduled', 'Picked Up', 'Delivered', 'Cancelled')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE
    );

    -- Book Donations Table
    CREATE TABLE IF NOT EXISTS book_donations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donor_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        category TEXT NOT NULL,
        quantity TEXT NOT NULL,
        condition TEXT NOT NULL,
        pickup_time TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        image_path TEXT,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Scheduled', 'Picked Up', 'Delivered', 'Cancelled')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE
    );

    -- Money Donations for Orphans Table
    CREATE TABLE IF NOT EXISTS money_donations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donor_id INTEGER NOT NULL,
        orphanage_id INTEGER,
        amount REAL NOT NULL,
        purpose TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Paid', 'Cancelled')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE CASCADE,
        FOREIGN KEY (orphanage_id) REFERENCES orphanages(id) ON DELETE SET NULL
    );

    -- Volunteer assignments for non-food donations
    CREATE TABLE IF NOT EXISTS donation_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_type TEXT NOT NULL CHECK(donation_type IN ('clothes', 'books')),
        donation_id INTEGER NOT NULL,
        ngo_id INTEGER NOT NULL,
        volunteer_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Assigned',
        assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(donation_type, donation_id),
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE CASCADE,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE CASCADE
    );

    -- Food Distribution & Assignment Table
    CREATE TABLE IF NOT EXISTS food_distribution (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_id INTEGER UNIQUE NOT NULL,
        ngo_id INTEGER,
        volunteer_id INTEGER,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Accepted', 'Volunteer Assigned', 'Picked Up', 'Delivered', 'Completed', 'Cancelled')),
        distribution_notes TEXT,
        delivery_proof_path TEXT,
        assigned_at TIMESTAMP,
        picked_up_at TIMESTAMP,
        delivered_at TIMESTAMP,
        FOREIGN KEY (donation_id) REFERENCES food_donations(id) ON DELETE CASCADE,
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE SET NULL,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE SET NULL
    );

    -- Pickup Assignments Table (Explicitly for timeline and historic pickup tracking)
    CREATE TABLE IF NOT EXISTS pickup_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_id INTEGER NOT NULL,
        ngo_id INTEGER NOT NULL,
        volunteer_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Assigned' CHECK(status IN ('Assigned', 'Picked Up', 'Delivered', 'Cancelled')),
        assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (donation_id) REFERENCES food_donations(id) ON DELETE CASCADE,
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE CASCADE,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE CASCADE
    );

    -- Notifications Table
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        is_read INTEGER DEFAULT 0, -- 0 = Unread, 1 = Read
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Notification Email Delivery Log
    CREATE TABLE IF NOT EXISTS notification_email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event_key TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'sent', -- sent, failed
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, event_key),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Feedback Table
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        donation_id INTEGER NOT NULL,
        rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
        food_rating INTEGER,
        delivery_rating INTEGER,
        comments TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (donation_id) REFERENCES food_donations(id) ON DELETE CASCADE
    );

    -- Platform Feedback Table
    CREATE TABLE IF NOT EXISTS platform_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
        comments TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Contact Messages Table
    CREATE TABLE IF NOT EXISTS contact_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT,
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Unread' CHECK(status IN ('Unread', 'Read')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS message_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_message_id INTEGER NOT NULL,
        admin_user_id INTEGER,
        recipient_email TEXT NOT NULL,
        subject TEXT NOT NULL,
        reply_message TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('sent', 'failed')),
        sent_at TIMESTAMP,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (contact_message_id) REFERENCES contact_messages(id) ON DELETE CASCADE,
        FOREIGN KEY (admin_user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    -- Emergency Alerts Table
    CREATE TABLE IF NOT EXISTS emergencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        disaster_type TEXT NOT NULL,
        location TEXT NOT NULL,
        severity TEXT NOT NULL CHECK(severity IN ('Low', 'Medium', 'High', 'Critical')),
        details TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
    );

    -- Emergency Mode Requests Table
    CREATE TABLE IF NOT EXISTS emergency_mode_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected')),
        reviewed_by INTEGER,
        reviewed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE SET NULL
    );

    -- Application Settings Table
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Reports Table (Stores generated report references)
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        report_type TEXT NOT NULL, -- Donation, NGO, Volunteer, Inventory
        format TEXT NOT NULL, -- PDF, Excel
        file_path TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
    );

    -- Certificates Table (Delivery completion certificates)
    CREATE TABLE IF NOT EXISTS certificates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_id INTEGER UNIQUE NOT NULL,
        volunteer_id INTEGER,
        donor_id INTEGER,
        ngo_id INTEGER,
        certificate_path TEXT NOT NULL,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (donation_id) REFERENCES food_donations(id) ON DELETE CASCADE,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE SET NULL,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE SET NULL,
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE SET NULL
    );

    -- Category-neutral lifecycle records. Donation IDs are scoped by donation_type.
    CREATE TABLE IF NOT EXISTS donation_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_type TEXT NOT NULL CHECK(donation_type IN ('food', 'clothes', 'books', 'money')),
        donation_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        status TEXT,
        actor_user_id INTEGER,
        actor_role TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(donation_type, donation_id, event, status),
        FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS donation_distribution (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_type TEXT NOT NULL CHECK(donation_type IN ('clothes', 'books', 'money')),
        donation_id INTEGER NOT NULL,
        ngo_id INTEGER,
        volunteer_id INTEGER,
        status TEXT NOT NULL DEFAULT 'Pending',
        delivery_proof_path TEXT,
        assigned_at TIMESTAMP,
        picked_up_at TIMESTAMP,
        delivered_at TIMESTAMP,
        UNIQUE(donation_type, donation_id),
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE SET NULL,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS donation_certificates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_type TEXT NOT NULL CHECK(donation_type IN ('food', 'clothes', 'books', 'money')),
        donation_id INTEGER NOT NULL,
        volunteer_id INTEGER,
        donor_id INTEGER,
        ngo_id INTEGER,
        certificate_path TEXT NOT NULL,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(donation_type, donation_id),
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE SET NULL,
        FOREIGN KEY (donor_id) REFERENCES donors(id) ON DELETE SET NULL,
        FOREIGN KEY (ngo_id) REFERENCES ngos(id) ON DELETE SET NULL
    );
    -- Food Inspections Table
    CREATE TABLE IF NOT EXISTS food_inspections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        donation_id INTEGER NOT NULL,
        volunteer_id INTEGER NOT NULL,
        inspected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        appearance_ok INTEGER NOT NULL CHECK(appearance_ok IN (0,1)),
        packaging_intact INTEGER NOT NULL CHECK(packaging_intact IN (0,1)),
        within_expiry INTEGER NOT NULL CHECK(within_expiry IN (0,1)),
        storage_ok INTEGER NOT NULL CHECK(storage_ok IN (0,1)),
        suitable INTEGER NOT NULL CHECK(suitable IN (0,1)),
        notes TEXT,
        status TEXT NOT NULL CHECK(status IN ('Approved','Rejected')),
        rejection_reason TEXT,
        FOREIGN KEY (donation_id) REFERENCES food_donations(id) ON DELETE CASCADE,
        FOREIGN KEY (volunteer_id) REFERENCES volunteers(id) ON DELETE CASCADE
    );
    """
    
    conn = get_db_connection()
    try:
        conn.executescript(schema)
        conn.commit()
        ensure_food_distribution_status_constraint()
        
        # Check and add columns dynamically if they do not exist
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(users)")
        user_cols = [col[1] for col in cursor.fetchall()]
        if 'full_name' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
            conn.commit()
            print("Added 'full_name' column to 'users' table.")
        if 'phone' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            conn.commit()
            print("Added 'phone' column to 'users' table.")
        if 'profile_image_path' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN profile_image_path TEXT")
            conn.commit()
            print("Added 'profile_image_path' column to 'users' table.")
        if 'last_login' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN last_login TIMESTAMP")
            conn.commit()
            print("Added 'last_login' column to 'users' table.")
        if 'address' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN address TEXT")
            conn.commit()
            print("Added 'address' column to 'users' table.")
        if 'email_verified' not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            print("Added 'email_verified' column to 'users' table.")

        # Old databases may have an email uniqueness constraint that blocks shared-email multi-role registration.
        cursor.execute("PRAGMA index_list(users)")
        user_indexes = cursor.fetchall()
        needs_user_table_rebuild = False
        for index_row in user_indexes:
            index_name = index_row[1]
            is_unique = bool(index_row[2])
            if not is_unique:
                continue
            cursor.execute(f"PRAGMA index_info('{index_name}')")
            index_cols = [info_row[2] for info_row in cursor.fetchall()]
            if 'email' in index_cols:
                needs_user_table_rebuild = True
                break

        if needs_user_table_rebuild:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("""
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    full_name TEXT,
                    email TEXT NOT NULL,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    phone TEXT,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'donor', 'ngo', 'volunteer')),
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "INSERT INTO users_new (id, username, full_name, email, email_verified, phone, password_hash, role, status, created_at) SELECT id, username, full_name, email, email_verified, phone, password_hash, role, status, created_at FROM users"
            )
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_new RENAME TO users")
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            print("Recreated users table without the legacy email uniqueness constraint.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_roles'")
        if not cursor.fetchone():
            cursor.execute(
                "CREATE TABLE user_roles (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin', 'donor', 'ngo', 'volunteer')), status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, role), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)"
            )
            conn.commit()
            print("Created 'user_roles' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_phone'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_email'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_food_donations_status_expiry'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_food_donations_status_expiry ON food_donations(status, expiry_time)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_clothes_donations_status'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clothes_donations_status ON clothes_donations(status, created_at)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_book_donations_status'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_donations_status ON book_donations(status, created_at)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_money_donations_status'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_money_donations_status ON money_donations(status, created_at)")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_notifications_user_unread'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read, created_at DESC)")
            conn.commit()

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_donation_activity_lookup ON donation_activity(donation_type, donation_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_donation_distribution_volunteer ON donation_distribution(volunteer_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_donation_certificates_donor ON donation_certificates(donor_id, issued_at DESC)")
        conn.commit()

        cursor.execute("PRAGMA table_info(donors)")
        donor_cols = [col[1] for col in cursor.fetchall()]
        if 'organization_type' not in donor_cols:
            cursor.execute("ALTER TABLE donors ADD COLUMN organization_type TEXT")
            conn.commit()
            print("Added 'organization_type' column to 'donors' table.")
        if 'is_approved' not in donor_cols:
            cursor.execute("ALTER TABLE donors ADD COLUMN is_approved INTEGER DEFAULT 0")
            conn.commit()
            print("Added 'is_approved' column to 'donors' table.")

        cursor.execute("PRAGMA table_info(ngos)")
        ngo_cols = [col[1] for col in cursor.fetchall()]
        if 'organization_type' not in ngo_cols:
            cursor.execute("ALTER TABLE ngos ADD COLUMN organization_type TEXT")
            conn.commit()
            print("Added 'organization_type' column to 'ngos' table.")

        cursor.execute("PRAGMA table_info(volunteers)")
        volunteer_cols = [col[1] for col in cursor.fetchall()]
        if 'transport_type' not in volunteer_cols:
            cursor.execute("ALTER TABLE volunteers ADD COLUMN transport_type TEXT")
            conn.commit()
            print("Added 'transport_type' column to 'volunteers' table.")

        cursor.execute("PRAGMA table_info(food_distribution)")
        distribution_cols = [col[1] for col in cursor.fetchall()]
        if 'assigned_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN assigned_at TIMESTAMP")
            conn.commit()
            print("Added 'assigned_at' column to 'food_distribution' table.")
        if 'picked_up_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN picked_up_at TIMESTAMP")
            conn.commit()
            print("Added 'picked_up_at' column to 'food_distribution' table.")
        if 'delivered_at' not in distribution_cols:
            cursor.execute("ALTER TABLE food_distribution ADD COLUMN delivered_at TIMESTAMP")
            conn.commit()
            print("Added 'delivered_at' column to 'food_distribution' table.")

        cursor.execute("PRAGMA table_info(food_donations)")
        food_cols = [col[1] for col in cursor.fetchall()]
        if 'inspection_status' not in food_cols:
            cursor.execute("ALTER TABLE food_donations ADD COLUMN inspection_status TEXT NOT NULL DEFAULT 'Pending Inspection'")
        if 'inspection_result' not in food_cols:
            cursor.execute("ALTER TABLE food_donations ADD COLUMN inspection_result TEXT")
        if 'inspected_by' not in food_cols:
            cursor.execute("ALTER TABLE food_donations ADD COLUMN inspected_by INTEGER")
        if 'inspection_completed_at' not in food_cols:
            cursor.execute("ALTER TABLE food_donations ADD COLUMN inspection_completed_at TIMESTAMP")
        cursor.execute(
            """UPDATE food_donations
               SET inspection_status = 'Inspection Completed',
                   inspection_result = (SELECT fi.status FROM food_inspections fi WHERE fi.donation_id = food_donations.id ORDER BY fi.inspected_at DESC LIMIT 1),
                   inspected_by = (SELECT fi.volunteer_id FROM food_inspections fi WHERE fi.donation_id = food_donations.id ORDER BY fi.inspected_at DESC LIMIT 1),
                   inspection_completed_at = (SELECT fi.inspected_at FROM food_inspections fi WHERE fi.donation_id = food_donations.id ORDER BY fi.inspected_at DESC LIMIT 1)
               WHERE EXISTS (SELECT 1 FROM food_inspections fi WHERE fi.donation_id = food_donations.id)"""
        )
        conn.commit()

        cursor.execute("PRAGMA table_info(feedback)")
        feedback_cols = [col[1] for col in cursor.fetchall()]
        if 'food_rating' not in feedback_cols:
            cursor.execute("ALTER TABLE feedback ADD COLUMN food_rating INTEGER")
            conn.commit()
            print("Added 'food_rating' column to 'feedback' table.")
        if 'delivery_rating' not in feedback_cols:
            cursor.execute("ALTER TABLE feedback ADD COLUMN delivery_rating INTEGER")
            conn.commit()
            print("Added 'delivery_rating' column to 'feedback' table.")

        cursor.execute("PRAGMA table_info(money_donations)")
        money_cols = [col[1] for col in cursor.fetchall()]
        if 'razorpay_order_id' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN razorpay_order_id TEXT")
            conn.commit()
        if 'razorpay_payment_id' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN razorpay_payment_id TEXT")
            conn.commit()
        if 'razorpay_signature' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN razorpay_signature TEXT")
            conn.commit()
        if 'currency' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN currency TEXT NOT NULL DEFAULT 'INR'")
            conn.commit()
        if 'payment_status' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'Pending'")
            conn.commit()
        if 'donation_status' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN donation_status TEXT NOT NULL DEFAULT 'Pending'")
            conn.commit()
        if 'verified_at' not in money_cols:
            cursor.execute("ALTER TABLE money_donations ADD COLUMN verified_at TIMESTAMP")
            conn.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='platform_feedback'")
        if not cursor.fetchone():
            cursor.execute(
                "CREATE TABLE platform_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5), comments TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)"
            )
            conn.commit()
            print("Created 'platform_feedback' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contact_messages'")
        if not cursor.fetchone():
            cursor.execute(
                "CREATE TABLE contact_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT, subject TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Unread' CHECK(status IN ('Unread', 'Read')), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL)"
            )
            conn.commit()
            print("Created 'contact_messages' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='emergencies'")
        if not cursor.fetchone():
            cursor.execute(
                "CREATE TABLE emergencies (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, disaster_type TEXT NOT NULL, location TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('Low', 'Medium', 'High', 'Critical')), details TEXT NOT NULL, start_date TEXT, end_date TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL)"
            )
            conn.commit()
            print("Created 'emergencies' table.")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'")
        if not cursor.fetchone():
            cursor.execute(
                "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()
            print("Created 'app_settings' table.")
            cursor.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                ('enable_emergency_mode', '1')
            )
            conn.commit()
            print("Initialized 'enable_emergency_mode' setting.")

        cursor.execute("SELECT user_id, role FROM user_roles WHERE role = 'admin'")
        admin_roles = cursor.fetchall()
        if not admin_roles:
            cursor.execute("SELECT id FROM users WHERE username = 'admin'")
            admin_user_id = cursor.fetchone()
            if admin_user_id:
                cursor.execute("INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)", (admin_user_id[0], 'admin', 'active'))
                conn.commit()

        # Ensure a default Admin account exists without overwriting an existing password.
        cursor.execute("SELECT id, password_hash FROM users WHERE username = 'admin'")
        admin_user = cursor.fetchone()
        default_pw_hash = generate_password_hash('admin123')

        if not admin_user:
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, role, status) VALUES (?, ?, ?, ?, ?)",
                ('admin', 'admin@feedforward.com', default_pw_hash, 'admin', 'active')
            )
            user_id = cursor.lastrowid
            cursor.execute(
                "INSERT OR IGNORE INTO user_roles (user_id, role, status) VALUES (?, ?, ?)",
                (user_id, 'admin', 'active')
            )

            cursor.execute(
                "INSERT INTO admins (user_id, name, phone) VALUES (?, ?, ?)",
                (user_id, 'System Administrator', '+1234567890')
            )
            conn.commit()
            print("Database initialized with default admin user (username: admin, password: admin123)")
            
    except Exception as e:
        print(f"Error initializing database: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    init_db()
