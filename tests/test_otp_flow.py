import unittest
import uuid

from app import app, store_otp, verify_otp
from database import get_db_connection, init_db
from models import Donor, FoodDonation, NGO, User, Volunteer


class OtpFlowTests(unittest.TestCase):
    def test_verify_otp_works_across_new_request_contexts(self):
        with app.test_request_context('/'):
            store_otp('register_email', 'user@example.com', '123456', expires_minutes=5)

        with app.test_request_context('/'):
            import app as app_module
            app_module.otp_store.clear()
            self.assertTrue(verify_otp('register_email', 'user@example.com', '123456'))


class SharedEmailRoleTests(unittest.TestCase):
    def test_shared_email_can_register_multiple_roles_with_role_specific_usernames(self):
        init_db()
        email = f"shared-{uuid.uuid4().hex[:6]}@example.com"

        donor_id = User.create(username='shared_donor', email=email, password='Password1!', role='donor', status='active', full_name='Shared User', phone='9876543210')
        ngo_id = User.create(username='shared_ngo', email=email, password='Password1!', role='ngo', status='pending', full_name='Shared User', phone='9876543211')
        volunteer_id = User.create(username='shared_volunteer', email=email, password='Password1!', role='volunteer', status='active', full_name='Shared User', phone='9876543212')

        self.assertIsNotNone(donor_id)
        self.assertIsNotNone(ngo_id)
        self.assertIsNotNone(volunteer_id)
        self.assertIsNotNone(User.find_by_username_and_role('shared_donor', 'donor'))
        self.assertIsNotNone(User.find_by_username_and_role('shared_ngo', 'ngo'))
        self.assertIsNotNone(User.find_by_username_and_role('shared_volunteer', 'volunteer'))
        self.assertIsNone(User.find_by_username_and_role('shared_donor', 'ngo'))
        self.assertIsNotNone(User.find_by_email_and_role(email, 'donor'))
        self.assertIsNotNone(User.find_by_email_and_role(email, 'ngo'))
        self.assertIsNotNone(User.find_by_email_and_role(email, 'volunteer'))


class VolunteerAuthRegressionTests(unittest.TestCase):
    def test_volunteer_login_accepts_case_insensitive_username(self):
        init_db()
        username = f"volunteer_{uuid.uuid4().hex[:8]}"
        email = f"{username}@example.com"

        user_id = User.create(username=username, email=email, password='Password1!', role='volunteer', status='active', full_name='Volunteer User', phone='9876543219')
        self.assertIsNotNone(user_id)

        with app.test_client() as client:
            response = client.post('/login', data={
                'username': username.upper(),
                'user_type': 'volunteer',
                'password': 'Password1!'
            }, follow_redirects=False)

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get('Location'), '/dashboard')

    def test_unified_registration_uses_role_specific_generated_username(self):
        init_db()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['registration_email_verified'] = 'roleuser@example.com'

            response = client.post('/register', data={
                'full_name': 'Role User',
                'email': 'roleuser@example.com',
                'phone': '9876543220',
                'password': 'Password1!',
                'confirm_password': 'Password1!',
                'role': 'volunteer'
            })

            self.assertIn(response.status_code, (200, 302))
            saved = get_db_connection().execute("SELECT username FROM users WHERE email = ? AND role = 'volunteer'", ('roleuser@example.com',)).fetchone()
            self.assertIsNotNone(saved)
            self.assertTrue(saved['username'].lower().endswith('_volunteer'))


class PortfolioPageTests(unittest.TestCase):
    def test_volunteer_portfolio_page_loads_for_logged_in_user(self):
        init_db()
        username = f"portfolio_vol_{uuid.uuid4().hex[:8]}"
        email = f"{username}@example.com"
        user_id = User.create(username=username, email=email, password='Password1!', role='volunteer', status='active', full_name='Portfolio Volunteer', phone='9876543221')
        self.assertIsNotNone(user_id)
        Volunteer.create(user_id=user_id, name='Portfolio Volunteer', phone='9876543221', address='Test Street', is_approved=1)

        with app.test_client() as client:
            response = client.post('/login', data={
                'username': username,
                'user_type': 'volunteer',
                'password': 'Password1!'
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 302)

            portfolio_response = client.get('/portfolio')
            self.assertEqual(portfolio_response.status_code, 200)
            self.assertIn(b'Portfolio', portfolio_response.data)


class ContactUsFeatureTests(unittest.TestCase):
    def test_contact_form_persists_message_and_admin_page_lists_it(self):
        init_db()
        admin_username = f"admin_contact_{uuid.uuid4().hex[:8]}"
        admin_user_id = User.create(username=admin_username, email=f"{admin_username}@example.com", password='Password1!', role='admin', status='active', full_name='Admin User', phone='9876543230')
        self.assertIsNotNone(admin_user_id)

        with app.test_client() as client:
            response = client.post('/contact', data={
                'name': 'Test User',
                'email': 'user@example.com',
                'phone': '9876543231',
                'subject': 'Need help',
                'message': 'I would like to ask about donation pickup.'
            }, follow_redirects=False)
            self.assertIn(response.status_code, (200, 302))

            saved = get_db_connection().execute("SELECT * FROM contact_messages WHERE subject = ?", ('Need help',)).fetchone()
            self.assertIsNotNone(saved)
            self.assertEqual(saved['name'], 'Test User')

            admin_login = client.post('/admin/login', data={
                'username': admin_username,
                'password': 'Password1!'
            }, follow_redirects=False)
            self.assertEqual(admin_login.status_code, 302)

            admin_view = client.get('/admin/contact-messages')
            self.assertEqual(admin_view.status_code, 200)
            self.assertIn(b'Need help', admin_view.data)


class NgoOrganizationTypeTests(unittest.TestCase):
    def test_ngo_create_persists_organization_type(self):
        init_db()
        username = f"orgtest_{uuid.uuid4().hex[:8]}"
        user_id = User.create(username=username, email=f"{username}@example.com", password='Password1', role='ngo', status='pending')
        self.assertIsNotNone(user_id)

        NGO.create(
            user_id=user_id,
            ngo_name='Hope Home',
            registration_number=f"REG-{uuid.uuid4().hex[:8]}",
            contact_person='Test Contact',
            phone='+919999999999',
            address='Test Address',
            is_verified=0,
            organization_type='Orphanage'
        )

        conn = get_db_connection()
        saved = conn.execute("SELECT organization_type FROM ngos WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()

        self.assertIsNotNone(saved)
        self.assertEqual(saved['organization_type'], 'Orphanage')

    def test_unified_registration_persists_selected_ngo_organization_type(self):
        init_db()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['registration_email_verified'] = 'new-org@example.com'

            response = client.post('/register', data={
                'full_name': 'New Organization',
                'email': 'new-org@example.com',
                'phone': '9876543210',
                'password': 'Password1!',
                'confirm_password': 'Password1!',
                'role': 'ngo',
                'organization_type': 'Orphanage',
                'organization_type_other': ''
            })

            self.assertIn(response.status_code, (200, 302))

            conn = get_db_connection()
            saved = conn.execute("SELECT organization_type FROM ngos WHERE contact_person = ?", ('New Organization',)).fetchone()
            conn.close()

            self.assertIsNotNone(saved)
            self.assertEqual(saved['organization_type'], 'Orphanage')


class NgoAssignmentTableUiTests(unittest.TestCase):
    def test_ngo_assignments_uses_clear_deadline_labels(self):
        init_db()
        donor_username = f"donor_{uuid.uuid4().hex[:8]}"
        ngo_username = f"ngo_{uuid.uuid4().hex[:8]}"
        volunteer_username = f"volunteer_{uuid.uuid4().hex[:8]}"

        donor_user_id = User.create(username=donor_username, email=f"{donor_username}@example.com", password='Password1!', role='donor', status='active', full_name='Donor User', phone='9876543300')
        ngo_user_id = User.create(username=ngo_username, email=f"{ngo_username}@example.com", password='Password1!', role='ngo', status='active', full_name='NGO User', phone='9876543301')
        volunteer_user_id = User.create(username=volunteer_username, email=f"{volunteer_username}@example.com", password='Password1!', role='volunteer', status='active', full_name='Volunteer User', phone='9876543302')

        donor_id = Donor.create(donor_user_id, 'My Kitchen', 'Donor Contact', '9876543300', 'Donor Street')
        ngo_id = NGO.create(ngo_user_id, 'Food Care', 'REG-456', 'NGO Contact', '9876543301', 'NGO Street', is_verified=1)
        volunteer_id = Volunteer.create(volunteer_user_id, 'Volunteer User', '9876543302', 'Volunteer Street', is_approved=1)

        donation_id = FoodDonation.create(donor_id, 'Rice and Curry', 'Cooked Food', 'Veg', '10 servings', '2025-12-31 18:00', '2025-12-31 19:00', 'Donor Street')

        conn = get_db_connection()
        conn.execute("INSERT INTO food_distribution (donation_id, ngo_id, volunteer_id, status, assigned_at) VALUES (?, ?, ?, 'Accepted', CURRENT_TIMESTAMP)", (donation_id, ngo_id, volunteer_id))
        conn.execute("UPDATE food_donations SET status = 'Accepted' WHERE id = ?", (donation_id,))
        conn.commit()
        conn.close()

        with app.test_client() as client:
            login = client.post('/login', data={
                'username': ngo_username,
                'user_type': 'ngo',
                'password': 'Password1!'
            }, follow_redirects=False)
            self.assertEqual(login.status_code, 302)

            response = client.get('/ngo/assignments')
            self.assertEqual(response.status_code, 200)
            body = response.data.decode('utf-8')
            self.assertIn('Pickup Deadline', body)
            self.assertIn('Best Before', body)
            self.assertNotIn('Uploaded: N/A', body)
            self.assertNotIn('ETA', body)


class DonationCategoryModuleTests(unittest.TestCase):
    def test_homepage_includes_donation_category_cards(self):
        with app.test_client() as client:
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            body = response.data.decode('utf-8')
            self.assertIn('Food Donation', body)
            self.assertIn('Clothes Donation', body)
            self.assertIn('Book Donation', body)
            self.assertIn('Money Donation for Orphans', body)

    def test_new_donation_tables_exist(self):
        init_db()
        conn = get_db_connection()
        names = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        for table in ['clothes_donations', 'book_donations', 'money_donations', 'orphanages']:
            self.assertIn(table, names)


class VolunteerInspectionWorkflowTests(unittest.TestCase):
    def test_approved_inspection_does_not_mark_pickup_complete(self):
        init_db()
        donor_username = f"donor_{uuid.uuid4().hex[:8]}"
        ngo_username = f"ngo_{uuid.uuid4().hex[:8]}"
        volunteer_username = f"volunteer_{uuid.uuid4().hex[:8]}"

        donor_user_id = User.create(username=donor_username, email=f"{donor_username}@example.com", password='Password1!', role='donor', status='active', full_name='Donor User', phone='9876543200')
        ngo_user_id = User.create(username=ngo_username, email=f"{ngo_username}@example.com", password='Password1!', role='ngo', status='active', full_name='NGO User', phone='9876543201')
        volunteer_user_id = User.create(username=volunteer_username, email=f"{volunteer_username}@example.com", password='Password1!', role='volunteer', status='active', full_name='Volunteer User', phone='9876543202')

        donor_id = Donor.create(donor_user_id, 'My Kitchen', 'Donor Contact', '9876543200', 'Donor Street')
        ngo_id = NGO.create(ngo_user_id, 'Food Care', 'REG-123', 'NGO Contact', '9876543201', 'NGO Street', is_verified=1)
        volunteer_id = Volunteer.create(volunteer_user_id, 'Volunteer User', '9876543202', 'Volunteer Street', is_approved=1)

        donation_id = FoodDonation.create(donor_id, 'Rice and Curry', 'Cooked Food', 'Veg', '10 servings', '2025-12-31 18:00', '2025-12-31 19:00', 'Donor Street')

        conn = get_db_connection()
        conn.execute("UPDATE food_distribution SET ngo_id = ?, volunteer_id = ?, status = 'Volunteer Assigned' WHERE donation_id = ?", (ngo_id, volunteer_id, donation_id))
        conn.execute("UPDATE food_donations SET status = 'Volunteer Assigned' WHERE id = ?", (donation_id,))
        conn.execute("INSERT INTO pickup_assignments (donation_id, ngo_id, volunteer_id, status, assigned_at) VALUES (?, ?, ?, 'Assigned', CURRENT_TIMESTAMP)", (donation_id, ngo_id, volunteer_id))
        conn.commit()
        conn.close()

        with app.test_client() as client:
            login = client.post('/login', data={
                'username': volunteer_username,
                'user_type': 'volunteer',
                'password': 'Password1!'
            }, follow_redirects=False)
            self.assertEqual(login.status_code, 302)

            response = client.post(f'/volunteer/inspect/{donation_id}', data={
                'appearance': 'yes',
                'packaging': 'yes',
                'within_expiry': 'yes',
                'storage_ok': 'yes',
                'suitable': 'yes',
                'notes': 'All good',
                'decision': 'Approved',
                'rejection_reason': ''
            }, follow_redirects=False)

            self.assertEqual(response.status_code, 302)

            conn = get_db_connection()
            distribution = conn.execute("SELECT status, picked_up_at FROM food_distribution WHERE donation_id = ?", (donation_id,)).fetchone()
            donation = conn.execute("SELECT status FROM food_donations WHERE id = ?", (donation_id,)).fetchone()
            latest_inspection = conn.execute("SELECT status FROM food_inspections WHERE donation_id = ? ORDER BY inspected_at DESC LIMIT 1", (donation_id,)).fetchone()
            notifications = conn.execute("SELECT message FROM notifications WHERE user_id = ? ORDER BY created_at DESC", (donor_user_id,)).fetchall()
            conn.close()

            self.assertEqual(distribution['status'], 'Volunteer Assigned')
            self.assertEqual(donation['status'], 'Volunteer Assigned')
            self.assertEqual(latest_inspection['status'], 'Approved')
            self.assertFalse(any('pickup completed' in (n['message'] or '').lower() for n in notifications))
            self.assertTrue(any('approved your donation' in (n['message'] or '').lower() for n in notifications))


if __name__ == '__main__':
    unittest.main()
