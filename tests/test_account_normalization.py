import json
from pathlib import Path
import unittest

import code_goc
from account_normalization import normalize_account_payload, normalize_payment_method, normalize_profiles
from miniapp_server import public_account


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

    def test_account_name_rejects_country_alias_even_when_country_code_differs(self):
        account = normalize_account_payload({"account_name": "Việt Nam", "country": "MY"})
        self.assertEqual(account["account_name"], "N/A")
        account = normalize_account_payload({
            "account_name": "Việt Nam",
            "country": "MY",
            "firstName": "Azril Joy Nalo",
        })
        self.assertEqual(account["account_name"], "Azril Joy Nalo")

    def test_nested_shakti_billing_fixture_returns_full_safe_account_fields(self):
        info = {
            "account_name": "N/A", "email": "N/A", "email_masked": "N/A",
            "phone": "N/A", "country": "N/A", "country_currency": "",
            "membership_status": "N/A", "plan": "N/A", "plan_price": "N/A",
            "member_since": "N/A", "next_billing": "N/A", "payment_method": "N/A",
            "cc_type": "N/A", "last4": "N/A", "payment_on_hold": "N/A",
            "video_quality": "N/A", "max_streams": "N/A", "extra_member": "N/A",
            "extra_member_slots": "N/A", "profile_count": "N/A", "profiles": [],
            "_account_name_candidates": [],
        }
        fixture = {
            "userInfo": {
                "firstName": {"$type": "atom", "value": "Azril Joy Nalo"},
                "phoneNumber": {"$type": "atom", "value": "013-899 7042"},
            },
            "membershipStatus": {"$type": "atom", "value": "CURRENT_MEMBER"},
            "countryOfSignup": {"$type": "atom", "value": "MY"},
            "billing": {
                "currentPlan": {
                    "localizedPlanName": {"$type": "atom", "value": "Premium"},
                    "formattedPrice": {"$type": "atom", "value": "RM 62.90"},
                    "videoQuality": {"$type": "atom", "value": "UHD"},
                    "maxStreams": {"$type": "atom", "value": 4},
                },
                "currentPaymentMethod": {
                    "type": {"$type": "atom", "value": "DCB"},
                    "cardType": {"$type": "atom", "value": "DC"},
                },
            },
        }
        code_goc.NetflixTokenChecker()._parse_account_from_context(fixture, info)
        account = normalize_account_payload(info)
        self.assertEqual(account["account_name"], "Azril Joy Nalo")
        self.assertEqual(account["membership_status_label"], "Đang hoạt động")
        self.assertEqual(account["payment_method"], "Thanh toán nhà mạng (DCB)")
        self.assertEqual(account["cc_type"], "Thanh toán nhà mạng (DCB)")
        self.assertEqual(account["last4"], "Không có")
        self.assertEqual(account["plan"], "Premium")
        self.assertEqual(account["max_streams"], "4")
        public = public_account(account)
        values = [item["value"] for item in public["details"]]
        self.assertNotIn("Không rõ", values)
        self.assertEqual(public["status"], "Đang hoạt động")

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
        self.assertIn("MenuButtonWebApp", source)

    def test_bot_menu_exposes_only_shop_mmo_mini_app(self):
        source = Path(code_goc.__file__).read_text(encoding="utf-8")
        self.assertNotIn('CommandHandler("app"', source)
        self.assertNotIn("/app", source)
        self.assertIn("MenuButtonWebApp", source)
        self.assertIn("Shop MMO", source)
        self.assertIn("set_my_commands([])", source)
        self.assertNotIn("callback_data='store_main'", source)
        self.assertNotIn("callback_data='deposit_main'", source)


if __name__ == "__main__":
    unittest.main()
