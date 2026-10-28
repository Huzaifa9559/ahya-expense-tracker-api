#!/usr/bin/env python3
"""
Hit the live API at http://localhost:8000 and save request/response pairs
to postman/captured/*.json for building the Postman collection examples.

Re-runnable: uses reviewer+<timestamp>@example.com and Passw0rd-demo.
Requires: docker compose stack up (api, worker, postgres, redis, s3) + alembic upgrade head.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

BASE = "http://localhost:8000"
S3_PUBLIC = "http://localhost:9000"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postman" / "captured"
FIXTURES = ROOT / "postman" / "fixtures"
SAMPLE_CSV = ROOT / "postman" / "sample_import.csv"
PASSWORD = "Passw0rd-demo"


def _save(name: str, payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"  wrote {path.name} ({payload.get('status_code')})")


async def _capture(
    client: httpx.AsyncClient,
    name: str,
    *,
    method: str,
    url: str,
    headers: dict | None = None,
    json_body: dict | list | None = None,
    content: bytes | None = None,
    files: dict | None = None,
    data: dict | None = None,
) -> httpx.Response:
    hdrs = dict(headers or {})
    kwargs: dict = {"method": method, "url": url, "headers": hdrs}
    req_body: object | None = None
    if files is not None:
        kwargs["files"] = files
        if data:
            kwargs["data"] = data
        req_body = {"form": True, "files": list(files.keys()), "data": data}
    elif content is not None:
        kwargs["content"] = content
        req_body = "<binary>"
    elif json_body is not None:
        kwargs["json"] = json_body
        req_body = json_body

    resp = await client.request(**kwargs)
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:2000]

    _save(
        name,
        {
            "name": name,
            "method": method,
            "url": url,
            "request_headers": {k: v for k, v in hdrs.items()},
            "request_body": req_body,
            "status_code": resp.status_code,
            "response_headers": dict(resp.headers),
            "response_body": body,
        },
    )
    return resp


async def main() -> None:
    ts = int(time.time())
    email = f"reviewer+{ts}@example.com"
    throwaway = f"ratelimit+{ts}@example.com"
    theft_email = f"theft+{ts}@example.com"
    # API container uses UTC; host local date can be "tomorrow" and fail FUTURE_SPENT_ON.
    today = datetime.now(UTC).date().isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"capturing against {BASE} as {email} spent_on={today}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        # --- Health ---
        await _capture(client, "health_ok", method="GET", url="/health")

        # --- Auth ---
        await _capture(
            client,
            "auth_register_201",
            method="POST",
            url="/api/v1/auth/register",
            json_body={
                "email": email,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        await _capture(
            client,
            "auth_register_duplicate_409",
            method="POST",
            url="/api/v1/auth/register",
            json_body={
                "email": email,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        await _capture(
            client,
            "auth_register_invalid_currency_422",
            method="POST",
            url="/api/v1/auth/register",
            json_body={
                "email": f"badcur+{ts}@example.com",
                "password": PASSWORD,
                "base_currency": "ZZZ",
            },
        )
        login = await _capture(
            client,
            "auth_login_200",
            method="POST",
            url="/api/v1/auth/login",
            json_body={"email": email, "password": PASSWORD},
        )
        tokens = login.json()
        access = tokens["access_token"]
        refresh = tokens["refresh_token"]
        auth = {"Authorization": f"Bearer {access}"}

        await _capture(
            client,
            "auth_login_wrong_password_401",
            method="POST",
            url="/api/v1/auth/login",
            json_body={"email": email, "password": "wrong-password"},
        )

        # Rate limit on a separate throwaway account
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": throwaway,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        for i in range(5):
            await client.post(
                "/api/v1/auth/login",
                json={"email": throwaway, "password": "wrong-password"},
            )
        await _capture(
            client,
            "auth_login_rate_limited_429",
            method="POST",
            url="/api/v1/auth/login",
            json_body={"email": throwaway, "password": "wrong-password"},
        )

        refreshed = await _capture(
            client,
            "auth_refresh_200",
            method="POST",
            url="/api/v1/auth/refresh",
            json_body={"refresh_token": refresh},
        )
        new_tokens = refreshed.json()
        access = new_tokens["access_token"]
        refresh = new_tokens["refresh_token"]
        auth = {"Authorization": f"Bearer {access}"}
        old_refresh = tokens["refresh_token"]

        # Theft path on a separate login so main session survives
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": theft_email,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        theft_login = await client.post(
            "/api/v1/auth/login",
            json={"email": theft_email, "password": PASSWORD},
        )
        theft_refresh = theft_login.json()["refresh_token"]
        theft_rot = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": theft_refresh},
        )
        assert theft_rot.status_code == 200
        await _capture(
            client,
            "auth_refresh_reuse_401",
            method="POST",
            url="/api/v1/auth/refresh",
            json_body={"refresh_token": theft_refresh},
        )

        # Logout a disposable session (register+login again) so we keep main tokens
        logout_email = f"logout+{ts}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": logout_email,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        lo = await client.post(
            "/api/v1/auth/login",
            json={"email": logout_email, "password": PASSWORD},
        )
        await _capture(
            client,
            "auth_logout_204",
            method="POST",
            url="/api/v1/auth/logout",
            json_body={"refresh_token": lo.json()["refresh_token"]},
        )
        await _capture(
            client,
            "auth_protected_no_token_401",
            method="GET",
            url="/api/v1/categories",
        )

        # --- Categories ---
        cat = await _capture(
            client,
            "categories_create_201",
            method="POST",
            url="/api/v1/categories",
            headers=auth,
            json_body={"name": "Food", "monthly_budget": "100.00"},
        )
        category_id = cat.json()["id"]
        await _capture(
            client,
            "categories_duplicate_409",
            method="POST",
            url="/api/v1/categories",
            headers=auth,
            json_body={"name": "Food", "monthly_budget": "100.00"},
        )

        # Seed spend for list percent_used (and later notifications)
        await client.post(
            "/api/v1/expenses",
            headers=auth,
            json={
                "category_id": category_id,
                "amount": "21.00",
                "currency": "USD",
                "description": "list spend",
                "spent_on": today,
            },
        )
        await _capture(
            client,
            "categories_list_200",
            method="GET",
            url="/api/v1/categories",
            headers=auth,
        )

        # --- Expenses ---
        exp = await _capture(
            client,
            "expenses_create_201",
            method="POST",
            url="/api/v1/expenses",
            headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
            json_body={
                "category_id": category_id,
                "amount": "12.50",
                "currency": "USD",
                "description": "coffee",
                "spent_on": today,
            },
        )
        expense_id = exp.json()["id"]

        idem_key = f"demo-idem-{ts}"
        body_a = {
            "category_id": category_id,
            "amount": "7.00",
            "currency": "USD",
            "description": "idem a",
            "spent_on": today,
        }
        await _capture(
            client,
            "expenses_create_idempotent_201",
            method="POST",
            url="/api/v1/expenses",
            headers={**auth, "Idempotency-Key": idem_key},
            json_body=body_a,
        )
        await _capture(
            client,
            "expenses_create_idempotent_replay",
            method="POST",
            url="/api/v1/expenses",
            headers={**auth, "Idempotency-Key": idem_key},
            json_body=body_a,
        )
        await _capture(
            client,
            "expenses_create_idempotent_reuse_422",
            method="POST",
            url="/api/v1/expenses",
            headers={**auth, "Idempotency-Key": idem_key},
            json_body={**body_a, "amount": "8.00"},
        )

        for name, amount in [
            ("expenses_amount_zero_422", "0"),
            ("expenses_amount_negative_422", "-1.00"),
            ("expenses_amount_3dp_422", "1.123"),
        ]:
            await _capture(
                client,
                name,
                method="POST",
                url="/api/v1/expenses",
                headers=auth,
                json_body={
                    "category_id": category_id,
                    "amount": amount,
                    "currency": "USD",
                    "spent_on": today,
                },
            )
        await _capture(
            client,
            "expenses_future_date_422",
            method="POST",
            url="/api/v1/expenses",
            headers=auth,
            json_body={
                "category_id": category_id,
                "amount": "1.00",
                "currency": "USD",
                "spent_on": "2099-01-01",
            },
        )

        got = await _capture(
            client,
            "expenses_get_200",
            method="GET",
            url=f"/api/v1/expenses/{expense_id}",
            headers=auth,
        )
        etag = got.headers.get("etag") or got.headers.get("ETag")

        # Extra rows for pagination
        for i in range(3):
            await client.post(
                "/api/v1/expenses",
                headers=auth,
                json={
                    "category_id": category_id,
                    "amount": f"{1 + i}.00",
                    "currency": "USD",
                    "description": f"page {i}",
                    "spent_on": today,
                },
            )
        page1 = await _capture(
            client,
            "expenses_list_page1_200",
            method="GET",
            url=f"/api/v1/expenses?limit=2&category_id={category_id}",
            headers=auth,
        )
        next_cursor = page1.json().get("next_cursor")
        if next_cursor:
            await _capture(
                client,
                "expenses_list_page2_200",
                method="GET",
                url=f"/api/v1/expenses?limit=2&cursor={next_cursor}",
                headers=auth,
            )
        await _capture(
            client,
            "expenses_list_invalid_cursor_400",
            method="GET",
            url="/api/v1/expenses?cursor=not-a-cursor",
            headers=auth,
        )

        patched = await _capture(
            client,
            "expenses_patch_200",
            method="PATCH",
            url=f"/api/v1/expenses/{expense_id}",
            headers={**auth, "If-Match": etag or '"1"'},
            json_body={"description": "coffee updated"},
        )
        new_etag = patched.headers.get("etag") or patched.headers.get("ETag")
        await _capture(
            client,
            "expenses_patch_stale_412",
            method="PATCH",
            url=f"/api/v1/expenses/{expense_id}",
            headers={**auth, "If-Match": etag or '"1"'},
            json_body={"description": "stale"},
        )
        await _capture(
            client,
            "expenses_patch_no_if_match_428",
            method="PATCH",
            url=f"/api/v1/expenses/{expense_id}",
            headers=auth,
            json_body={"description": "missing if-match"},
        )

        other_email = f"other+{ts}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": other_email,
                "password": PASSWORD,
                "base_currency": "USD",
            },
        )
        other_login = await client.post(
            "/api/v1/auth/login",
            json={"email": other_email, "password": PASSWORD},
        )
        other_auth = {
            "Authorization": f"Bearer {other_login.json()['access_token']}"
        }
        await _capture(
            client,
            "expenses_other_user_404",
            method="GET",
            url=f"/api/v1/expenses/{expense_id}",
            headers=other_auth,
        )

        del_exp = await client.post(
            "/api/v1/expenses",
            headers=auth,
            json={
                "category_id": category_id,
                "amount": "3.00",
                "currency": "USD",
                "description": "to delete",
                "spent_on": today,
            },
        )
        await _capture(
            client,
            "expenses_delete_204",
            method="DELETE",
            url=f"/api/v1/expenses/{del_exp.json()['id']}",
            headers=auth,
        )

        # --- Reports (mixed currencies) ---
        await client.post(
            "/api/v1/expenses",
            headers=auth,
            json={
                "category_id": category_id,
                "amount": "10.00",
                "currency": "EUR",
                "description": "eur report",
                "spent_on": today,
            },
        )
        utc_today = datetime.now(UTC).date()
        y, m = utc_today.year, utc_today.month
        await _capture(
            client,
            "reports_monthly_200",
            method="GET",
            url=f"/api/v1/reports/monthly?year={y}&month={m}",
            headers=auth,
        )
        await _capture(
            client,
            "reports_monthly_invalid_month_422",
            method="GET",
            url=f"/api/v1/reports/monthly?year={y}&month=13",
            headers=auth,
        )

        # --- Notifications: push over 80% and 100% ---
        alert_cat = await client.post(
            "/api/v1/categories",
            headers=auth,
            json={"name": "Alerts", "monthly_budget": "100.00"},
        )
        alert_id = alert_cat.json()["id"]
        await client.post(
            "/api/v1/expenses",
            headers=auth,
            json={
                "category_id": alert_id,
                "amount": "85.00",
                "currency": "USD",
                "description": "hit 80",
                "spent_on": today,
            },
        )
        await client.post(
            "/api/v1/expenses",
            headers=auth,
            json={
                "category_id": alert_id,
                "amount": "20.00",
                "currency": "USD",
                "description": "hit 100",
                "spent_on": today,
            },
        )
        notes = None
        for _ in range(20):
            await asyncio.sleep(1)
            poll = await client.get("/api/v1/notifications", headers=auth)
            items = poll.json().get("items") or poll.json()
            if isinstance(items, list) and len(items) >= 1:
                notes = poll
                break
        if notes is None:
            notes = await client.get("/api/v1/notifications", headers=auth)
        _save(
            "notifications_list_200",
            {
                "name": "notifications_list_200",
                "method": "GET",
                "url": "/api/v1/notifications",
                "request_headers": auth,
                "request_body": None,
                "status_code": notes.status_code,
                "response_headers": dict(notes.headers),
                "response_body": notes.json(),
            },
        )
        print(f"  wrote notifications_list_200.json ({notes.status_code})")

        # --- Imports ---
        with SAMPLE_CSV.open("rb") as f:
            imp = await _capture(
                client,
                "imports_post_202",
                method="POST",
                url="/api/v1/expenses/import",
                headers=auth,
                files={"file": ("sample_import.csv", f, "text/csv")},
            )
        job_id = imp.json()["job_id"]
        completed = None
        for _ in range(30):
            await asyncio.sleep(1)
            st = await client.get(f"/api/v1/imports/{job_id}", headers=auth)
            body = st.json()
            if body.get("status") in {"completed", "failed"}:
                completed = st
                break
        if completed is None:
            completed = await client.get(f"/api/v1/imports/{job_id}", headers=auth)
        _save(
            "imports_status_completed_200",
            {
                "name": "imports_status_completed_200",
                "method": "GET",
                "url": f"/api/v1/imports/{job_id}",
                "request_headers": auth,
                "request_body": None,
                "status_code": completed.status_code,
                "response_headers": dict(completed.headers),
                "response_body": completed.json(),
            },
        )
        print(f"  wrote imports_status_completed_200.json ({completed.status_code})")
        with SAMPLE_CSV.open("rb") as f:
            await _capture(
                client,
                "imports_post_same_file_202",
                method="POST",
                url="/api/v1/expenses/import",
                headers=auth,
                files={"file": ("sample_import.csv", f, "text/csv")},
            )

        # --- Receipts ---
        upload = await _capture(
            client,
            "receipts_upload_url_200",
            method="POST",
            url=f"/api/v1/expenses/{expense_id}/receipt/upload-url",
            headers=auth,
            json_body={"content_type": "image/png"},
        )
        up = upload.json()
        receipt_key = up["key"]
        # Perform real presigned POST to LocalStack
        png_bytes = (FIXTURES / "receipt.png").read_bytes()
        form = dict(up["fields"])
        files = {"file": ("receipt.png", png_bytes, "image/png")}
        # LocalStack may need path-style URL already in `url`
        async with httpx.AsyncClient(timeout=60.0) as s3client:
            s3_resp = await s3client.post(up["url"], data=form, files=files)
        _save(
            "receipts_presigned_s3_post",
            {
                "name": "receipts_presigned_s3_post",
                "method": "POST",
                "url": up["url"],
                "request_headers": {},
                "request_body": {
                    "fields": form,
                    "file": "postman/fixtures/receipt.png",
                },
                "status_code": s3_resp.status_code,
                "response_headers": dict(s3_resp.headers),
                "response_body": s3_resp.text[:500],
                "receipt_key": receipt_key,
            },
        )
        print(f"  wrote receipts_presigned_s3_post.json ({s3_resp.status_code})")

        await _capture(
            client,
            "receipts_confirm_200",
            method="POST",
            url=f"/api/v1/expenses/{expense_id}/receipt/confirm",
            headers=auth,
            json_body={"key": receipt_key},
        )
        await _capture(
            client,
            "receipts_download_200",
            method="GET",
            url=f"/api/v1/expenses/{expense_id}/receipt",
            headers=auth,
        )

        # Fake PNG confirm
        fake_key = receipt_key.rsplit("/", 1)[0] + f"/{uuid.uuid4()}.png"
        # Put fake via upload-url path: get new url then upload MZ bytes, or put via boto
        # Easiest: use another upload-url then overwrite isn't possible; put via aioboto3 sync
        from app.storage import s3 as s3mod
        import io

        await s3mod.put_fileobj(fake_key, io.BytesIO((FIXTURES / "fake.png").read_bytes()))
        await _capture(
            client,
            "receipts_confirm_invalid_type_422",
            method="POST",
            url=f"/api/v1/expenses/{expense_id}/receipt/confirm",
            headers=auth,
            json_body={"key": fake_key},
        )

        # Persist ids for collection builder
        _save(
            "_meta",
            {
                "email": email,
                "password": PASSWORD,
                "category_id": category_id,
                "expense_id": expense_id,
                "job_id": job_id,
                "receipt_key": receipt_key,
                "etag": new_etag,
            },
        )
        print("done")


if __name__ == "__main__":
    asyncio.run(main())
