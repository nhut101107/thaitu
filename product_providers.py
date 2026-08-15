"""Generic, deliberately small product-provider adapters.

The provider contract is intentionally generic because NFToken Pro cannot
assume the private API shape of a supplier.  Adapters never include secrets
in exceptions, reprs, or returned admin payloads.
"""
import json
from urllib.parse import urljoin

import requests


class ProviderError(RuntimeError):
    def __init__(self, reason_code, message="Nhà cung cấp không khả dụng"):
        super().__init__(message)
        self.reason_code = reason_code


class GenericJsonProvider:
    def __init__(self, row, session=None):
        self.provider_id = int(row["id"])
        self.base_url = str(row["base_url"]).rstrip("/") + "/"
        self.api_key = str(row["api_key"] or "")
        self.timeout = max(1, min(int(row["timeout"] or 10), 60))
        self.session = session or requests.Session()

    def _request(self, method, path, payload=None):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        try:
            response = self.session.request(
                method, urljoin(self.base_url, path.lstrip("/")),
                json=payload, headers=headers, timeout=self.timeout,
            )
        except requests.Timeout as error:
            raise ProviderError("provider_timeout", "Nhà cung cấp phản hồi quá chậm") from error
        except requests.RequestException as error:
            raise ProviderError("provider_network_error") from error
        if response.status_code >= 500:
            raise ProviderError("provider_unavailable")
        if response.status_code >= 400:
            raise ProviderError("provider_rejected", "Nhà cung cấp từ chối yêu cầu")
        try:
            value = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise ProviderError("provider_invalid_response") from error
        if not isinstance(value, dict):
            raise ProviderError("provider_invalid_response")
        return value

    def health(self):
        return self._request("GET", "/health")

    def products(self):
        value = self._request("GET", "/products")
        items = value.get("products", value.get("items", []))
        if not isinstance(items, list):
            raise ProviderError("provider_invalid_response")
        return items

    def create_order(self, external_product_id, quantity, idempotency_key, metadata=None):
        return self._request("POST", "/orders", {
            "product_id": str(external_product_id), "quantity": int(quantity),
            "idempotency_key": str(idempotency_key), "metadata": metadata or {},
        })

    def refund(self, external_order_id):
        return self._request("POST", f"/orders/{external_order_id}/refund")


class MockProvider:
    """Deterministic adapter used by unit tests and local admin verification."""
    def __init__(self, products=None, failures=None):
        self._products = list(products or [])
        self._failures = dict(failures or {})
        self.orders = {}
        self.calls = []

    def health(self):
        self.calls.append(("health",))
        if "health" in self._failures:
            raise self._failures["health"]
        return {"ok": True}

    def products(self):
        self.calls.append(("products",))
        if "products" in self._failures:
            raise self._failures["products"]
        return list(self._products)

    def create_order(self, external_product_id, quantity, idempotency_key, metadata=None):
        self.calls.append(("order", str(external_product_id), int(quantity), str(idempotency_key)))
        if "order" in self._failures:
            raise self._failures["order"]
        if idempotency_key in self.orders:
            return self.orders[idempotency_key]
        result = {"ok": True, "status": "fulfilled", "order_id": f"mock-{len(self.orders) + 1}"}
        self.orders[idempotency_key] = result
        return result

    def refund(self, external_order_id):
        self.calls.append(("refund", str(external_order_id)))
        return {"ok": True, "status": "refunded"}


def provider_from_row(row):
    return GenericJsonProvider(row)
