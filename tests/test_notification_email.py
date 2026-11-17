import unittest

from app import app, should_send_email_for_notification, send_notification_email


class NotificationEmailTests(unittest.TestCase):
    def test_important_notification_is_flagged_for_email(self):
        self.assertTrue(should_send_email_for_notification("NGO 'Test NGO' has accepted your surplus food donation: Vegetable Rice."))
        self.assertTrue(should_send_email_for_notification("You have been assigned a new food pickup: Vegetable Rice at 5 Main Street."))
        self.assertFalse(should_send_email_for_notification("Your profile has been updated."))

    def test_send_notification_email_requires_valid_recipients(self):
        with app.app_context():
            self.assertFalse(send_notification_email('', 'FeedForward - Test', 'body'))
            self.assertFalse(send_notification_email('user@example.com', 'FeedForward - Test', 'body'))


if __name__ == '__main__':
    unittest.main()
