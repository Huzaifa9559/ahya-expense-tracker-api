#!/usr/bin/env python3
"""Build redacted Postman collection JSON from postman/captured/*.json."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT / "postman" / "captured"
OUT = ROOT / "postman" / "Ahya_Expense_Tracker.postman_collection.json"
ENV_OUT = ROOT / "postman" / "local.postman_environment.json"

# Match JWTs and any other eyJ… base64 blobs (opaque cursors also start with eyJ).
EYJ_RE = re.compile(r"eyJ[A-Za-z0-9_\-+=/]+")
SIG_RE = re.compile(r"(X-Amz-Signature=)[0-9a-fA-F]+")
CRED_RE = re.compile(r"(X-Amz-Credential=)[^&\"]+")
TOKEN_RE = re.compile(r"(X-Amz-Security-Token=)[^&\"]+")
AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")


def redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = k.lower()
            if lk in {"access_token", "accesstoken"}:
                out[k] = "<redacted-access-token>"
            elif lk in {"refresh_token", "refreshtoken"}:
                out[k] = "<redacted-refresh-token>"
            elif lk == "authorization" and isinstance(v, str):
                out[k] = "Bearer {{accessToken}}"
            elif lk in {"policy", "signature", "x-amz-signature", "x-amz-security-token", "x-amz-credential"}:
                out[k] = "<redacted>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        s = EYJ_RE.sub("<redacted>", obj)
        s = SIG_RE.sub(r"\1<redacted>", s)
        s = CRED_RE.sub(r"\1<redacted>", s)
        s = TOKEN_RE.sub(r"\1<redacted>", s)
        s = AKIA_RE.sub("<redacted>", s)
        if "Bearer <redacted>" in s or s.startswith("Bearer eyJ"):
            s = "Bearer {{accessToken}}"
        return s
    return obj


def load(name: str) -> dict:
    return json.loads((CAP / f"{name}.json").read_text())


def example_from(cap: dict, name: str) -> dict:
    cap = redact(deepcopy(cap))
    status = cap["status_code"]
    body = cap.get("response_body")
    body_str = body if isinstance(body, str) else json.dumps(body, indent=2)
    headers = [
        {"key": k, "value": str(v)}
        for k, v in (cap.get("response_headers") or {}).items()
        if k.lower() in {"content-type", "etag", "retry-after", "idempotent-replayed"}
    ]
    req_headers = [
        {"key": k, "value": str(v)}
        for k, v in (cap.get("request_headers") or {}).items()
    ]
    original_request = {
        "method": cap["method"],
        "header": req_headers,
        "url": "{{baseUrl}}" + (cap["url"] if cap["url"].startswith("/") else "/" + cap["url"].split("://", 1)[-1].split("/", 1)[-1] if "://" in cap["url"] else cap["url"]),
    }
    # Keep absolute S3 URLs for the presigned POST example
    if "amazonaws" in cap["url"] or ":9000" in cap["url"] or "localhost:9000" in cap["url"]:
        original_request["url"] = redact(cap["url"])

    if cap.get("request_body") and not (
        isinstance(cap["request_body"], dict) and cap["request_body"].get("form")
    ):
        if isinstance(cap["request_body"], (dict, list)):
            original_request["body"] = {
                "mode": "raw",
                "raw": json.dumps(redact(cap["request_body"]), indent=2),
                "options": {"raw": {"language": "json"}},
            }
        elif isinstance(cap["request_body"], dict) and "fields" in cap["request_body"]:
            original_request["body"] = {
                "mode": "formdata",
                "formdata": [
                    {"key": k, "value": "<redacted>" if k.lower() in {"policy", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token"} else str(v), "type": "text"}
                    for k, v in cap["request_body"]["fields"].items()
                ]
                + [{"key": "file", "type": "file", "src": "postman/fixtures/receipt.png"}],
            }

    return {
        "name": name,
        "originalRequest": original_request,
        "status": str(status),
        "code": status,
        "_postman_previewlanguage": "json",
        "header": headers,
        "body": body_str,
    }


def req(
    name: str,
    method: str,
    path: str,
    *,
    description: str,
    body: dict | None = None,
    headers: list | None = None,
    auth: bool = True,
    formdata: list | None = None,
    events: list | None = None,
    examples: list | None = None,
) -> dict:
    header = list(headers or [])
    if auth and not any(h["key"].lower() == "authorization" for h in header):
        header.insert(0, {"key": "Authorization", "value": "Bearer {{accessToken}}"})
    if body is not None and not any(h["key"].lower() == "content-type" for h in header):
        header.append({"key": "Content-Type", "value": "application/json"})

    request: dict = {
        "method": method,
        "header": header,
        "url": "{{baseUrl}}" + path,
        "description": description,
    }
    if body is not None:
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    if formdata is not None:
        request["body"] = {"mode": "formdata", "formdata": formdata}

    item: dict = {"name": name, "request": request, "response": examples or []}
    if events:
        item["event"] = events
    return item


def test_script(lines: list[str]) -> dict:
    return {
        "listen": "test",
        "script": {"type": "text/javascript", "exec": lines},
    }


def main() -> None:
    meta = load("_meta")
    collection = {
        "info": {
            "name": "Ahya Expense Tracker API",
            "description": (
                "Run Auth → Login first. Worker must be running for Notifications and Imports. "
                "Saved examples are real responses captured against a local stack; tokens and "
                "presign signatures are redacted. Regenerate with "
                "`python scripts/capture_postman_examples.py` then "
                "`python scripts/build_postman_collection.py`."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000"},
            {"key": "accessToken", "value": ""},
            {"key": "refreshToken", "value": ""},
            {"key": "expenseId", "value": meta.get("expense_id", "")},
            {"key": "categoryId", "value": meta.get("category_id", "")},
            {"key": "etag", "value": ""},
            {"key": "jobId", "value": meta.get("job_id", "")},
            {"key": "receiptKey", "value": meta.get("receipt_key", "")},
        ],
        "item": [],
    }

    # --- Health ---
    collection["item"].append(
        {
            "name": "Health",
            "item": [
                req(
                    "GET /health",
                    "GET",
                    "/health",
                    description="Liveness probe used by the load balancer.",
                    auth=False,
                    examples=[example_from(load("health_ok"), "200 OK")],
                )
            ],
        }
    )

    # --- Auth ---
    auth_items = [
        req(
            "Register",
            "POST",
            "/api/v1/auth/register",
            description="Create a user; base_currency must exist in exchange_rates.",
            auth=False,
            body={
                "email": "reviewer@example.com",
                "password": "Passw0rd-demo",
                "base_currency": "USD",
            },
            examples=[
                example_from(load("auth_register_201"), "201 Created"),
                example_from(load("auth_register_duplicate_409"), "409 Duplicate email"),
                example_from(load("auth_register_invalid_currency_422"), "422 Invalid base_currency"),
            ],
        ),
        req(
            "Login",
            "POST",
            "/api/v1/auth/login",
            description="Returns access JWT + opaque refresh; saves tokens in test script.",
            auth=False,
            body={"email": "reviewer@example.com", "password": "Passw0rd-demo"},
            events=[
                test_script(
                    [
                        "if (pm.response.code === 200) {",
                        "  const j = pm.response.json();",
                        "  pm.collectionVariables.set('accessToken', j.access_token);",
                        "  pm.collectionVariables.set('refreshToken', j.refresh_token);",
                        "}",
                    ]
                )
            ],
            examples=[
                example_from(load("auth_login_200"), "200 OK"),
                example_from(load("auth_login_wrong_password_401"), "401 Wrong password"),
                example_from(load("auth_login_rate_limited_429"), "429 Too many failed logins"),
            ],
        ),
        req(
            "Refresh",
            "POST",
            "/api/v1/auth/refresh",
            description="Rotates the opaque refresh token; reuse of the old token revokes all.",
            auth=False,
            body={"refresh_token": "{{refreshToken}}"},
            events=[
                test_script(
                    [
                        "if (pm.response.code === 200) {",
                        "  const j = pm.response.json();",
                        "  pm.collectionVariables.set('accessToken', j.access_token);",
                        "  pm.collectionVariables.set('refreshToken', j.refresh_token);",
                        "}",
                    ]
                )
            ],
            examples=[
                example_from(load("auth_refresh_200"), "200 OK"),
                example_from(load("auth_refresh_reuse_401"), "401 Refresh reuse (theft)"),
            ],
        ),
        req(
            "Logout",
            "POST",
            "/api/v1/auth/logout",
            description="Revokes the presented refresh token (204).",
            auth=False,
            body={"refresh_token": "{{refreshToken}}"},
            examples=[example_from(load("auth_logout_204"), "204 No Content")],
        ),
        req(
            "Protected without token",
            "GET",
            "/api/v1/categories",
            description="Missing Bearer token → 401.",
            auth=False,
            examples=[example_from(load("auth_protected_no_token_401"), "401 Unauthorized")],
        ),
    ]
    collection["item"].append({"name": "Auth", "item": auth_items})

    # --- Categories ---
    collection["item"].append(
        {
            "name": "Categories",
            "item": [
                req(
                    "Create category",
                    "POST",
                    "/api/v1/categories",
                    description="Create a category; saves categoryId.",
                    body={"name": "Food", "monthly_budget": "100.00"},
                    events=[
                        test_script(
                            [
                                "if (pm.response.code === 201) {",
                                "  pm.collectionVariables.set('categoryId', pm.response.json().id);",
                                "}",
                            ]
                        )
                    ],
                    examples=[
                        example_from(load("categories_create_201"), "201 Created"),
                        example_from(load("categories_duplicate_409"), "409 Duplicate name"),
                    ],
                ),
                req(
                    "List categories",
                    "GET",
                    "/api/v1/categories",
                    description="This-month spend vs budget (converted to base).",
                    examples=[example_from(load("categories_list_200"), "200 OK")],
                ),
            ],
        }
    )

    # --- Expenses ---
    expense_items = [
        req(
            "Create expense",
            "POST",
            "/api/v1/expenses",
            description="Create expense; Idempotency-Key {{$guid}}; saves expenseId.",
            headers=[
                {"key": "Authorization", "value": "Bearer {{accessToken}}"},
                {"key": "Idempotency-Key", "value": "{{$guid}}"},
                {"key": "Content-Type", "value": "application/json"},
            ],
            body={
                "category_id": "{{categoryId}}",
                "amount": "12.50",
                "currency": "USD",
                "description": "coffee",
                "spent_on": "2026-10-28",
            },
            events=[
                test_script(
                    [
                        "if (pm.response.code === 201) {",
                        "  pm.collectionVariables.set('expenseId', pm.response.json().id);",
                        "}",
                    ]
                )
            ],
            examples=[
                example_from(load("expenses_create_201"), "201 Created"),
                example_from(load("expenses_amount_zero_422"), "422 Amount zero"),
                example_from(load("expenses_amount_negative_422"), "422 Amount negative"),
                example_from(load("expenses_amount_3dp_422"), "422 More than two decimals"),
                example_from(load("expenses_future_date_422"), "422 Future spent_on"),
            ],
        ),
        req(
            "Create with fixed Idempotency-Key",
            "POST",
            "/api/v1/expenses",
            description="First create under a fixed key (for replay demos).",
            headers=[
                {"key": "Authorization", "value": "Bearer {{accessToken}}"},
                {"key": "Idempotency-Key", "value": "demo-fixed-key"},
                {"key": "Content-Type", "value": "application/json"},
            ],
            body={
                "category_id": "{{categoryId}}",
                "amount": "7.00",
                "currency": "USD",
                "description": "idem a",
                "spent_on": "2026-10-28",
            },
            examples=[
                example_from(load("expenses_create_idempotent_201"), "201 Created"),
                example_from(load("expenses_create_idempotent_replay"), "201 Idempotent replay"),
                example_from(load("expenses_create_idempotent_reuse_422"), "422 Key reused with different body"),
            ],
        ),
        req(
            "Get expense",
            "GET",
            "/api/v1/expenses/{{expenseId}}",
            description="Returns ETag; test script stores it in etag.",
            events=[
                test_script(
                    [
                        "const etag = pm.response.headers.get('ETag');",
                        "if (etag) { pm.collectionVariables.set('etag', etag); }",
                    ]
                )
            ],
            examples=[example_from(load("expenses_get_200"), "200 OK with ETag")],
        ),
        req(
            "List expenses (page 1)",
            "GET",
            "/api/v1/expenses?limit=2&category_id={{categoryId}}",
            description="Keyset page; follow next_cursor for page 2.",
            examples=[
                example_from(load("expenses_list_page1_200"), "200 Page 1"),
                example_from(load("expenses_list_invalid_cursor_400"), "400 Invalid cursor"),
            ],
        ),
        req(
            "List expenses (page 2)",
            "GET",
            "/api/v1/expenses?limit=2&cursor={{nextCursor}}",
            description="Second page via opaque cursor from page 1.",
            examples=[example_from(load("expenses_list_page2_200"), "200 Page 2")],
        ),
        req(
            "PATCH expense",
            "PATCH",
            "/api/v1/expenses/{{expenseId}}",
            description="Optimistic lock; sends If-Match {{etag}}.",
            headers=[
                {"key": "Authorization", "value": "Bearer {{accessToken}}"},
                {"key": "If-Match", "value": "{{etag}}"},
                {"key": "Content-Type", "value": "application/json"},
            ],
            body={"description": "coffee updated"},
            events=[
                test_script(
                    [
                        "const etag = pm.response.headers.get('ETag');",
                        "if (etag) { pm.collectionVariables.set('etag', etag); }",
                    ]
                )
            ],
            examples=[
                example_from(load("expenses_patch_200"), "200 OK"),
                example_from(load("expenses_patch_stale_412"), "412 Stale ETag"),
                example_from(load("expenses_patch_no_if_match_428"), "428 Missing If-Match"),
            ],
        ),
        req(
            "Other user's expense",
            "GET",
            "/api/v1/expenses/{{expenseId}}",
            description="Ownership filter returns 404 (never 403).",
            examples=[example_from(load("expenses_other_user_404"), "404 Not found")],
        ),
        req(
            "Delete expense",
            "DELETE",
            "/api/v1/expenses/{{expenseId}}",
            description="Soft-ownership delete; enqueues receipt_key if present.",
            examples=[example_from(load("expenses_delete_204"), "204 No Content")],
        ),
    ]
    collection["item"].append({"name": "Expenses", "item": expense_items})

    # --- Reports ---
    collection["item"].append(
        {
            "name": "Reports",
            "item": [
                req(
                    "Monthly report",
                    "GET",
                    "/api/v1/reports/monthly?year=2026&month=10",
                    description="One-SQL monthly report with MoM change.",
                    examples=[
                        example_from(load("reports_monthly_200"), "200 OK"),
                        example_from(load("reports_monthly_invalid_month_422"), "422 Invalid month"),
                    ],
                )
            ],
        }
    )

    # --- Notifications ---
    collection["item"].append(
        {
            "name": "Notifications",
            "item": [
                req(
                    "List notifications",
                    "GET",
                    "/api/v1/notifications",
                    description="Budget alerts from the outbox worker (needs worker running).",
                    examples=[example_from(load("notifications_list_200"), "200 OK")],
                )
            ],
        }
    )

    # --- Imports ---
    collection["item"].append(
        {
            "name": "Imports",
            "item": [
                req(
                    "POST CSV import",
                    "POST",
                    "/api/v1/expenses/import",
                    description="Multipart field 'file'; returns job_id (202).",
                    formdata=[
                        {
                            "key": "file",
                            "type": "file",
                            "src": "postman/sample_import.csv",
                        }
                    ],
                    events=[
                        test_script(
                            [
                                "if (pm.response.code === 202) {",
                                "  pm.collectionVariables.set('jobId', pm.response.json().job_id);",
                                "}",
                            ]
                        )
                    ],
                    examples=[
                        example_from(load("imports_post_202"), "202 Accepted"),
                        example_from(load("imports_post_same_file_202"), "202 Same file_hash"),
                    ],
                ),
                req(
                    "GET import job",
                    "GET",
                    "/api/v1/imports/{{jobId}}",
                    description="Job status, counts, and capped error list.",
                    examples=[
                        example_from(load("imports_status_completed_200"), "200 Completed")
                    ],
                ),
            ],
        }
    )

    # --- Receipts ---
    s3_cap = load("receipts_presigned_s3_post")
    s3_example = example_from(s3_cap, "204 Uploaded to S3")
    collection["item"].append(
        {
            "name": "Receipts",
            "item": [
                req(
                    "Get upload URL",
                    "POST",
                    "/api/v1/expenses/{{expenseId}}/receipt/upload-url",
                    description="Presigned POST fields; saves receiptKey.",
                    body={"content_type": "image/png"},
                    events=[
                        test_script(
                            [
                                "if (pm.response.code === 200) {",
                                "  pm.collectionVariables.set('receiptKey', pm.response.json().key);",
                                "}",
                            ]
                        )
                    ],
                    examples=[example_from(load("receipts_upload_url_200"), "200 OK")],
                ),
                {
                    "name": "Upload file to S3 (presigned POST, not our API)",
                    "request": {
                        "method": "POST",
                        "header": [],
                        "body": {
                            "mode": "formdata",
                            "formdata": [
                                {"key": "key", "value": "{{receiptKey}}", "type": "text"},
                                {"key": "Content-Type", "value": "image/png", "type": "text"},
                                {"key": "policy", "value": "<redacted>", "type": "text"},
                                {"key": "x-amz-algorithm", "value": "AWS4-HMAC-SHA256", "type": "text"},
                                {"key": "x-amz-credential", "value": "<redacted>", "type": "text"},
                                {"key": "x-amz-date", "value": "{{$timestamp}}", "type": "text"},
                                {"key": "x-amz-signature", "value": "<redacted>", "type": "text"},
                                {
                                    "key": "file",
                                    "type": "file",
                                    "src": "postman/fixtures/receipt.png",
                                },
                            ],
                        },
                        "url": "{{s3UploadUrl}}",
                        "description": "Browser/host POSTs the file to S3 with the fields from upload-url. Not an API route.",
                    },
                    "response": [s3_example],
                },
                req(
                    "Confirm receipt",
                    "POST",
                    "/api/v1/expenses/{{expenseId}}/receipt/confirm",
                    description="head + magic bytes; bumps ETag on success.",
                    body={"key": "{{receiptKey}}"},
                    examples=[
                        example_from(load("receipts_confirm_200"), "200 Confirmed"),
                        example_from(
                            load("receipts_confirm_invalid_type_422"),
                            "422 INVALID_FILE_TYPE",
                        ),
                    ],
                ),
                req(
                    "Download receipt URL",
                    "GET",
                    "/api/v1/expenses/{{expenseId}}/receipt",
                    description="15-minute presigned GET (X-Amz-Expires=900).",
                    examples=[example_from(load("receipts_download_200"), "200 OK")],
                ),
            ],
        }
    )

    # Add s3UploadUrl variable empty
    collection["variable"].append({"key": "s3UploadUrl", "value": ""})
    collection["variable"].append({"key": "nextCursor", "value": ""})

    redacted = redact(collection)
    OUT.write_text(json.dumps(redacted, indent=2) + "\n")
    print(f"wrote {OUT}")

    ENV_OUT.write_text(
        json.dumps(
            {
                "id": "ahya-local-env",
                "name": "Ahya Local",
                "values": [
                    {"key": "baseUrl", "value": "http://localhost:8000", "enabled": True, "type": "default"},
                    {"key": "accessToken", "value": "", "enabled": True, "type": "secret"},
                    {"key": "refreshToken", "value": "", "enabled": True, "type": "secret"},
                    {"key": "categoryId", "value": "", "enabled": True, "type": "default"},
                    {"key": "expenseId", "value": "", "enabled": True, "type": "default"},
                    {"key": "etag", "value": "", "enabled": True, "type": "default"},
                    {"key": "jobId", "value": "", "enabled": True, "type": "default"},
                    {"key": "receiptKey", "value": "", "enabled": True, "type": "default"},
                    {"key": "s3UploadUrl", "value": "", "enabled": True, "type": "default"},
                    {"key": "nextCursor", "value": "", "enabled": True, "type": "default"},
                ],
                "_postman_variable_scope": "environment",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {ENV_OUT}")


if __name__ == "__main__":
    main()
