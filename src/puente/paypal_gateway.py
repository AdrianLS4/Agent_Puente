"""Capa única de acceso a PayPal.

 - Órdenes (crear / consultar / capturar): usan el **PayPal Agent Toolkit** (`PayPalAPI.run`).
 - Reembolsos, tracking y disputas: el paquete `paypal-agent-toolkit` 1.1.0 NO trae esas tools,
   así que van por REST directo (`PayPalRest`). ⚠ Esos endpoints están escritos según la doc pública
   de PayPal pero NO verificados contra tu sandbox: probarlos el día 1 (ver README).
 - `MockPayPal`: mismo contrato, en memoria, con inyección de fallos. Permite correr y testear
   todo el flujo (incluidos los errores) sin credenciales.

Todo error se normaliza a `PayPalError(status, name, issue)` para que `errors.map_paypal_error` decida.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx


class PayPalError(Exception):
    def __init__(self, status_code: int | None, name: str = "", issue: str = "", debug_id: str = "",
                 body: Any = None):
        super().__init__(f"PayPal {status_code} {name} {issue}")
        self.status_code = status_code
        self.name = name
        self.issue = issue
        self.debug_id = debug_id
        self.body = body

    @classmethod
    def from_response(cls, status: int, body: Any) -> "PayPalError":
        name = issue = debug = ""
        if isinstance(body, dict):
            name = body.get("name", "") or ""
            debug = body.get("debug_id", "") or ""
            details = body.get("details") or []
            if details and isinstance(details[0], dict):
                issue = details[0].get("issue", "") or ""
        return cls(status, name, issue, debug, body)


class PayPalGateway(ABC):
    @abstractmethod
    def create_order(self, *, amount_usd: float, item_name: str, reference_id: str) -> dict: ...
    @abstractmethod
    def get_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def capture_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def refund_capture(self, capture_id: str, *, amount_usd: float, note: str) -> dict: ...
    @abstractmethod
    def add_tracking(self, *, capture_id: str, tracking_number: str, carrier: str) -> dict: ...
    @abstractmethod
    def provide_evidence(self, dispute_id: str, *, notes: str, tracking_number: str | None) -> dict: ...
    @abstractmethod
    def accept_claim(self, dispute_id: str, *, note: str) -> dict: ...
    @abstractmethod
    def create_invoice(self, *, amount_usd: float, item_name: str, recipient_email: str, reference_id: str, note: str = "") -> dict: ...
    @abstractmethod
    def send_invoice(self, invoice_id: str) -> dict: ...
    @abstractmethod
    def get_invoice(self, invoice_id: str) -> dict: ...
    @abstractmethod
    def create_authorize_order(self, *, amount_usd: float, item_name: str, reference_id: str) -> dict: ...
    @abstractmethod
    def authorize_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def capture_authorization(self, authorization_id: str, *, amount_usd: float | None = None) -> dict: ...
    @abstractmethod
    def void_authorization(self, authorization_id: str) -> dict: ...


# ───────────────────────── REST directo (refund / tracking / disputas) ─────────────────────────
class PayPalRest:
    SANDBOX = "https://api-m.sandbox.paypal.com"

    def __init__(self, client_id: str, secret: str, sandbox: bool = True, timeout: float = 15.0):
        assert sandbox, "este repo solo opera en sandbox"
        self.base = self.SANDBOX
        self._auth = (client_id, secret)
        self._timeout = timeout
        self._token: tuple[str, float] | None = None

    def _get_token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        r = httpx.post(f"{self.base}/v1/oauth2/token", auth=self._auth, timeout=self._timeout,
                       data={"grant_type": "client_credentials"}, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise PayPalError.from_response(r.status_code, _safe_json(r))
        d = r.json()
        self._token = (d["access_token"], time.time() + int(d.get("expires_in", 300)))
        return self._token[0]

    def request(self, method: str, path: str, *, json_body: dict | None = None,
                request_id: str | None = None, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id          # idempotencia nativa de PayPal
        try:
            r = httpx.request(method, f"{self.base}{path}", json=json_body, headers=headers,
                              params=params, timeout=self._timeout)
        except httpx.HTTPError as e:                            # timeout / red caída
            raise PayPalError(None, "NETWORK", str(e)) from e
        if r.status_code >= 400:
            raise PayPalError.from_response(r.status_code, _safe_json(r))
        return _safe_json(r) if r.content else {}


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _capture_info(raw: dict) -> dict:
    cap = (((raw.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
    amount = (cap.get("amount") or {}).get("value")
    return {"capture_id": cap.get("id"), "capture_status": cap.get("status"),
            "amount_usd": float(amount) if amount else None}


# ───────────────────────── Implementación real (sandbox) ─────────────────────────
class LivePayPal(PayPalGateway):
    def __init__(self, client_id: str, secret: str, sandbox: bool = True):
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context

        self.toolkit_api = PayPalAPI(client_id=client_id, secret=secret, context=Context(sandbox=sandbox))
        self.rest = PayPalRest(client_id, secret, sandbox)

    # --- vía Agent Toolkit ---
    def _toolkit(self, method: str, params: dict) -> dict:
        import requests
        try:
            return json.loads(self.toolkit_api.run(method, params))
        except requests.HTTPError as e:
            resp = e.response
            raise PayPalError.from_response(resp.status_code if resp is not None else None,
                                            _safe_json_requests(resp)) from e
        except requests.RequestException as e:
            raise PayPalError(None, "NETWORK", str(e)) from e

    def create_order(self, *, amount_usd: float, item_name: str, reference_id: str) -> dict:
        out = self._toolkit("create_order", {
            "currency_code": "USD",
            "items": [{"name": item_name, "quantity": 1, "item_cost": amount_usd,
                       "tax_percent": 0, "item_total": amount_usd}],
        })
        links = {l.get("rel"): l.get("href") for l in out.get("links", [])}
        return {"order_id": out["id"], "status": out.get("status"),
                "approve_url": links.get("payer-action") or links.get("approve"), "raw": out}

    def get_order(self, order_id: str) -> dict:
        out = self._toolkit("get_order_details", {"order_id": order_id})
        return {"status": out.get("status"), **_capture_info(out.get("raw", {})), "raw": out.get("raw")}

    def capture_order(self, order_id: str) -> dict:
        try:
            out = self._toolkit("pay_order", {"order_id": order_id})
        except ValueError:
            out = self._toolkit("capture_order", {"order_id": order_id})
        return {"status": out.get("status"), **_capture_info(out.get("raw", {})), "raw": out.get("raw")}

    def create_invoice(self, *, amount_usd: float, item_name: str, recipient_email: str, reference_id: str, note: str = "") -> dict:
        try:
            num_res = self._toolkit("generate_invoice_number", {})
            inv_num = num_res.get("invoice_number")
        except Exception:
            inv_num = None

        out = self._toolkit("create_invoice", {
            "currency_code": "USD",
            "invoice_number": inv_num,
            "reference": reference_id,
            "note": note or f"PUENTE: {item_name}",
            "primary_recipients": [{
                "billing_info": {"email_address": recipient_email}
            }],
            "items": [{
                "name": item_name,
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": f"{amount_usd:.2f}"}
            }]
        })
        href = out.get("href", "")
        iid = href.split("/")[-1] if href else (out.get("id") or out.get("invoice_id"))
        return {"invoice_id": iid, "invoice_number": inv_num, "status": out.get("status", "DRAFT"), "raw": out}

    def send_invoice(self, invoice_id: str) -> dict:
        out = self._toolkit("send_invoice", {"invoice_id": invoice_id, "send_to_recipient": True})
        inv = self.get_invoice(invoice_id)
        return {"invoice_id": invoice_id, "status": inv.get("status", "SENT"), "invoice_url": inv.get("invoice_url"), "raw": out}

    def get_invoice(self, invoice_id: str) -> dict:
        out = self._toolkit("get_invoice", {"invoice_id": invoice_id})
        status = out.get("status")
        amount = (out.get("amount") or {}).get("value")
        links = {l.get("rel"): l.get("href") for l in out.get("links", [])}
        return {
            "invoice_id": invoice_id,
            "status": status,
            "invoice_url": links.get("payer-view") or links.get("self"),
            "amount_usd": float(amount) if amount else None,
            "raw": out,
        }

    # --- vía REST (NO verificado en sandbox) ---
    def refund_capture(self, capture_id: str, *, amount_usd: float, note: str) -> dict:
        return self.rest.request(
            "POST", f"/v2/payments/captures/{capture_id}/refund", request_id=f"refund-{capture_id}",
            json_body={"amount": {"value": f"{amount_usd:.2f}", "currency_code": "USD"},
                       "note_to_payer": note[:255]})

    def add_tracking(self, *, capture_id: str, tracking_number: str, carrier: str) -> dict:
        return self.rest.request("POST", "/v1/shipping/trackers-batch", json_body={"trackers": [{
            "transaction_id": capture_id, "tracking_number": tracking_number,
            "status": "SHIPPED", "carrier": carrier}]})

    def provide_evidence(self, dispute_id: str, *, notes: str, tracking_number: str | None) -> dict:
        evidence: dict[str, Any] = {"evidence_type": "PROOF_OF_FULFILLMENT", "notes": notes[:2000]}
        if tracking_number:
            evidence["evidence_info"] = {"tracking_info": [{"tracking_number": tracking_number}]}
        return self.rest.request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                                 json_body={"evidences": [evidence]})

    def accept_claim(self, dispute_id: str, *, note: str) -> dict:
        return self.rest.request("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim",
                                 json_body={"note": note[:2000]})

    def create_authorize_order(self, *, amount_usd: float, item_name: str, reference_id: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference_id,
                "description": item_name[:127],
                "amount": {
                    "currency_code": "USD",
                    "value": f"{amount_usd:.2f}"
                }
            }]
        }
        out = self.rest.request(
            "POST",
            "/v2/checkout/orders",
            json_body=body,
            request_id=f"order-auth-{reference_id}-{int(time.time() * 1000)}",
        )
        links = {l.get("rel"): l.get("href") for l in out.get("links", [])}
        return {
            "order_id": out["id"],
            "status": out.get("status"),
            "approve_url": links.get("payer-action") or links.get("approve"),
            "intent": "AUTHORIZE",
            "raw": out
        }

    def authorize_order(self, order_id: str) -> dict:
        out = self.rest.request("POST", f"/v2/checkout/orders/{order_id}/authorize",
                                request_id=f"auth-{order_id}", json_body={})
        units = out.get("purchase_units") or [{}]
        authorizations = (units[0].get("payments") or {}).get("authorizations") or [{}]
        auth_id = authorizations[0].get("id") if authorizations else None
        auth_status = authorizations[0].get("status") if authorizations else None
        return {
            "order_id": order_id,
            "authorization_id": auth_id,
            "status": auth_status or out.get("status"),
            "raw": out
        }

    def capture_authorization(self, authorization_id: str, *, amount_usd: float | None = None) -> dict:
        body = {}
        if amount_usd is not None:
            body = {"amount": {"currency_code": "USD", "value": f"{amount_usd:.2f}"}}
        out = self.rest.request("POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                                request_id=f"cap-auth-{authorization_id}", json_body=body)
        return {
            "authorization_id": authorization_id,
            "capture_id": out.get("id"),
            "status": out.get("status"),
            "raw": out
        }

    def void_authorization(self, authorization_id: str) -> dict:
        out = self.rest.request("POST", f"/v2/payments/authorizations/{authorization_id}/void",
                                request_id=f"void-{authorization_id}", json_body={})
        return {
            "authorization_id": authorization_id,
            "status": out.get("status", "VOIDED"),
            "raw": out
        }


def _safe_json_requests(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"raw": getattr(resp, "text", "")}


# ───────────────────────── Mock (demo sin credenciales + tests) ─────────────────────────
class MockPayPal(PayPalGateway):
    """En memoria. `fail_next[method] = [PayPalError, ...]` hace fallar las próximas llamadas."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.fail_next: dict[str, list[PayPalError]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._n = 0

    def _enter(self, method: str, **kw) -> None:
        self.calls.append((method, kw))
        q = self.fail_next.get(method)
        if q:
            raise q.pop(0)

    # helpers del demo (simulan acciones del humano en PayPal)
    def approve(self, order_id: str) -> None:
        self.orders[order_id]["status"] = "APPROVED"

    def set_capture_status(self, order_id: str, status: str) -> None:
        self.orders[order_id]["capture_status"] = status

    # contrato
    def create_order(self, *, amount_usd, item_name, reference_id):
        self._enter("create_order", amount_usd=amount_usd)
        self._n += 1
        oid = f"MOCK-ORDER-{self._n}"
        self.orders[oid] = {"status": "CREATED", "amount": amount_usd, "capture_id": None,
                            "capture_status": "COMPLETED"}
        return {"order_id": oid, "status": "CREATED",
                "approve_url": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}

    def get_order(self, order_id):
        self._enter("get_order", order_id=order_id)
        o = self.orders.get(order_id)
        if o is None:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        return {"status": o["status"], "capture_id": o["capture_id"], "capture_status":
                o["capture_status"] if o["capture_id"] else None,
                "amount_usd": o["amount"] if o["capture_id"] else None}

    def capture_order(self, order_id):
        self._enter("capture_order", order_id=order_id)
        o = self.orders.get(order_id)
        if o is None:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        if o["status"] == "COMPLETED":
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")
        if o["status"] != "APPROVED":
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
        o["status"] = "COMPLETED"
        o["capture_id"] = f"MOCK-CAP-{order_id[-1]}"
        return {"status": "COMPLETED", "capture_id": o["capture_id"],
                "capture_status": o["capture_status"], "amount_usd": o["amount"]}

    def refund_capture(self, capture_id, *, amount_usd, note):
        self._enter("refund_capture", capture_id=capture_id)
        return {"status": "COMPLETED", "id": f"MOCK-REFUND-{capture_id}"}

    def add_tracking(self, *, capture_id, tracking_number, carrier):
        self._enter("add_tracking", tracking_number=tracking_number)
        return {"tracker_id": f"{capture_id}-{tracking_number}"}

    def provide_evidence(self, dispute_id, *, notes, tracking_number):
        self._enter("provide_evidence", dispute_id=dispute_id)
        return {"status": "OK"}

    def accept_claim(self, dispute_id, *, note):
        self._enter("accept_claim", dispute_id=dispute_id)
        return {"status": "OK"}

    def create_invoice(self, *, amount_usd, item_name, recipient_email, reference_id, note=""):
        self._enter("create_invoice", amount_usd=amount_usd)
        self._n += 1
        iid = f"MOCK-INV-{self._n}"
        inv_num = f"INV-2026-{self._n:03d}"
        self.invoices[iid] = {
            "id": iid,
            "invoice_number": inv_num,
            "status": "DRAFT",
            "amount": amount_usd,
            "recipient_email": recipient_email,
            "invoice_url": f"https://www.sandbox.paypal.com/invoice/payerView/details/{iid}",
        }
        return {"invoice_id": iid, "invoice_number": inv_num, "status": "DRAFT", "invoice_url": self.invoices[iid]["invoice_url"]}

    def send_invoice(self, invoice_id):
        self._enter("send_invoice", invoice_id=invoice_id)
        inv = self.invoices.get(invoice_id)
        if inv is None:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        inv["status"] = "SENT"
        return {"invoice_id": invoice_id, "status": "SENT", "invoice_url": inv["invoice_url"]}

    def get_invoice(self, invoice_id):
        self._enter("get_invoice", invoice_id=invoice_id)
        inv = self.invoices.get(invoice_id)
        if inv is None:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        return {
            "invoice_id": invoice_id,
            "status": inv["status"],
            "amount_usd": inv["amount"],
            "invoice_url": inv["invoice_url"],
        }

    def pay_invoice(self, invoice_id):
        inv = self.invoices.get(invoice_id)
        if inv:
            inv["status"] = "PAID"

    def create_authorize_order(self, *, amount_usd, item_name, reference_id):
        self._enter("create_authorize_order", amount_usd=amount_usd)
        self._n += 1
        oid = f"MOCK-ORDER-AUTH-{self._n}"
        self.orders[oid] = {
            "status": "CREATED", "amount": amount_usd, "intent": "AUTHORIZE",
            "authorization_id": None, "capture_id": None, "capture_status": None
        }
        return {"order_id": oid, "status": "CREATED", "intent": "AUTHORIZE",
                "approve_url": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}

    def authorize_order(self, order_id):
        self._enter("authorize_order", order_id=order_id)
        o = self.orders.get(order_id)
        if o is None:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
        if o["status"] != "APPROVED":
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
        auth_id = f"MOCK-AUTH-{order_id}"
        o["status"] = "AUTHORIZED"
        o["authorization_id"] = auth_id
        return {"order_id": order_id, "authorization_id": auth_id, "status": "CREATED"}

    def capture_authorization(self, authorization_id, *, amount_usd=None):
        self._enter("capture_authorization", authorization_id=authorization_id)
        cap_id = f"MOCK-CAP-{authorization_id}"
        return {"authorization_id": authorization_id, "capture_id": cap_id, "status": "COMPLETED"}

    def void_authorization(self, authorization_id):
        self._enter("void_authorization", authorization_id=authorization_id)
        return {"authorization_id": authorization_id, "status": "VOIDED"}
