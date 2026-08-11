import hashlib
import hmac
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
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

    def test_rejects_missing_or_tampered_init_data(self):
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"X-Telegram-Init-Data": signed_init_data(1) + "x"}).status_code, 401)

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
        second = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(second.status_code, 409)

    def test_vip_nftoken_is_direct_and_deducts_only_on_success(self):
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

    def test_tv_login_returns_safe_progress_log(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=tv-cookie')")
        connection.commit()
        connection.close()
        account = {"account_name": "Test", "email_masked": "tes***@mail.com", "plan": "Premium", "membership_status": "CURRENT_MEMBER"}
        with patch.object(miniapp_server, "run_tv_login", return_value=(True, "Thành công", account)):
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

if __name__ == "__main__":
    unittest.main()
