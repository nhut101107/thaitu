import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import miniapp_server
from product_providers import MockProvider


TOKEN = "123456789:commercial_operations_test_token"


def signed(user_id=1):
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "commercial-ops",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": f"u{user_id}"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class CommercialOperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        miniapp_server.DATABASE_PATH = os.path.join(self.temp.name, "commercial.db")
        miniapp_server.MIGRATED_PATHS.clear()
        miniapp_server.PROVIDER_INSTANCES.clear()
        miniapp_server.TOOL_ATTEMPTS.clear()
        miniapp_server.CHECKOUT_ATTEMPTS.clear()
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.executescript("""
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE store(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER);
            CREATE TABLE purchase_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT DEFAULT 'PENDING');
            CREATE TABLE premium_cookies(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, is_used INTEGER DEFAULT 0);
            CREATE TABLE free_cookies(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, is_used INTEGER DEFAULT 0);
            INSERT INTO users(user_id,username,balance,credits,plan_name) VALUES(1,'owner',100000,0,'FREE');
            INSERT INTO store(name,price,credits) VALUES('Commercial item',10000,1);
        """)
        connection.commit()
        connection.close()
        miniapp_server.migrate()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_ADMIN_ID"] = "1"
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {
            "X-Telegram-Init-Data": signed(1),
            "X-Device-Id": "device-main-123456",
            "X-Device-Label": "Test phone",
            "X-Device-Platform": "TestOS",
        }

    def tearDown(self):
        os.environ.pop("PAYMENT_WEBHOOK_SECRET", None)
        self.temp.cleanup()

    def connection(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        return connection

    def test_device_limit_revoke_and_risk_event(self):
        for index in range(1, miniapp_server.DEVICE_LIMIT + 1):
            headers = {**self.headers, "X-Device-Id": f"device-{index}-abcdef"}
            self.assertEqual(self.client.get("/api/bootstrap", headers=headers).status_code, 200)
        blocked = self.client.get("/api/bootstrap", headers={**self.headers, "X-Device-Id": "device-over-limit"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json["reason_code"], "device_limit")
        devices = self.client.get("/api/devices", headers={**self.headers, "X-Device-Id": "device-5-abcdef"}).json["items"]
        target = next(item for item in devices if not item["current"])
        self.assertEqual(self.client.delete(f"/api/devices/{target['id']}", headers={**self.headers, "X-Device-Id": "device-5-abcdef"}).status_code, 200)
        dashboard = self.client.get("/api/admin/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200)
        admin_device = next(item for item in dashboard.json["devices"] if item["id"] == target["id"])
        self.assertEqual(admin_device["revoked"], 1)
        restored = self.client.post(f"/api/admin/devices/{target['id']}/restore", headers=self.headers, json={})
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json["restored"])
        repeated = self.client.post(f"/api/admin/devices/{target['id']}/restore", headers=self.headers, json={})
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json["restored"])
        self.assertTrue(repeated.json["alreadyActive"])
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT revoked FROM user_devices WHERE id=?", (target["id"],)).fetchone()[0], 0)
        self.assertGreaterEqual(connection.execute("SELECT COUNT(*) FROM risk_events WHERE code='device_limit'").fetchone()[0], 1)
        connection.close()

    def test_free_cookie_checks_live_preserves_quota_and_never_redelivers_same_cookie(self):
        connection = self.connection()
        connection.execute("INSERT OR REPLACE INTO plans(name,tokens_max,cookies_max) VALUES('VIP',0,2)")
        connection.execute("UPDATE users SET plan_name='VIP' WHERE user_id=1")
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=temporary')")
        connection.commit(); connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "network_error", {}, None)):
            failed = self.client.post("/api/tools/free-cookie", json={"requestId": "free-network-failure"}, headers=self.headers)
        self.assertEqual(failed.status_code, 409)
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT free_cookies_used FROM usage WHERE user_id=1").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT health_status FROM free_cookies").fetchone()[0], "quarantined")
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=live')")
        connection.commit(); connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")):
            delivered = self.client.post("/api/tools/free-cookie", json={"requestId": "free-live-delivery"}, headers=self.headers)
            repeated = self.client.post("/api/tools/free-cookie", json={"requestId": "free-live-repeat"}, headers=self.headers)
        self.assertEqual(delivered.status_code, 200)
        self.assertEqual(repeated.status_code, 409)
        self.assertNotIn("NetflixId", json.dumps(self.client.get("/api/deliveries", headers=self.headers).json))

    def test_warranty_dead_cookie_refunds_exact_credit_once(self):
        connection = self.connection()
        cookie_id = connection.execute("INSERT INTO premium_cookies(data,is_used) VALUES('NetflixId=dead',0)").lastrowid
        delivery = miniapp_server.record_delivery(connection, 1, "nftoken_vip", "premium", cookie_id, "warranty-source", {"membership_status": "CURRENT_MEMBER"})
        connection.commit(); connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "dead", {}, None)):
            first = self.client.post("/api/warranty", json={"deliveryId": delivery["id"], "reason": "Cookie không còn đăng nhập được", "idempotencyKey": "warranty-request-001"}, headers=self.headers)
            second = self.client.post("/api/warranty", json={"deliveryId": delivery["id"], "reason": "Cookie không còn đăng nhập được", "idempotencyKey": "warranty-request-001"}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["status"], "approved")
        self.assertTrue(second.json["duplicate"])
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM premium_cookies WHERE id=?", (cookie_id,)).fetchone()[0], 0)
        connection.close()

    def test_signed_payment_webhook_is_atomic_and_idempotent(self):
        os.environ["PAYMENT_WEBHOOK_SECRET"] = "webhook-test-secret"
        connection = self.connection()
        transaction_id = connection.execute(
            "INSERT INTO transactions(user_id,amount,status,created_at) VALUES(1,50000,'PENDING',?)",
            (miniapp_server.now_iso(),),
        ).lastrowid
        connection.commit(); connection.close()
        payload = json.dumps({"transactionId": transaction_id, "amount": 50000, "reference": "BANK-REF-001", "status": "paid"}, separators=(",", ":")).encode()
        signature = hmac.new(b"webhook-test-secret", payload, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Payment-Signature": signature}
        first = self.client.post("/api/payments/webhook/vietqr", data=payload, headers=headers)
        second = self.client.post("/api/payments/webhook/vietqr", data=payload, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json["duplicate"])
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 150000)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 1)
        connection.close()

    def test_background_nftoken_job_persists_result_and_charges_once(self):
        connection = self.connection()
        connection.execute("UPDATE users SET nftoken_credits=1 WHERE user_id=1")
        connection.execute("INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES('trial_nftoken_enabled','0')")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=background')")
        connection.commit(); connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")):
            created = self.client.post("/api/tools/nftoken", json={"mode": "plan", "requestId": "background-job-001", "background": True}, headers=self.headers)
            self.assertEqual(created.status_code, 202)
            result = None
            for _ in range(50):
                response = self.client.get("/api/tools/nftoken/job/background-job-001", headers=self.headers)
                if response.status_code == 200 and response.json.get("status") == "done":
                    result = response.json
                    break
                time.sleep(0.02)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["items"]), 1)
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_events WHERE request_id='background-job-001'").fetchone()[0], 1)
        connection.close()

    def test_provider_network_call_does_not_hold_sqlite_write_lock(self):
        class LockProbeProvider(MockProvider):
            def create_order(inner_self, *args, **kwargs):
                probe = sqlite3.connect(miniapp_server.DATABASE_PATH, timeout=1)
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                probe.close()
                return super().create_order(*args, **kwargs)

        connection = self.connection()
        provider_id = connection.execute(
            "INSERT INTO product_providers(name,base_url,api_key,timeout,enabled,created_at,updated_at) VALUES('Probe','https://provider.invalid','hidden',2,1,'','')"
        ).lastrowid
        connection.execute("UPDATE store SET provider_id=?,external_product_id='sku-probe' WHERE id=1", (provider_id,))
        connection.commit(); connection.close()
        miniapp_server.PROVIDER_INSTANCES[provider_id] = LockProbeProvider()
        self.client.put("/api/cart/1", json={"quantity": 1}, headers=self.headers)
        result = self.client.post("/api/checkout", json={"idempotencyKey": "provider-no-db-lock-001"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)

    def test_delivery_idempotency_does_not_inflate_inventory_metrics(self):
        connection = self.connection()
        cookie_id = connection.execute(
            "INSERT INTO premium_cookies(data) VALUES('NetflixId=delivery-metric')"
        ).lastrowid
        miniapp_server.record_delivery(
            connection, 1, "nftoken", "premium", cookie_id, "delivery-once", {}
        )
        miniapp_server.record_delivery(
            connection, 1, "nftoken", "premium", cookie_id, "delivery-once", {}
        )
        connection.commit()
        self.assertEqual(
            connection.execute(
                "SELECT delivery_count FROM premium_cookies WHERE id=?", (cookie_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM delivery_events WHERE request_id='delivery-once'"
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_migration_backups_are_unique_and_restore_without_data_loss(self):
        first = miniapp_server.backup_database_for_migration()
        second = miniapp_server.backup_database_for_migration()
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first))
        connection = self.connection()
        connection.execute("UPDATE users SET balance=1 WHERE user_id=1")
        connection.commit(); connection.close()
        self.assertTrue(miniapp_server.restore_database_backup(first))
        connection = self.connection()
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 100000)
        connection.close()


if __name__ == "__main__":
    unittest.main()
