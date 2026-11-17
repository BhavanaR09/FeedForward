import unittest
import uuid
from unittest.mock import patch

from app import app
from database import get_db_connection, init_db
from models import User


class AdminContactReplyTests(unittest.TestCase):
    def setUp(self):
        init_db()
        suffix = uuid.uuid4().hex[:10]
        self.admin_id = User.create(
            username=f'admin_reply_{suffix}',
            email=f'admin_reply_{suffix}@example.com',
            password='Password1!',
            role='admin',
            status='active',
            full_name='Reply Admin',
            phone=f'9{suffix[:9]}'
        )
        conn = get_db_connection()
        cursor = conn.execute(
            "INSERT INTO contact_messages (name, email, subject, message) VALUES (?, ?, ?, ?)",
            ('Inbox Sender', 'sender@example.com', 'Donation question', 'Please contact me about pickup.')
        )
        self.message_id = cursor.lastrowid
        conn.commit()
        conn.close()

        self.client = app.test_client()
        login = self.client.post('/admin/login', data={
            'username': f'admin_reply_{suffix}',
            'password': 'Password1!'
        }, follow_redirects=False)
        self.assertEqual(login.status_code, 302)

    def tearDown(self):
        conn = get_db_connection()
        conn.execute('DELETE FROM contact_messages WHERE id = ?', (self.message_id,))
        conn.execute('DELETE FROM users WHERE id = ?', (self.admin_id,))
        conn.commit()
        conn.close()

    def test_success_sends_to_database_email_and_records_sent_reply(self):
        with patch('app.send_notification_email', return_value=True) as send_email:
            response = self.client.post(
                f'/admin/contact-message/{self.message_id}/reply',
                data={
                    'reply_message': 'We can help arrange pickup.',
                    'recipient_email': 'attacker@example.com'
                },
                follow_redirects=True
            )

        self.assertIn(b'Reply Sent Successfully', response.data)
        send_email.assert_called_once()
        recipient, subject, body = send_email.call_args.args
        self.assertEqual(recipient, 'sender@example.com')
        self.assertEqual(subject, 'Re: Donation question')
        self.assertIn('Hello Inbox Sender,', body)
        self.assertIn('We can help arrange pickup.', body)

        conn = get_db_connection()
        reply = conn.execute(
            'SELECT * FROM message_replies WHERE contact_message_id = ?',
            (self.message_id,)
        ).fetchone()
        original = conn.execute('SELECT * FROM contact_messages WHERE id = ?', (self.message_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(reply)
        self.assertEqual(reply['status'], 'sent')
        self.assertEqual(reply['recipient_email'], 'sender@example.com')
        self.assertIsNotNone(reply['sent_at'])
        self.assertIsNotNone(original)

    def test_smtp_failure_records_failure_without_success_flash(self):
        with patch('app.send_notification_email', return_value=False) as send_email:
            response = self.client.post(
                f'/admin/contact-message/{self.message_id}/reply',
                data={'reply_message': 'We can help arrange pickup.'},
                follow_redirects=True
            )

        send_email.assert_called_once()
        self.assertIn(b'Unable to send reply. Please try again.', response.data)
        self.assertNotIn(b'Reply Sent Successfully', response.data)
        conn = get_db_connection()
        reply = conn.execute(
            'SELECT status, sent_at FROM message_replies WHERE contact_message_id = ?',
            (self.message_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(reply['status'], 'failed')
        self.assertIsNone(reply['sent_at'])

    def test_empty_reply_is_rejected_before_smtp(self):
        with patch('app.send_notification_email') as send_email:
            response = self.client.post(
                f'/admin/contact-message/{self.message_id}/reply',
                data={'reply_message': '   '},
                follow_redirects=True
            )

        send_email.assert_not_called()
        self.assertIn(b'Reply message cannot be empty.', response.data)
        self.assertNotIn(b'Reply Sent Successfully', response.data)


if __name__ == '__main__':
    unittest.main()