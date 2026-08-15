import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import urlencode
from pathlib import Path
from unittest.mock import patch

import miniapp_server


TOKEN = "test-token-customer-email"


def signed_init_data(user_id=1):
    user = {"id": user_id, "first_name": "Test", "username": "tester"}
    values = {"auth_date": str(int(time.time())), "query_id": "email-checkout", "user": json.dumps(user)}
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class CustomerEmailCheckoutTest(unittest.TestCase):
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
            INSERT INTO users(user_id, username, balance, credits, plan_name) VALUES(1, 'owner', 100000, 0, 'FREE');
            INSERT INTO store(name, price, credits) VALUES('Email service', 20000, 1);
            """
        )
        connection.commit()
        connection.close()
        miniapp_server.migrate()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_ADMIN_ID"] = "1"
        miniapp_server.GROUP_GATE_ENABLED = False
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {"X-Telegram-Init-Data": signed_init_data(1)}

    def tearDown(self):
        self.temp.cleanup()

    def add_required_product_to_cart(self):
        product = self.client.post(
            "/api/admin/products",
            json={
                "name": "Email delivery",
                "price": 20000,
                "credits": 1,
                "requiresCustomerEmail": True,
            },
            headers=self.headers,
        )
        self.assertEqual(product.status_code, 200)
        item_id = product.json["id"]
        public = self.client.get(f"/api/products/{item_id}", headers=self.headers)
        self.assertTrue(public.json["item"]["requiresCustomerEmail"])
        self.assertEqual(
            self.client.put(f"/api/cart/{item_id}", json={"quantity": 1}, headers=self.headers).status_code,
            200,
        )
        return item_id

    def test_admin_product_flag_and_checkout_email_validation(self):
        self.add_required_product_to_cart()
        before_connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        before = before_connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0]
        before_connection.close()

        missing = self.client.post(
            "/api/checkout", json={"idempotencyKey": "email-missing-0001"}, headers=self.headers
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json["reason_code"], "customer_email_required")

        invalid = self.client.post(
            "/api/checkout",
            json={"idempotencyKey": "email-invalid-0001", "customerEmail": "not-an-email"},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json["reason_code"], "invalid_customer_email")

        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], before)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history").fetchone()[0], 0)
        connection.close()

        success = self.client.post(
            "/api/checkout",
            json={"idempotencyKey": "email-success-0001", "customerEmail": " Customer@Example.com "},
            headers=self.headers,
        )
        self.assertEqual(success.status_code, 200)
        order_id = success.json["orderIds"][0]
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(
            connection.execute("SELECT customer_email FROM purchase_history WHERE id=?", (order_id,)).fetchone()[0],
            "customer@example.com",
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history").fetchone()[0], 1)
        connection.close()

        replay = self.client.post(
            "/api/checkout",
            json={"idempotencyKey": "email-success-0001"},
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])

    def test_existing_product_does_not_require_email(self):
        products = self.client.get("/api/products", headers=self.headers)
        self.assertEqual(products.status_code, 200)
        legacy = next(item for item in products.json["items"] if item["name"] == "Email service")
        self.assertFalse(legacy["requiresCustomerEmail"])

    def test_frontend_wires_admin_flag_checkout_input_and_api_payload(self):
        root = Path(miniapp_server.BASE_DIR)
        admin = (root / "miniapp" / "assets" / "admin.js").read_text(encoding="utf-8")
        views = (root / "miniapp" / "assets" / "views.js").read_text(encoding="utf-8")
        api = (root / "miniapp" / "assets" / "api.js").read_text(encoding="utf-8")
        self.assertIn("requiresCustomerEmail", admin)
        self.assertIn("checkout-customer-email", views)
        self.assertIn("customerEmail", api)

    def test_admin_email_approval_notifies_customer_once(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        order_id = connection.execute(
            """INSERT INTO purchase_history
               (user_id,plan_name,price,date,status,customer_email)
               VALUES(1,'Email delivery',20000,'2026-08-12','PROCESSING','customer@example.com')"""
        ).lastrowid
        connection.commit()
        connection.close()

        with patch.object(miniapp_server, "telegram_send", return_value=True) as send:
            approved = self.client.put(
                f"/api/admin/orders/{order_id}",
                json={"status": "PROCESSING", "emailApproved": True},
                headers=self.headers,
            )
            self.assertEqual(approved.status_code, 200)
            self.assertTrue(approved.json["emailApproved"])
            self.assertTrue(approved.json["notificationSent"])
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], "1")
            self.assertNotIn("customer@example.com", send.call_args.args[1])

            repeated = self.client.put(
                f"/api/admin/orders/{order_id}",
                json={"status": "PROCESSING", "emailApproved": True},
                headers=self.headers,
            )
            self.assertEqual(repeated.status_code, 200)
            self.assertFalse(repeated.json["notificationSent"])
            send.assert_called_once()

        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        row = connection.execute(
            "SELECT customer_email_approved,customer_email_approved_at,customer_email_notified_at FROM purchase_history WHERE id=?",
            (order_id,),
        ).fetchone()
        notification = connection.execute(
            "SELECT kind,title FROM notifications WHERE user_id=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 1)
        self.assertTrue(row[1])
        self.assertTrue(row[2])
        self.assertEqual(notification[0], "customer_email_approved")


if __name__ == "__main__":
    unittest.main()
