"""Thin REST client for the Meridian CRM sandbox.

Auth is a candidate bearer token (the ``bh_...`` segment from the CRM URL).
It is read from ``CRM_API_TOKEN`` (or a ``.env`` file, which is gitignored).

Set ``CRM_SYNC_DRY_RUN=1`` to make every write (``patch_account`` /
``create_account``) log its payload and return a synthetic response instead
of calling the API - used for rehearsing changes before committing them.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

import requests

try:  # optional convenience; not required
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("crm_sync.api")

BASE_URL = os.environ.get(
    "CRM_BASE_URL", "https://analyst-assessment-production.up.railway.app"
)
API = f"{BASE_URL}/api/v1"

# Fields the PATCH endpoint accepts (confirmed against the live API).
MUTABLE_FIELDS = {
    "name",
    "parent_id",
    "status",
    "note",
    "care_type",
    "phone",
    "billing_street",
    "billing_city",
    "billing_state",
    "billing_zip",
    "chow_current_account",
    "duplicate_of_account",
}

# Bellhaven Senior Living (Parent Account).
BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"


def account_web_url(account_id: str, token: Optional[str] = None) -> str:
    """Human-facing CRM URL for an account (the token is the URL path segment)."""
    token = token or os.environ.get("CRM_API_TOKEN", "")
    if not token or not account_id:
        return ""
    return f"{BASE_URL}/crm/{token}/accounts/{account_id}"


def dry_run_enabled() -> bool:
    return os.environ.get("CRM_SYNC_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


class CrmApiError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str, url: str):
        self.status = status
        self.body = body
        self.method = method
        self.url = url
        super().__init__(f"{method} {url} -> {status}: {body}")


class CrmClient:
    def __init__(self, token: Optional[str] = None, timeout: int = 30):
        self.token = token or os.environ.get("CRM_API_TOKEN", "")
        if not self.token:
            raise RuntimeError(
                "No CRM token. Set CRM_API_TOKEN (env or .env) to the bh_... "
                "token from the CRM URL."
            )
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )

    # -- low level ---------------------------------------------------------

    def _request(self, method: str, path: str, **kw) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        resp = self._session.request(method, url, timeout=self.timeout, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if not resp.ok:
            raise CrmApiError(resp.status_code, body, method, url)
        return body

    # -- reads -----------------------------------------------------------

    def get_me(self) -> Dict[str, Any]:
        return self._request("GET", "/me")

    def iter_accounts(self, page_size: int = 200, **filters) -> Iterator[Dict[str, Any]]:
        page = 1
        while True:
            params = {"page": page, "page_size": page_size, **filters}
            payload = self._request("GET", "/accounts", params=params)
            data = payload.get("data", [])
            for row in data:
                yield row
            total = payload.get("total", 0)
            if page * page_size >= total or not data:
                break
            page += 1

    def list_accounts(self, **filters) -> List[Dict[str, Any]]:
        return list(self.iter_accounts(**filters))

    def get_account(self, account_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/accounts/{account_id}")

    def search_accounts(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        payload = self._request(
            "GET", "/accounts", params={"q": q, "page": 1, "page_size": limit}
        )
        return payload.get("data", [])

    # -- writes (guarded) ----------------------------------------------

    def patch_account(self, account_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        body = {k: v for k, v in fields.items() if k in MUTABLE_FIELDS}
        if not body:
            raise ValueError(f"No mutable fields to PATCH (got {list(fields)})")
        if dry_run_enabled():
            log.warning("[DRY RUN] PATCH /accounts/%s %s", account_id, json.dumps(body))
            return {"dry_run": True, "account_id": account_id, "sent": body}
        return self._request("PATCH", f"/accounts/{account_id}", json=body)

    def create_account(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        if not body.get("name"):
            raise ValueError("create_account requires a 'name'")
        if dry_run_enabled():
            log.warning("[DRY RUN] POST /accounts %s", json.dumps(body))
            return {"dry_run": True, "sent": body}
        return self._request("POST", "/accounts", json=body)
