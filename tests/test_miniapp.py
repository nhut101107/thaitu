import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
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
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
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


if __name__ == "__main__":
    unittest.main()
