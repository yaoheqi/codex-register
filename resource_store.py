# -*- coding: utf-8 -*-
"""统一管理邮箱与 CDK 的 SQLite 数据层。"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "resources.sqlite3"

GPT_LOGIN_MAIL_FILE = ROOT.parent / "gpt-login" / "mail.csv"
EMAIL_UNUSED_FILE = DATA_DIR / "emails_unused" / "mail.csv"
EMAIL_USED_FILE = DATA_DIR / "emails_used" / "mail.csv"
CDK_UNUSED_FILE = DATA_DIR / "cdks_unused" / "cdks.txt"
CDK_USED_FILE = DATA_DIR / "cdks_used" / "cdks.txt"

EMAIL_STATUS_UNREGISTERED = "unregistered"
EMAIL_STATUS_REGISTERED = "registered"
EMAIL_STATUS_RECEIVED = "received"
EMAIL_STATUSES = {
    EMAIL_STATUS_UNREGISTERED,
    EMAIL_STATUS_REGISTERED,
    EMAIL_STATUS_RECEIVED,
}
EMAIL_STATUS_PRIORITY = {
    EMAIL_STATUS_UNREGISTERED: 0,
    EMAIL_STATUS_REGISTERED: 1,
    EMAIL_STATUS_RECEIVED: 2,
}
EMAIL_SOURCE_MAPPING_VERSION = "2026-06-02-email-source-files-v2"
SALE_STATUS_UNSOLD = "unsold"
SALE_STATUS_SOLD = "sold"
SALE_STATUSES = {SALE_STATUS_UNSOLD, SALE_STATUS_SOLD}
CDK_STATUS_UNUSED = "unused"
CDK_STATUS_USED = "used"
CDK_STATUSES = {CDK_STATUS_UNUSED, CDK_STATUS_USED}


def now_ts() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS emails (
              email TEXT PRIMARY KEY,
              raw TEXT NOT NULL DEFAULT '',
              password TEXT NOT NULL DEFAULT '',
              client_id TEXT NOT NULL DEFAULT '',
              refresh_token TEXT NOT NULL DEFAULT '',
              register_status TEXT NOT NULL DEFAULT 'unregistered'
                CHECK (register_status IN ('unregistered', 'registered', 'received')),
              sale_status TEXT NOT NULL DEFAULT 'unsold'
                CHECK (sale_status IN ('unsold', 'sold')),
              reserved_by TEXT NOT NULL DEFAULT '',
              reserved_at INTEGER,
              registered_at INTEGER,
              code_received_at INTEGER,
              sold_at INTEGER,
              last_cdk TEXT NOT NULL DEFAULT '',
              last_task_id TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_emails_status
              ON emails (register_status, sale_status, reserved_at, updated_at);

            CREATE TABLE IF NOT EXISTS cdks (
              cdk TEXT PRIMARY KEY,
              status TEXT NOT NULL DEFAULT 'unused'
                CHECK (status IN ('unused', 'used')),
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cdks_status
              ON cdks (status, updated_at);

            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        done = conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("legacy_import_done",),
        ).fetchone()
        if not done:
            import_legacy_sources(conn)
            conn.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("legacy_import_done", str(now_ts())),
            )
        source_mapping = conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("email_source_mapping_version",),
        ).fetchone()
        if not source_mapping or str(source_mapping["value"]) != EMAIL_SOURCE_MAPPING_VERSION:
            sync_source_files(conn)
            conn.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("email_source_mapping_version", EMAIL_SOURCE_MAPPING_VERSION),
            )


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    values: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value:
            values.append(value)
    return values


def email_key(value: str) -> str:
    return str(value or "").strip().lower()


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "已"}


def parse_email_record(line: str) -> dict[str, str] | None:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = [part.strip() for part in text.split("----")]
    if len(parts) < 4:
        return None
    email = parts[0]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None
    refresh_token = "----".join(parts[3:]).strip()
    if not refresh_token:
        return None
    return {
        "raw": text,
        "email": email,
        "password": parts[1],
        "client_id": parts[2],
        "refresh_token": refresh_token,
    }


def better_status(current: str, incoming: str) -> str:
    current = current if current in EMAIL_STATUSES else EMAIL_STATUS_UNREGISTERED
    incoming = incoming if incoming in EMAIL_STATUSES else EMAIL_STATUS_UNREGISTERED
    if EMAIL_STATUS_PRIORITY[incoming] > EMAIL_STATUS_PRIORITY[current]:
        return incoming
    return current


def normalize_sale_status(value: str | None) -> str:
    return value if value in SALE_STATUSES else SALE_STATUS_UNSOLD


def normalize_cdk_status(value: str | None) -> str:
    return value if value in CDK_STATUSES else CDK_STATUS_UNUSED


def normalize_page(page: Any = 1, page_size: Any = 20) -> tuple[int, int]:
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        page_number = 1
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        size = 20
    page_number = max(1, page_number)
    size = min(100, max(1, size))
    return page_number, size


def import_legacy_sources(conn: sqlite3.Connection) -> None:
    sync_source_files(conn)
    import_cdk_file(conn, CDK_UNUSED_FILE, CDK_STATUS_UNUSED)
    import_cdk_file(conn, CDK_USED_FILE, CDK_STATUS_USED)


def email_source_files() -> tuple[tuple[Path, str], ...]:
    return (
        (GPT_LOGIN_MAIL_FILE, EMAIL_STATUS_UNREGISTERED),
        (EMAIL_UNUSED_FILE, EMAIL_STATUS_REGISTERED),
        (EMAIL_USED_FILE, EMAIL_STATUS_RECEIVED),
    )


def import_email_file(
    conn: sqlite3.Connection,
    path: Path,
    default_status: str,
) -> dict[str, int]:
    imported = 0
    skipped = 0
    for line in read_lines(path):
        if str(line or "").strip().startswith("#"):
            continue
        account = parse_email_record(line)
        if account:
            upsert_email(conn, account, register_status=default_status)
            imported += 1
        else:
            skipped += 1
    return {"imported": imported, "skipped": skipped}


def sync_source_files(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    if conn is None:
        with connect() as local_conn:
            return sync_source_files(local_conn)

    summary = {
        "emails_unregistered": 0,
        "emails_registered": 0,
        "emails_received": 0,
        "emails_skipped": 0,
    }
    for path, status in email_source_files():
        result = import_email_file(conn, path, status)
        if status == EMAIL_STATUS_UNREGISTERED:
            summary["emails_unregistered"] += result["imported"]
        elif status == EMAIL_STATUS_REGISTERED:
            summary["emails_registered"] += result["imported"]
        elif status == EMAIL_STATUS_RECEIVED:
            summary["emails_received"] += result["imported"]
        summary["emails_skipped"] += result["skipped"]
    return summary


def import_cdk_file(conn: sqlite3.Connection, path: Path, status: str) -> None:
    for value in read_lines(path):
        upsert_cdk(conn, value, status)


def upsert_email(
    conn: sqlite3.Connection,
    account: dict[str, str],
    register_status: str = EMAIL_STATUS_UNREGISTERED,
    sale_status: str = SALE_STATUS_UNSOLD,
    registered_at: Any = None,
    code_received_at: Any = None,
    sold_at: Any = None,
    last_cdk: str = "",
    last_task_id: str = "",
    created_at: Any = None,
    updated_at: Any = None,
) -> None:
    key = email_key(account.get("email") or "")
    if not key:
        return
    now = now_ts()
    row = conn.execute("SELECT * FROM emails WHERE email = ?", (key,)).fetchone()
    register_status = register_status if register_status in EMAIL_STATUSES else EMAIL_STATUS_UNREGISTERED
    sale_status = normalize_sale_status(sale_status)
    created = int(created_at or now)
    updated = int(updated_at or now)
    if not row:
        conn.execute(
            """
            INSERT INTO emails (
              email, raw, password, client_id, refresh_token, register_status,
              sale_status, registered_at, code_received_at, sold_at, last_cdk,
              last_task_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                account.get("raw") or account.get("email") or key,
                account.get("password") or "",
                account.get("client_id") or "",
                account.get("refresh_token") or "",
                register_status,
                sale_status,
                int(registered_at) if registered_at else None,
                int(code_received_at) if code_received_at else None,
                int(sold_at) if sold_at else None,
                last_cdk or "",
                last_task_id or "",
                created,
                updated,
            ),
        )
        return

    next_status = better_status(str(row["register_status"]), register_status)
    next_sale = SALE_STATUS_SOLD if str(row["sale_status"]) == SALE_STATUS_SOLD or sale_status == SALE_STATUS_SOLD else SALE_STATUS_UNSOLD
    conn.execute(
        """
        UPDATE emails
           SET raw = CASE WHEN raw = '' THEN ? ELSE raw END,
               password = CASE WHEN password = '' THEN ? ELSE password END,
               client_id = CASE WHEN client_id = '' THEN ? ELSE client_id END,
               refresh_token = CASE WHEN refresh_token = '' THEN ? ELSE refresh_token END,
               register_status = ?,
               sale_status = ?,
               registered_at = COALESCE(registered_at, ?),
               code_received_at = COALESCE(code_received_at, ?),
               sold_at = COALESCE(sold_at, ?),
               last_cdk = CASE WHEN ? != '' THEN ? ELSE last_cdk END,
               last_task_id = CASE WHEN ? != '' THEN ? ELSE last_task_id END,
               updated_at = MAX(updated_at, ?)
         WHERE email = ?
        """,
        (
            account.get("raw") or account.get("email") or key,
            account.get("password") or "",
            account.get("client_id") or "",
            account.get("refresh_token") or "",
            next_status,
            next_sale,
            int(registered_at) if registered_at else None,
            int(code_received_at) if code_received_at else None,
            int(sold_at) if sold_at else None,
            last_cdk or "",
            last_cdk or "",
            last_task_id or "",
            last_task_id or "",
            updated,
            key,
        ),
    )


def upsert_cdk(conn: sqlite3.Connection, cdk: str, status: str = CDK_STATUS_UNUSED) -> None:
    value = str(cdk or "").strip()
    if not value:
        return
    status = normalize_cdk_status(status)
    now = now_ts()
    row = conn.execute("SELECT status FROM cdks WHERE cdk = ?", (value,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO cdks (cdk, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (value, status, now, now),
        )
        return
    if row["status"] != CDK_STATUS_USED and status == CDK_STATUS_USED:
        conn.execute(
            "UPDATE cdks SET status = ?, updated_at = ? WHERE cdk = ?",
            (CDK_STATUS_USED, now, value),
        )


def row_to_email_record(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["register_status"])
    sale_status = str(row["sale_status"])
    raw = str(row["raw"] or "").strip()
    return {
        "email": row["email"],
        "raw": raw or row["email"],
        "password": row["password"] or "",
        "client_id": row["client_id"] or "",
        "refresh_token": row["refresh_token"] or "",
        "register_status": status,
        "sale_status": sale_status,
        "is_registered": status in {EMAIL_STATUS_REGISTERED, EMAIL_STATUS_RECEIVED},
        "has_received_code": status == EMAIL_STATUS_RECEIVED,
        "is_sold": sale_status == SALE_STATUS_SOLD,
        "registered_at": row["registered_at"],
        "code_received_at": row["code_received_at"],
        "sold_at": row["sold_at"],
        "last_cdk": row["last_cdk"] or "",
        "last_task_id": row["last_task_id"] or "",
        "reserved_by": row["reserved_by"] or "",
        "reserved_at": row["reserved_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def load_email_records() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM emails").fetchall()
    return {str(row["email"]): row_to_email_record(row) for row in rows}


def available_email_accounts(active_keys: set[str] | None = None) -> list[dict[str, Any]]:
    active_keys = active_keys or set()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM emails
             WHERE register_status = ?
               AND sale_status = ?
               AND (reserved_at IS NULL OR reserved_at = 0)
             ORDER BY created_at ASC, email ASC
            """,
            (EMAIL_STATUS_REGISTERED, SALE_STATUS_UNSOLD),
        ).fetchall()
    return [row_to_email_record(row) for row in rows if str(row["email"]) not in active_keys]


def record_email_status(email_record: str, **updates: Any) -> dict[str, Any]:
    account = parse_email_record(email_record)
    if not account:
        raise ValueError("邮箱格式应为 email----password----client_id----refresh_token")
    key = email_key(account["email"])
    now = now_ts()
    with connect() as conn:
        upsert_email(conn, account)
        row = conn.execute("SELECT * FROM emails WHERE email = ?", (key,)).fetchone()
        current_status = str(row["register_status"]) if row else EMAIL_STATUS_UNREGISTERED
        next_status = current_status
        if "has_received_code" in updates and bool_value(updates["has_received_code"]):
            next_status = EMAIL_STATUS_RECEIVED
        elif "is_registered" in updates and bool_value(updates["is_registered"]):
            next_status = better_status(current_status, EMAIL_STATUS_REGISTERED)
        elif updates.get("register_status") in EMAIL_STATUSES:
            next_status = str(updates["register_status"])

        sale_status = SALE_STATUS_SOLD if bool_value(updates.get("is_sold")) else None
        set_parts = ["register_status = ?", "updated_at = ?", "reserved_by = ''", "reserved_at = NULL"]
        params: list[Any] = [next_status, now]
        if next_status in {EMAIL_STATUS_REGISTERED, EMAIL_STATUS_RECEIVED}:
            set_parts.append("registered_at = COALESCE(registered_at, ?)")
            params.append(now)
        if next_status == EMAIL_STATUS_RECEIVED:
            set_parts.append("code_received_at = COALESCE(code_received_at, ?)")
            params.append(now)
        if "is_sold" in updates:
            set_parts.append("sale_status = ?")
            set_parts.append("sold_at = ?")
            params.extend([sale_status or SALE_STATUS_UNSOLD, now if sale_status == SALE_STATUS_SOLD else None])
        if updates.get("last_cdk") is not None:
            set_parts.append("last_cdk = ?")
            params.append(str(updates.get("last_cdk") or ""))
        if updates.get("last_task_id") is not None:
            set_parts.append("last_task_id = ?")
            params.append(str(updates.get("last_task_id") or ""))
        params.append(key)
        conn.execute(f"UPDATE emails SET {', '.join(set_parts)} WHERE email = ?", params)
        row = conn.execute("SELECT * FROM emails WHERE email = ?", (key,)).fetchone()
    return row_to_email_record(row)


def list_emails(
    status_filter: str,
    sale_filter: str,
    query: str = "",
    page: Any = 1,
    page_size: Any = 20,
) -> dict[str, Any]:
    status_filter = (status_filter or EMAIL_STATUS_REGISTERED).strip().lower()
    sale_filter = (sale_filter or SALE_STATUS_UNSOLD).strip().lower()
    page_number, size = normalize_page(page, page_size)
    if status_filter not in EMAIL_STATUSES | {"marketable", "all"}:
        raise ValueError("邮箱状态只能是 unregistered、registered、received、marketable 或 all")
    if sale_filter not in {"all", SALE_STATUS_UNSOLD, SALE_STATUS_SOLD}:
        raise ValueError("销售状态只能是 all、unsold 或 sold")

    where: list[str] = []
    params: list[Any] = []
    if status_filter == "marketable":
        where.append("register_status IN ('registered', 'received')")
    elif status_filter != "all":
        where.append("register_status = ?")
        params.append(status_filter)
    if sale_filter != "all":
        where.append("sale_status = ?")
        params.append(sale_filter)
    needle = (query or "").strip().lower()
    if needle:
        where.append("(email LIKE ? OR client_id LIKE ? OR last_cdk LIKE ?)")
        like = f"%{needle}%"
        params.extend([like, like, like])
    where_sql = " AND ".join(where) if where else "1 = 1"
    offset = (page_number - 1) * size
    with connect() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS total FROM emails WHERE {where_sql}", params).fetchone()
        rows = conn.execute(
            f"""
            SELECT * FROM emails
             WHERE {where_sql}
             ORDER BY updated_at DESC, email ASC
             LIMIT ? OFFSET ?
            """,
            [*params, size, offset],
        ).fetchall()
    items = [row_to_email_record(row) for row in rows]
    total = int(total_row["total"] or 0)
    total_pages = max(1, (total + size - 1) // size)
    return {
        "status": status_filter,
        "sale": sale_filter,
        "count": len(items),
        "total": total,
        "page": page_number,
        "page_size": size,
        "total_pages": total_pages,
        "items": items,
    }


def update_email_sale(values: list[str], is_sold: bool) -> dict[str, int]:
    targets = {email_key(value) for value in values if email_key(value)}
    if not targets:
        raise ValueError("请选择要更新的邮箱")
    now = now_ts()
    updated = 0
    skipped = 0
    with connect() as conn:
        for key in targets:
            row = conn.execute("SELECT email, register_status FROM emails WHERE email = ?", (key,)).fetchone()
            if not row:
                skipped += 1
                continue
            if str(row["register_status"]) == EMAIL_STATUS_UNREGISTERED:
                skipped += 1
                continue
            conn.execute(
                """
                UPDATE emails
                   SET sale_status = ?, sold_at = ?, updated_at = ?
                 WHERE email = ?
                """,
                (SALE_STATUS_SOLD if is_sold else SALE_STATUS_UNSOLD, now if is_sold else None, now, key),
            )
            updated += 1
    return {"updated": updated, "skipped": skipped}


def delete_emails(values: list[str]) -> dict[str, int]:
    targets = {email_key(value) for value in values if email_key(value)}
    if not targets:
        raise ValueError("请选择要删除的邮箱")
    removed = 0
    skipped = 0
    with connect() as conn:
        for key in targets:
            cur = conn.execute("DELETE FROM emails WHERE email = ?", (key,))
            if cur.rowcount:
                removed += cur.rowcount
            else:
                skipped += 1
    return {"removed": removed, "skipped": skipped}


def cdk_values(status: str) -> list[str]:
    status = normalize_cdk_status(status)
    with connect() as conn:
        rows = conn.execute(
            "SELECT cdk FROM cdks WHERE status = ? ORDER BY created_at ASC, cdk ASC",
            (status,),
        ).fetchall()
    return [str(row["cdk"]) for row in rows]


def list_cdks(status: str, query: str = "") -> list[str]:
    status = normalize_cdk_status(status)
    needle = (query or "").strip().lower()
    values = cdk_values(status)
    if needle:
        values = [value for value in values if needle in value.lower()]
    return values


def add_cdks(values: list[str], status: str = CDK_STATUS_UNUSED) -> dict[str, int]:
    status = normalize_cdk_status(status)
    added = 0
    skipped = 0
    with connect() as conn:
        for value in values:
            cdk = str(value or "").strip()
            if not cdk:
                continue
            row = conn.execute("SELECT cdk FROM cdks WHERE cdk = ?", (cdk,)).fetchone()
            if row:
                skipped += 1
                continue
            upsert_cdk(conn, cdk, status)
            added += 1
    return {"added": added, "skipped": skipped}


def add_emails(values: list[str], register_status: str = EMAIL_STATUS_UNREGISTERED) -> dict[str, int]:
    register_status = register_status if register_status in EMAIL_STATUSES else EMAIL_STATUS_UNREGISTERED
    added = 0
    skipped = 0
    with connect() as conn:
        for value in values:
            account = parse_email_record(value)
            if not account:
                raise ValueError("邮箱格式应为 email----password----client_id----refresh_token")
            key = email_key(account["email"])
            row = conn.execute("SELECT email FROM emails WHERE email = ?", (key,)).fetchone()
            if row:
                skipped += 1
                continue
            upsert_email(conn, account, register_status=register_status)
            added += 1
    return {"added": added, "skipped": skipped}


def delete_cdks(values: list[str], status: str) -> dict[str, int]:
    status = normalize_cdk_status(status)
    targets = {str(value or "").strip() for value in values if str(value or "").strip()}
    if not targets:
        raise ValueError("请选择要删除的数据")
    removed = 0
    with connect() as conn:
        for value in targets:
            cur = conn.execute("DELETE FROM cdks WHERE cdk = ? AND status = ?", (value, status))
            removed += cur.rowcount
    return {"removed": removed}


def move_cdk(value: str, to_status: str) -> None:
    cdk = str(value or "").strip()
    if not cdk:
        return
    to_status = normalize_cdk_status(to_status)
    with connect() as conn:
        upsert_cdk(conn, cdk, to_status)
        conn.execute(
            "UPDATE cdks SET status = ?, updated_at = ? WHERE cdk = ?",
            (to_status, now_ts(), cdk),
        )


def claim_email(owner: str = "gpt-login") -> dict[str, Any] | None:
    now = now_ts()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM emails
             WHERE register_status = ?
               AND sale_status = ?
               AND (reserved_at IS NULL OR reserved_at = 0)
             ORDER BY created_at ASC, email ASC
             LIMIT 1
            """,
            (EMAIL_STATUS_UNREGISTERED, SALE_STATUS_UNSOLD),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE emails
               SET reserved_by = ?, reserved_at = ?, updated_at = ?
             WHERE email = ?
            """,
            (owner, now, now, row["email"]),
        )
        row = conn.execute("SELECT * FROM emails WHERE email = ?", (row["email"],)).fetchone()
    return row_to_email_record(row)


def mark_gpt_login_email(email: str, status: str) -> None:
    key = email_key(email)
    if not key:
        raise ValueError("缺少邮箱")
    status = str(status or "").strip().lower()
    now = now_ts()
    with connect() as conn:
        row = conn.execute("SELECT * FROM emails WHERE email = ?", (key,)).fetchone()
        if not row:
            raise ValueError("邮箱不存在")
        if status == "completed":
            conn.execute(
                """
                UPDATE emails
                   SET register_status = ?, registered_at = COALESCE(registered_at, ?),
                       reserved_by = '', reserved_at = NULL, updated_at = ?
                 WHERE email = ?
                """,
                (EMAIL_STATUS_REGISTERED, now, now, key),
            )
        elif status == "in_progress":
            conn.execute(
                """
                UPDATE emails
                   SET reserved_by = ?, reserved_at = COALESCE(reserved_at, ?), updated_at = ?
                 WHERE email = ?
                """,
                ("gpt-login", now, now, key),
            )
        elif status in {"failed", "not_started"}:
            conn.execute(
                """
                UPDATE emails
                   SET register_status = ?,
                       reserved_by = '', reserved_at = NULL, updated_at = ?
                 WHERE email = ?
                   AND register_status = ?
                """,
                (EMAIL_STATUS_UNREGISTERED, now, key, EMAIL_STATUS_UNREGISTERED),
            )
        else:
            raise ValueError("邮箱状态只能是 not_started、in_progress、completed 或 failed")


def release_email_reservations() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE emails
               SET reserved_by = '', reserved_at = NULL, updated_at = ?
             WHERE register_status = ? AND reserved_at IS NOT NULL
            """,
            (now_ts(), EMAIL_STATUS_UNREGISTERED),
        )


def stats(active_email_keys: set[str] | None = None, active_cdks: set[str] | None = None) -> dict[str, int]:
    active_email_keys = active_email_keys or set()
    active_cdks = active_cdks or set()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN register_status = 'unregistered' THEN 1 ELSE 0 END) AS emails_unregistered,
              SUM(CASE WHEN register_status = 'registered' THEN 1 ELSE 0 END) AS emails_registered,
              SUM(CASE WHEN register_status = 'received' THEN 1 ELSE 0 END) AS emails_received,
              SUM(CASE WHEN register_status IN ('registered', 'received')
                        AND sale_status = 'unsold' THEN 1 ELSE 0 END) AS emails_unsold,
              SUM(CASE WHEN register_status IN ('registered', 'received')
                        AND sale_status = 'sold' THEN 1 ELSE 0 END) AS emails_sold,
              SUM(CASE WHEN register_status = 'unregistered' AND sale_status = 'unsold'
                        AND reserved_at IS NULL THEN 1 ELSE 0 END) AS emails_available,
              SUM(CASE WHEN register_status = 'unregistered' AND reserved_at IS NOT NULL THEN 1 ELSE 0 END) AS emails_reserved,
              SUM(CASE WHEN register_status = 'registered' AND sale_status = 'unsold'
                        AND reserved_at IS NULL THEN 1 ELSE 0 END) AS emails_registered_available,
              SUM(CASE WHEN register_status = 'registered' AND reserved_at IS NOT NULL THEN 1 ELSE 0 END) AS emails_registered_reserved,
              COUNT(*) AS emails_total
            FROM emails
            """
        ).fetchone()
        cdk_row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS cdks_unused,
              SUM(CASE WHEN status = 'used' THEN 1 ELSE 0 END) AS cdks_used,
              COUNT(*) AS cdks_total
            FROM cdks
            """
        ).fetchone()
    cdks_unused = int(cdk_row["cdks_unused"] or 0)
    return {
        "emails_unregistered": int(row["emails_unregistered"] or 0),
        "emails_registered": int(row["emails_registered"] or 0),
        "emails_received_code": int(row["emails_received"] or 0),
        "emails_unsold": int(row["emails_unsold"] or 0),
        "emails_sold": int(row["emails_sold"] or 0),
        "emails_source_total": int(row["emails_total"] or 0),
        "emails_unused": int(row["emails_unregistered"] or 0),
        "emails_used": int(row["emails_registered"] or 0) + int(row["emails_received"] or 0),
        "emails_reserved": int(row["emails_reserved"] or 0),
        "emails_available": int(row["emails_available"] or 0),
        "emails_registered_available": max(0, int(row["emails_registered_available"] or 0) - len(active_email_keys)),
        "emails_registered_reserved": int(row["emails_registered_reserved"] or 0) + len(active_email_keys),
        "cdks_unused": cdks_unused,
        "cdks_used": int(cdk_row["cdks_used"] or 0),
        "cdks_reserved": len(set(cdk_values(CDK_STATUS_UNUSED)) & active_cdks),
        "cdks_available": max(0, cdks_unused - len(set(cdk_values(CDK_STATUS_UNUSED)) & active_cdks)),
    }


def mail_pool_summary() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM emails ORDER BY created_at ASC, email ASC").fetchall()
    summary = {
        "total": len(rows),
        "not_started": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
    }
    items: list[dict[str, Any]] = []
    for row in rows:
        record = row_to_email_record(row)
        if record["register_status"] == EMAIL_STATUS_UNREGISTERED:
            status = "in_progress" if record["reserved_at"] else "not_started"
        else:
            status = "completed"
        summary[status] += 1
        items.append({"email": record["email"], "status": status})
    return {
        "summary": summary,
        "rows": items,
        "file": {"bound": True, "supported": True, "name": DB_FILE.name, "backend": "sqlite"},
        "fileSync": {"ok": True, "reason": "sqlite", "name": DB_FILE.name},
    }
