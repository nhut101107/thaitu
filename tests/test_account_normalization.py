import json
from pathlib import Path
import unittest

import code_goc
from account_normalization import normalize_account_payload, normalize_payment_method, normalize_profiles


class AccountNormalizationTest(unittest.TestCase):
    def test_account_name_uses_real_name_fields_not_country(self):
        self.assertEqual(
            normalize_account_payload({"account_name": "Việt Nam", "country": "VN"})["account_name"],
            "N/A",
        )
        account = normalize_account_payload({"country": "VN", "firstName": r"Nguy\u1ec5n Văn"})
        self.assertEqual(account["account_name"], "Nguyễn Văn")

    def test_payment_mapping_and_non_card_last4(self):
        for raw in ("DC", "DCB", "DIRECT_CARRIER_BILLING", "DIRECT CARRIER"):
            self.assertEqual(normalize_payment_method(raw), "Thanh toán nhà mạng (DCB)")
        self.assertEqual(normalize_payment_method("PAYPAL"), "PayPal")
        self.assertEqual(normalize_payment_method("CC"), "Thẻ tín dụng")
        account = normalize_account_payload({"payment_method": "DC", "cc_type": "DC", "last4": "1234"})
        self.assertEqual(account["payment_method"], "Thanh toán nhà mạng (DCB)")
        self.assertEqual(account["cc_type"], "Thanh toán nhà mạng (DCB)")
        self.assertEqual(account["last4"], "Không có")

    def test_profile_escape_decode_and_deduplication(self):
        profiles = normalize_profiles([
            {"profileName": r"Hugo\x20Nguyen"},
            {"name": r"\u0048\u0075\u0067\u006f\u0020\u004e\u0067\u0075\u0079\u0065\u006e"},
            {"rawFirstName": r"\u004e\u0067\u0075\u0079\u1ec5n \u00c1nh"},
            {"firstName": "Nguyễn Ánh"},
            json.dumps(r"Emoji\x20\uD83D\uDE00"),
        ])
        self.assertEqual(profiles, ["Hugo Nguyen", "Nguyễn Ánh", "Emoji 😀"])
        account = normalize_account_payload({"profiles": profiles})
        self.assertEqual(account["profile_count"], 3)
        self.assertEqual(account["profiles"], profiles)
        self.assertNotIn("\\x", " ".join(profiles))
        self.assertNotIn("\\u", " ".join(profiles))

    def test_bot_no_longer_registers_or_advertises_app_command(self):
        source = Path(code_goc.__file__).read_text(encoding="utf-8")
        self.assertNotIn('CommandHandler("app"', source)
        self.assertNotIn("Mở NFToken Mini App", source)
        self.assertNotIn("MỞ NFToken MINI APP", source)
        self.assertNotIn("/app", source)
        self.assertIn("MenuButtonCommands", source)


if __name__ == "__main__":
    unittest.main()
