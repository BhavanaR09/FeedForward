import time
import unittest

from app import app
from database import get_db_connection, init_db


class ExpiryPerformanceTests(unittest.TestCase):
    def test_expiry_scan_is_rate_limited_when_recently_checked(self):
        init_db()
        conn = get_db_connection()
        try:
            donor = conn.execute("SELECT id FROM donors LIMIT 1").fetchone()
            if donor is None:
                conn.execute(
                    "INSERT INTO donors (user_id, company_name, contact_name, phone, address) VALUES (1, 'Test Donor', 'Test', '9000000001', 'Test Address')"
                )
                conn.commit()
                donor = conn.execute("SELECT id FROM donors WHERE user_id = 1").fetchone()

            conn.execute(
                "INSERT INTO food_donations (donor_id, food_name, category, veg_non_veg, quantity, expiry_time, pickup_time, address, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending')",
                (donor['id'], 'Past Donation', 'Cooked Food', 'Veg', '5 kg', '2020-01-01 12:00:00', 'Today 6 PM', 'Test Address')
            )
            conn.commit()
            donation_id = conn.execute("SELECT id FROM food_donations WHERE food_name = 'Past Donation' ORDER BY id DESC LIMIT 1").fetchone()['id']

            app._LAST_EXPIRY_CHECK = time.monotonic()
            result = app._auto_expire_donations(conn=conn, force=False)

            self.assertEqual(result, [])
            row = conn.execute("SELECT status FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
            self.assertEqual(row['status'], 'Pending')
        finally:
            conn.close()
