import unittest

from validation import is_valid_email


class ValidationTests(unittest.TestCase):
    def test_accepts_synthetic_address(self):
        self.assertTrue(is_valid_email("demo.user@example.test"))

    def test_rejects_missing_at_sign(self):
        self.assertFalse(is_valid_email("not-an-email"))

    def test_rejects_embedded_whitespace(self):
        self.assertFalse(is_valid_email("demo user@example.test"))

    def test_rejects_empty_and_none(self):
        self.assertFalse(is_valid_email(""))
        self.assertFalse(is_valid_email(None))


if __name__ == "__main__":
    unittest.main()
