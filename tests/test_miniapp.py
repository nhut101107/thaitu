import hashlib
import hmac
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import requests
import code_goc
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import miniapp_server


TOKEN = str(123456789) + ":" + "test_token_long_enough_for_hmac"


def signed_init_data(user_id):
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": f"u{user_id}"}, separators=(",", ":")),
    }
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MiniAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        miniapp_server.DATABASE_PATH = os.path.join(self.temp.name, "test.db")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.executescript(
            """
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE store(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER);
            CREATE TABLE purchase_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT DEFAULT 'PENDING');
            CREATE TABLE premium_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            INSERT INTO users(user_id, username, balance, credits, plan_name) VALUES(1, 'owner', 100000, 0, 'FREE'), (2, 'other', 0, 0, 'FREE');
            INSERT INTO store(name, price, credits) VALUES('Gói 10 lượt', 20000, 10);
            """
        )
        connection.commit()
        connection.close()
        miniapp_server.migrate()
        miniapp_server.TOOL_ATTEMPTS.clear()
        miniapp_server.CHECKOUT_ATTEMPTS.clear()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_ADMIN_ID"] = "1"
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {"X-Telegram-Init-Data": signed_init_data(1)}

    def tearDown(self):
        self.temp.cleanup()

    def set_trial(self, nftoken=False, cookie=False, nftoken_limit=2, cookie_limit=2):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        values = {
            "trial_nftoken_enabled": "1" if nftoken else "0",
            "trial_nftoken_daily_limit": str(nftoken_limit),
            "trial_cookie_enabled": "1" if cookie else "0",
            "trial_cookie_daily_limit": str(cookie_limit),
        }
        connection.executemany(
            "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES(?,?)",
            values.items(),
        )
        connection.commit()
        connection.close()

    def test_rejects_missing_or_tampered_init_data(self):
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"X-Telegram-Init-Data": signed_init_data(1) + "x"}).status_code, 401)

    def test_pwa_session_can_reopen_authenticated_app_without_telegram_init_data(self):
        issued = self.client.post("/api/pwa/session", json={}, headers=self.headers)
        self.assertEqual(issued.status_code, 200)
        token = issued.json["token"]
        self.assertNotIn(TOKEN, token)
        reopened = self.client.get("/api/bootstrap", headers={"X-PWA-Session": token})
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json["user"]["id"], 1)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"X-PWA-Session": token + "x"}).status_code, 401)

    def test_bootstrap_uses_shop_mmo_customer_brand(self):
        response = self.client.get("/api/bootstrap", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["brand"]["name"], "Shop MMO")
        self.assertEqual(response.json["brand"]["tagline"], "Premium MMO Store")

    def test_cookie_check_has_hard_timeout(self):
        with patch("code_goc.checker.extract_cookies_from_text", return_value=[{"NetflixId": "safe"}]), patch(
            "code_goc.checker.check_cookie", side_effect=lambda _cookies, **_kwargs: time.sleep(0.2)
        ):
            result = miniapp_server.run_cookie_check("NetflixId=safe", timeout=0.01)
        self.assertEqual(result[2], "network_timeout")

    def test_cookie_check_normal_result_is_preserved(self):
        account = {"membership_status": "CURRENT_MEMBER"}
        with patch("code_goc.checker.extract_cookies_from_text", return_value=[{"NetflixId": "safe"}]), patch(
            "code_goc.checker.check_cookie", return_value=(True, "safe-token", None, account)
        ), patch("code_goc.checker.build_netscape_format", return_value="netscape"):
            result = miniapp_server.run_cookie_check("NetflixId=safe", timeout=1)
        self.assertEqual(result, (True, "safe-token", None, account, "netscape"))

    def test_cookie_check_uses_bounded_no_retry_checker_mode(self):
        account = {"membership_status": "CURRENT_MEMBER"}
        with patch("code_goc.checker.extract_cookies_from_text", return_value=[{"NetflixId": "safe"}]), patch(
            "code_goc.checker.check_cookie", return_value=(True, "safe-token", None, account)
        ) as check, patch("code_goc.checker.build_netscape_format", return_value="netscape"):
            miniapp_server.run_cookie_check("NetflixId=safe", timeout=2)
        self.assertEqual(check.call_args.kwargs["max_retries"], 0)
        self.assertLessEqual(check.call_args.kwargs["request_timeout"], 2)
        self.assertGreater(check.call_args.kwargs["deadline"], time.monotonic() - 2.1)

    def test_checker_network_error_tries_each_official_endpoint_without_cookie_logging(self):
        checker = code_goc.NetflixTokenChecker()
        session = Mock()
        session.post.side_effect = requests.exceptions.ConnectionError("blocked upstream")
        with patch.object(checker, "_create_session", return_value=session) as create_session, patch(
            "code_goc.time.sleep"
        ):
            result = checker.check_cookie(
                {"NetflixId": "safe"}, request_timeout=1, max_retries=0,
                deadline=time.monotonic() + 10,
            )
        self.assertEqual(result[:3], (False, None, "network_error"))
        self.assertEqual(session.post.call_count, len(code_goc.API_ENDPOINTS) * len(code_goc.QUERY_CONFIGS))
        self.assertEqual(create_session.call_args.args, (0,))
        self.assertNotIn("safe", repr(result))

    def test_checker_falls_back_after_one_endpoint_connection_error(self):
        checker = code_goc.NetflixTokenChecker()
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"createAutoLoginToken": "safe-token"}}
        session.post.side_effect = [requests.exceptions.ConnectionError("temporary"), response]
        account = {"membership_status": "CURRENT_MEMBER"}
        with patch.object(checker, "_create_session", return_value=session), patch.object(
            checker, "get_account_info", return_value=account
        ), patch("code_goc.time.sleep"):
            result = checker.check_cookie(
                {"NetflixId": "safe"}, request_timeout=1, max_retries=0,
                deadline=time.monotonic() + 10,
            )
        self.assertEqual(result[:3], (True, "safe-token", None))
        self.assertEqual(session.post.call_count, 2)

    def test_frontend_nftoken_request_has_timeout_cleanup_and_idempotency(self):
        api_source = Path("miniapp/assets/api.js").read_text(encoding="utf-8")
        views_source = Path("miniapp/assets/views.js").read_text(encoding="utf-8")
        self.assertIn("AbortController", api_source)
        self.assertIn("finally", api_source)
        self.assertIn("nftokenJob", api_source)
        self.assertIn("X-PWA-Session", api_source)
        self.assertIn("pwaSession", api_source)
        self.assertIn("requestId", views_source)
        self.assertIn("Thử lại", views_source)
        self.assertIn("finally", views_source)
        self.assertIn('timeoutMs: 90000', api_source)
        self.assertNotIn('tv-note', views_source)
        self.assertNotIn('Credential nhạy cảm đã được ẩn', views_source)
        self.assertIn('api.js?v=17', Path("miniapp/assets/app.js").read_text(encoding="utf-8"))
        self.assertIn('scheduleRender', Path("miniapp/assets/app.js").read_text(encoding="utf-8"))
        self.assertIn('beforeinstallprompt', Path("miniapp/assets/app.js").read_text(encoding="utf-8"))
        self.assertIn('shop-mmo-static-v5', Path("miniapp/sw.js").read_text(encoding="utf-8"))
        self.assertIn('trial_nftoken_enabled', Path("miniapp/assets/admin.js").read_text(encoding="utf-8"))
        self.assertNotIn('id="tool-quantity"', views_source)
        self.assertIn('backdrop-filter: none', Path("miniapp/assets/theme.css").read_text(encoding="utf-8"))

    def test_checkout_is_atomic_and_idempotent(self):
        response = self.client.put("/api/cart/1", json={"quantity": 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        first = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_123456"}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["total"], 40000)
        duplicate = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_123456"}, headers=self.headers)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 60000)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 20)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history WHERE user_id=1").fetchone()[0], 1)
        connection.close()

    def test_product_grants_nftoken_and_cookie_vip_credits_separately(self):
        product = self.client.post(
            "/api/admin/products",
            json={"name": "COMBO", "price": 30000, "nftokenCredits": 7,
                  "credits": 3, "category": "COMBO", "active": True},
            headers=self.headers,
        )
        self.assertEqual(product.status_code, 200)
        product_id = product.json["id"]
        self.assertEqual(
            self.client.put(
                f"/api/cart/{product_id}", json={"quantity": 2}, headers=self.headers
            ).status_code,
            200,
        )
        checkout = self.client.post(
            "/api/checkout", json={"idempotencyKey": "combo_checkout_123456"},
            headers=self.headers,
        )
        self.assertEqual(checkout.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(
            connection.execute(
                "SELECT nftoken_credits,credits FROM users WHERE user_id=1"
            ).fetchone(),
            (14, 6),
        )
        connection.close()

    def test_order_ownership_is_enforced(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        cursor = connection.execute("INSERT INTO purchase_history(user_id,plan_name,price,date) VALUES(2,'Private',1,'2026-01-01')")
        order_id = cursor.lastrowid
        connection.commit()
        connection.close()
        response = self.client.get(f"/api/orders/{order_id}", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_insufficient_balance_does_not_create_order(self):
        self.client.put("/api/cart/1", json={"quantity": 6}, headers=self.headers)
        response = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_too_much"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 100000)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history WHERE user_id=1").fetchone()[0], 0)
        connection.close()

    def test_free_cookie_runs_inside_miniapp_and_enforces_quota(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT OR REPLACE INTO plans(name,tokens_max,cookies_max) VALUES('VIP',2,1)")
        connection.execute("UPDATE users SET plan_name='VIP' WHERE user_id=1")
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=free-cookie')")
        connection.commit()
        connection.close()
        first = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["cookie"], "NetflixId=free-cookie")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT is_used FROM free_cookies WHERE data='NetflixId=free-cookie'").fetchone()[0], 1)
        connection.close()
        second = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(second.status_code, 409)

    def test_vip_nftoken_is_direct_and_deducts_only_on_success(self):
        self.set_trial(cookie=False)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=premium-cookie')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "email_masked": "tes***@mail.com", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")):
            response = self.client.post(
                "/api/tools/nftoken", json={"mode": "vip", "quantity": 1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 0)
        connection.close()

    def test_trial_nftoken_is_used_before_paid_credit_and_resets_by_local_date(self):
        self.set_trial(nftoken=True, nftoken_limit=1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=2 WHERE user_id=1")
        connection.execute("INSERT INTO trial_usage(user_id,local_date,nftoken_used) VALUES(1,'2000-01-01',99)")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=trial-one')")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=paid-two')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")):
            first = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "trial-plan-first"},
                headers=self.headers,
            )
            second = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "trial-plan-second"},
                headers=self.headers,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["quota"]["trial"]["nftokenRemaining"], 0)
        self.assertEqual(second.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT nftoken_used FROM trial_usage WHERE user_id=1 AND local_date=?",
                (miniapp_server.local_today(),),
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_trial_nftoken_prefers_free_cookie_and_returns_it_to_correct_stock(self):
        self.set_trial(nftoken=True, nftoken_limit=1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=weighted-free')")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=weighted-premium')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server.random, "randrange", return_value=0), patch.object(
            miniapp_server,
            "run_cookie_check",
            return_value=(True, "safe-token", None, account, "netscape"),
        ) as check:
            response = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "weighted-free-request"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("weighted-free", check.call_args.args[0])
        self.assertGreater(miniapp_server.TRIAL_NFTOKEN_FREE_COOKIE_PERCENT, 50)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT is_used FROM free_cookies").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT is_used FROM premium_cookies").fetchone()[0], 0)
        connection.close()

    def test_trial_nftoken_can_pick_premium_and_falls_back_to_available_stock(self):
        self.set_trial(nftoken=True, nftoken_limit=2)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=fallback-free')")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=minority-premium')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server.random, "randrange", return_value=99), patch.object(
            miniapp_server,
            "run_cookie_check",
            return_value=(True, "safe-token", None, account, "netscape"),
        ) as check:
            first = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "weighted-premium-request"},
                headers=self.headers,
            )
            connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
            connection.execute("UPDATE premium_cookies SET is_used=1")
            connection.commit()
            connection.close()
            second = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "weighted-fallback-request"},
                headers=self.headers,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("minority-premium", check.call_args_list[0].args[0])
        self.assertIn("fallback-free", check.call_args_list[1].args[0])

    def test_paid_nftoken_never_consumes_free_cookie_stock(self):
        self.set_trial(nftoken=False)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=free-must-stay')")
        connection.commit()
        connection.close()
        response = self.client.post(
            "/api/tools/nftoken",
            json={"mode": "plan", "requestId": "paid-premium-only"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason_code"], "stock_empty")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT is_used FROM free_cookies").fetchone()[0], 0)
        connection.close()

    def test_trial_cookie_is_used_before_paid_credit_and_idempotent(self):
        self.set_trial(cookie=True, cookie_limit=1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=trial-cookie')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")) as check:
            first = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "vip", "requestId": "trial-cookie-once"},
                headers=self.headers,
            )
            duplicate = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "vip", "requestId": "trial-cookie-once"},
                headers=self.headers,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertEqual(check.call_count, 1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT cookie_used FROM trial_usage WHERE user_id=1 AND local_date=?",
                (miniapp_server.local_today(),),
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_failed_trial_request_refunds_trial_and_returns_live_cookie(self):
        self.set_trial(nftoken=True, nftoken_limit=1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=temporary-network-error')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server.random, "randrange", return_value=0), patch.object(
            miniapp_server, "run_cookie_check", return_value=(False, None, "network_error", {}, None)
        ):
            response = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "trial-refund-network"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(
            connection.execute(
                "SELECT nftoken_used FROM trial_usage WHERE user_id=1 AND local_date=?",
                (miniapp_server.local_today(),),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(connection.execute("SELECT is_used FROM free_cookies").fetchone()[0], 0)
        connection.close()

    def test_daily_package_refund_uses_original_reservation_date(self):
        reservation_date = "2026-08-11"
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute(
            "INSERT INTO usage(user_id,date,tokens_used) VALUES(?,?,1)",
            (1, reservation_date),
        )
        connection.commit()
        connection.row_factory = sqlite3.Row
        miniapp_server.refund_nftoken_request(
            connection, 1, {"kind": "daily_nftoken", "date": reservation_date}
        )
        self.assertEqual(
            connection.execute(
                "SELECT tokens_used FROM usage WHERE user_id=? AND date=?",
                (1, reservation_date),
            ).fetchone()[0],
            0,
        )
        connection.close()

    def test_trial_can_be_locked_and_then_requires_purchased_quota(self):
        self.set_trial(nftoken=False, cookie=False)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=unused-when-locked')")
        connection.commit()
        connection.close()
        plan = self.client.post(
            "/api/tools/nftoken",
            json={"mode": "plan", "requestId": "trial-plan-locked"},
            headers=self.headers,
        )
        vip = self.client.post(
            "/api/tools/nftoken",
            json={"mode": "vip", "requestId": "trial-cookie-locked"},
            headers=self.headers,
        )
        self.assertEqual(plan.status_code, 409)
        self.assertEqual(plan.json["reason_code"], "nftoken_quota_exhausted")
        self.assertEqual(vip.status_code, 409)
        self.assertEqual(vip.json["reason_code"], "cookie_quota_exhausted")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT is_used FROM premium_cookies").fetchone()[0], 0)
        connection.close()

    def test_failed_vip_nftoken_refunds_credit(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=dead-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "dead", {}, None)):
            response = self.client.post(
                "/api/tools/nftoken", json={"mode": "vip", "quantity": 1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 1)
        connection.close()

    def test_failed_paid_nftoken_refunds_nftoken_credit(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=dead-paid')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "dead", {}, None)):
            response = self.client.post(
                "/api/tools/nftoken", json={"mode": "plan", "quantity": 1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(
            connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0],
            1,
        )
        connection.close()

    def test_nftoken_timeout_returns_reason_refunds_credit_and_returns_cookie(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=timeout-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "network_timeout", {}, None)):
            response = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "quantity": 1, "requestId": "timeout-request-1"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json["reason_code"], "nftoken_timeout")
        self.assertNotIn("NetflixId", response.get_data(as_text=True))
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT is_used FROM premium_cookies WHERE data='NetflixId=timeout-cookie'").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT status FROM nftoken_jobs WHERE request_id='timeout-request-1'").fetchone()[0], "error")
        connection.close()

    def test_nftoken_network_error_returns_safe_reason_and_refunds(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=network-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "network_error", {}, None)):
            response = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "quantity": 1, "requestId": "network-request-1"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason_code"], "network_error")
        self.assertNotIn("NetflixId", response.get_data(as_text=True))
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT is_used FROM premium_cookies WHERE data='NetflixId=network-cookie'").fetchone()[0], 0)
        connection.close()

    def test_dead_cookie_is_deleted_but_unknown_failure_is_returned_to_stock(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=2 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=dead-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "dead", {}, None)):
            dead = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "dead-request-1"},
                headers=self.headers,
            )
        self.assertEqual(dead.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=unknown-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "checker_exception", {}, None)):
            unknown = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "requestId": "unknown-request-1"},
                headers=self.headers,
            )
        self.assertEqual(unknown.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertIsNone(connection.execute("SELECT id FROM premium_cookies WHERE data='NetflixId=dead-cookie'").fetchone())
        self.assertEqual(connection.execute("SELECT is_used FROM premium_cookies WHERE data='NetflixId=unknown-cookie'").fetchone()[0], 0)
        connection.close()

    def test_nftoken_request_id_is_idempotent_and_job_result_is_replayable(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=idempotent-cookie')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "email_masked": "tes***@mail.com", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")) as check:
            first = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "quantity": 1, "requestId": "same-request-1"},
                headers=self.headers,
            )
            duplicate = self.client.post(
                "/api/tools/nftoken",
                json={"mode": "plan", "quantity": 1, "requestId": "same-request-1"},
                headers=self.headers,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertEqual(check.call_count, 1)
        replay = self.client.get("/api/tools/nftoken/job/same-request-1", headers=self.headers)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])

    def test_running_request_id_does_not_create_duplicate_work(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        now = miniapp_server.now_iso()
        connection.execute(
            "INSERT INTO nftoken_jobs(request_id,user_id,mode,quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("running-request-1", 1, "plan", 1, "running", now, now),
        )
        connection.commit()
        connection.close()
        response = self.client.post(
            "/api/tools/nftoken",
            json={"mode": "plan", "requestId": "running-request-1"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["reason_code"], "nftoken_in_progress")

    def test_tv_login_returns_safe_progress_log(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=tv-cookie')")
        connection.commit()
        connection.close()
        account = {"account_name": "Test", "email_masked": "tes***@mail.com", "plan": "Premium", "membership_status": "CURRENT_MEMBER"}
        with patch.object(miniapp_server, "run_tv_login", return_value=(True, "connected", "Thành công", account)):
            response = self.client.post("/api/tools/tv-login", json={"code": "12345678"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([step["status"] for step in response.json["steps"]], ["done"] * 5)
        self.assertNotIn("NetflixId", response.get_data(as_text=True))

    def test_giftcode_deposit_and_support_are_direct(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO discount_codes(code,amount,uses) VALUES('GIFT',5000,1)")
        connection.commit()
        connection.close()
        gift = self.client.post("/api/giftcode", json={"code": "gift"}, headers=self.headers)
        self.assertEqual(gift.status_code, 200)
        self.assertEqual(gift.json["amount"], 5000)
        with patch.object(miniapp_server, "telegram_notify", return_value=True) as notify:
            deposit = self.client.post("/api/deposits", json={"amount": 50000}, headers=self.headers)
            support = self.client.post(
                "/api/support", json={"message": "Tôi cần hỗ trợ đơn hàng"}, headers=self.headers
            )
        self.assertEqual(deposit.status_code, 200)
        self.assertEqual(deposit.json["status"], "AWAITING_PAYMENT")
        notify.assert_called_once()  # Chỉ yêu cầu hỗ trợ dùng thông báo Telegram cũ.
        self.assertEqual(support.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 105000)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM miniapp_support WHERE user_id=1").fetchone()[0], 1)
        connection.close()

    def test_giftcode_accepts_legacy_mixed_case_and_bot_normalizes_new_codes(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO discount_codes(code,amount,uses) VALUES('LegacyGift',3000,1)")
        connection.commit()
        connection.close()

        legacy = self.client.post("/api/giftcode", json={"code": " legacygift "}, headers=self.headers)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json["amount"], 3000)

        with patch.object(code_goc, "DATABASE_PATH", miniapp_server.DATABASE_PATH):
            code_goc.add_discount_code("NewGift", 7000, 1)
            success, amount = code_goc.use_discount_code(" newgift ", 1)
        self.assertTrue(success)
        self.assertEqual(amount, 7000)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertIsNotNone(connection.execute("SELECT 1 FROM discount_codes WHERE code='NEWGIFT'").fetchone())
        connection.close()

    def test_deposit_is_submitted_and_reviewed_entirely_in_miniapp(self):
        created = self.client.post(
            "/api/deposits", json={"amount": 70000}, headers=self.headers
        )
        self.assertEqual(created.status_code, 200)
        transaction_id = created.json["transactionId"]
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(
            connection.execute("SELECT status FROM transactions WHERE id=?", (transaction_id,)).fetchone()[0],
            "AWAITING_PAYMENT",
        )
        connection.close()

        submitted = self.client.post(
            f"/api/deposits/{transaction_id}/submit", json={}, headers=self.headers
        )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/api/deposits/{transaction_id}/submit", json={}, headers=self.headers
            ).status_code,
            409,
        )
        rejected = self.client.put(
            f"/api/admin/transactions/{transaction_id}",
            json={"status": "REJECTED", "note": "Chưa nhận được tiền"},
            headers=self.headers,
        )
        self.assertEqual(rejected.status_code, 200)
        history = self.client.get("/api/transactions", headers=self.headers).json["items"]
        item = next(tx for tx in history if tx["id"] == transaction_id)
        self.assertEqual(item["status"], "REJECTED")
        self.assertEqual(item["review_note"], "Chưa nhận được tiền")
        self.assertIsNotNone(item["reviewed_at"])

    def test_admin_api_is_hidden_from_normal_users(self):
        response = self.client.get(
            "/api/admin/dashboard",
            headers={"X-Telegram-Init-Data": signed_init_data(2)},
        )
        self.assertEqual(response.status_code, 403)
        bootstrap = self.client.get("/api/bootstrap", headers=self.headers)
        self.assertTrue(bootstrap.json["isAdmin"])

    def test_admin_can_manage_products_plans_users_deposits_codes_and_support(self):
        product = self.client.post(
            "/api/admin/products",
            json={
                "name": "VIP PRO", "price": 50000, "credits": 25,
                "category": "VIP", "description": "Gói quản trị tạo",
                "warrantyDays": 7, "featured": True, "active": True,
            },
            headers=self.headers,
        )
        self.assertEqual(product.status_code, 200)
        self.assertEqual(
            self.client.put(
                "/api/admin/plans/VIP%20PRO",
                json={"tokensMax": 10, "cookiesMax": 2}, headers=self.headers,
            ).status_code,
            200,
        )
        user = self.client.put(
            "/api/admin/users/2",
            json={"balance": 120000, "credits": 9, "plan": "VIP PRO", "isBanned": False},
            headers=self.headers,
        )
        self.assertEqual(user.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        tx_id = connection.execute(
            "INSERT INTO transactions(user_id,amount,status) VALUES(2,30000,'PENDING')"
        ).lastrowid
        ticket_id = connection.execute(
            "INSERT INTO miniapp_support(user_id,message,created_at) VALUES(2,'help','2026-01-01')"
        ).lastrowid
        connection.commit()
        connection.close()
        approved = self.client.put(
            f"/api/admin/transactions/{tx_id}", json={"status": "APPROVED"}, headers=self.headers
        )
        self.assertEqual(approved.status_code, 200)
        duplicate = self.client.put(
            f"/api/admin/transactions/{tx_id}", json={"status": "APPROVED"}, headers=self.headers
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            self.client.put(
                "/api/admin/codes/WELCOME", json={"amount": 10000, "uses": 5}, headers=self.headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                f"/api/admin/support/{ticket_id}", json={"status": "CLOSED"}, headers=self.headers
            ).status_code,
            200,
        )
        with patch.object(miniapp_server, "telegram_send", return_value=True):
            reply = self.client.post(
                f"/api/admin/support/{ticket_id}/reply",
                json={"message": "Admin đã xử lý yêu cầu của bạn"}, headers=self.headers,
            )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(
            self.client.put(
                "/api/admin/users/1",
                json={"balance": 0, "credits": 0, "plan": "FREE", "isBanned": True},
                headers=self.headers,
            ).status_code,
            400,
        )
        dashboard = self.client.get("/api/admin/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertTrue(any(item["name"] == "VIP PRO" for item in dashboard.json["products"]))
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=2").fetchone()[0], 150000)
        self.assertEqual(connection.execute("SELECT status FROM miniapp_support WHERE id=?", (ticket_id,)).fetchone()[0], "CLOSED")
        connection.close()

    def test_admin_can_delete_unused_product_and_preserve_image_url(self):
        invalid = self.client.post(
            "/api/admin/products",
            json={"name": "Invalid image", "category": "VIP", "imageUrl": "http://example.com/a.png"},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json["reason_code"], "product_validation_error")
        product = self.client.post(
            "/api/admin/products",
            json={"name": "IMAGE PRODUCT", "price": 1000, "category": "VIP", "imageUrl": "https://example.com/a.png"},
            headers=self.headers,
        )
        self.assertEqual(product.status_code, 200)
        product_id = product.json["id"]
        dashboard = self.client.get("/api/admin/dashboard", headers=self.headers)
        item = next(item for item in dashboard.json["products"] if item["id"] == product_id)
        self.assertEqual(item["imageUrl"], "https://example.com/a.png")
        public = self.client.get("/api/products", headers=self.headers)
        public_item = next(item for item in public.json["items"] if item["id"] == product_id)
        self.assertEqual(public_item["imageUrl"], "https://example.com/a.png")
        deleted = self.client.delete(f"/api/admin/products/{product_id}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json["deleted"])

    def test_admin_controls_features_inventory_orders_maintenance_and_audit(self):
        settings = self.client.put(
            "/api/admin/settings",
            json={
                "maintenance": False,
                "announcement": "Thông báo kiểm thử",
                "features": {
                    "tv": True, "planToken": True, "vipToken": True,
                    "freeCookie": False, "giftcode": True,
                    "deposit": True, "support": True,
                },
                "trial": {
                    "nftokenEnabled": True, "nftokenDailyLimit": 3,
                    "cookieEnabled": True, "cookieDailyLimit": 4,
                },
            },
            headers=self.headers,
        )
        self.assertEqual(settings.status_code, 200)
        disabled = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(disabled.status_code, 503)
        inventory = self.client.post(
            "/api/admin/inventory/premium",
            json={"data": "NetflixId=one\n---\nNetflixId=two"}, headers=self.headers,
        )
        self.assertEqual(inventory.status_code, 200)
        self.assertEqual(inventory.json["added"], 2)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        order_id = connection.execute(
            "INSERT INTO purchase_history(user_id,plan_name,price,date) VALUES(2,'VIP',10000,'2026-01-01')"
        ).lastrowid
        connection.commit()
        connection.close()
        order = self.client.put(
            f"/api/admin/orders/{order_id}",
            json={"status": "WARRANTY", "warrantyUntil": "2026-12-31"},
            headers=self.headers,
        )
        self.assertEqual(order.status_code, 200)
        dashboard = self.client.get("/api/admin/dashboard", headers=self.headers)
        self.assertEqual(dashboard.json["stats"]["premiumStock"], 2)
        self.assertEqual(
            dashboard.json["settings"]["trial"],
            {
                "nftokenEnabled": True, "nftokenDailyLimit": 3,
                "cookieEnabled": True, "cookieDailyLimit": 4,
            },
        )
        self.assertEqual(dashboard.json["settings"]["announcement"], "Thông báo kiểm thử")
        self.assertGreaterEqual(len(dashboard.json["audit"]), 3)
        self.assertNotIn("data", dashboard.json)
        maintenance = self.client.put(
            "/api/admin/settings",
            json={"maintenance": True, "announcement": "Bảo trì", "features": {}},
            headers=self.headers,
        )
        self.assertEqual(maintenance.status_code, 200)
        normal = self.client.get(
            "/api/bootstrap", headers={"X-Telegram-Init-Data": signed_init_data(2)}
        )
        self.assertEqual(normal.status_code, 503)
        self.assertEqual(self.client.get("/api/bootstrap", headers=self.headers).status_code, 200)

    def test_admin_uploads_folder_files_filters_txt_and_only_saves_live_cookies(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("inside-live.txt", "NetflixId=live-cookie")
            zipped.writestr("inside-dead.txt", "NetflixId=dead-cookie")
        archive.seek(0)

        def check_cookie(entry):
            if "live-cookie" in entry or "folder-live" in entry:
                return True, "token", None, {"membership_status": "CURRENT_MEMBER"}, "netscape"
            return False, None, "dead", {}, None

        with patch.object(miniapp_server, "run_cookie_check", side_effect=check_cookie):
            response = self.client.post(
                "/api/admin/inventory/premium/upload",
                data={"files": [
                    (io.BytesIO(b"NetflixId=folder-live"), "cookies/live.txt"),
                    (io.BytesIO(b"NetflixId=dead-cookie"), "cookies/dead.txt"),
                    (archive, "bundles/cookies.zip"),
                    (io.BytesIO(b"{}"), "cookies/ignored.json"),
                ]},
                content_type="multipart/form-data",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["files"], 4)
            self.assertEqual(response.json["skipped"], 1)
            job_id = response.json["job_id"]
            for _ in range(200):
                job = self.client.get(
                    f"/api/admin/inventory/job/{job_id}", headers=self.headers
                ).json
                if job["status"] != "running":
                    break
                time.sleep(0.01)

        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["checked"], 3)
        self.assertEqual(job["result"]["live"], 2)
        self.assertEqual(job["result"]["dead"], 1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        saved = [row[0] for row in connection.execute("SELECT data FROM premium_cookies")]
        self.assertEqual(len(saved), 2)
        self.assertTrue(any("live-cookie" in value for value in saved))
        self.assertTrue(any("folder-live" in value for value in saved))
        connection.close()

    def test_tv_login_returns_reason_code_for_each_backend_failure(self):
        cases = [
            ("tv_code_expired", "Netflix xác nhận mã đã hết hạn"),
            ("cookie_expired", "Cookie đã chết hoặc hết hạn"),
            ("selector_changed", "Netflix chưa hiển thị ô nhập mã"),
            ("network_timeout", "Netflix phản hồi quá chậm"),
            ("webdriver_error", "EdgeDriver gặp lỗi"),
            ("netflix_error", "Netflix trả về lỗi"),
        ]
        for reason_code, message in cases:
            with self.subTest(reason_code=reason_code):
                miniapp_server.TOOL_ATTEMPTS.clear()
                connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
                connection.execute("INSERT INTO premium_cookies(data,is_used) VALUES(?,0)", ("NetflixId=test",))
                connection.commit()
                connection.close()
                with patch.object(
                    miniapp_server,
                    "run_tv_login",
                    return_value=(False, reason_code, message, {}),
                ):
                    response = self.client.post(
                        "/api/tools/tv-login",
                        json={"code": "12345678"},
                        headers=self.headers,
                    )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json["reason_code"], reason_code)
                self.assertNotIn("NetflixId", response.get_data(as_text=True))
                self.assertNotIn("SecureNetflixId", response.get_data(as_text=True))

    def test_tv_login_only_reports_connected_for_explicit_success(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO premium_cookies(data,is_used) VALUES(?,0)", ("NetflixId=test",))
        connection.commit()
        connection.close()
        with patch.object(
            miniapp_server,
            "run_tv_login",
            return_value=(True, "connected", "Thành công", {"account_name": "Test"}),
        ):
            response = self.client.post(
                "/api/tools/tv-login",
                json={"code": "12345678"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["reason_code"], "connected")

    def test_tv_login_rejects_invalid_code_with_reason_code(self):
        response = self.client.post(
            "/api/tools/tv-login",
            json={"code": "1234"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["reason_code"], "invalid_code")

    def test_health_exposes_runtime_source_fingerprint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["runtime_version"], miniapp_server.TV_LOGIN_RUNTIME_VERSION)
        self.assertEqual(len(response.json["source_fingerprint"]), 20)

    def test_account_name_repairs_encoding_and_markdown_special_chars(self):
        account = {"account_name": "NguyÃªn_VÄƒn[*]", "email_masked": "n***@mail.com"}
        public = miniapp_server.public_account(account)
        self.assertEqual(public["name"], "Nguyên_Văn[*]")
        from code_goc import format_account_card
        card = format_account_card(account, "https://example.invalid")
        self.assertIn("Nguyên\\_Văn\\[", card)
        self.assertNotIn("NguyÃªn", card)
        self.assertNotIn("Nếu có lỗi xảy ra", card)

if __name__ == "__main__":
    unittest.main()
