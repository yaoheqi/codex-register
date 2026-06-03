# -*- coding: utf-8 -*-
"""
Codex 本地资源管理台。

当前服务只保留本地邮箱资源、GPT 登录池与 Codex 凭据管理。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import resource_store as store


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

INDEX_FILE = ROOT / "index.html"

DATA_LOCK = threading.RLock()


class AppError(Exception):
    """可返回给前端的业务错误。"""

    def __init__(self, message: str, status: int = 400, payload: Any | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def now_ts() -> int:
    return int(time.time())


def iso_time(ts: int | float | None = None) -> str:
    value = time.time() if ts is None else ts
    return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init_db()


def email_key(email: str) -> str:
    return str(email or "").strip().lower()


def parse_email_record(line: str) -> dict[str, str]:
    parts = [part.strip() for part in line.strip().split("----")]
    if len(parts) < 4:
        raise AppError("邮箱格式应为 email----password----client_id----refresh_token")
    email = parts[0]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise AppError(f"邮箱格式不正确：{email}")
    refresh_token = "----".join(parts[3:]).strip()
    if not parts[2] or not refresh_token:
        raise AppError("邮箱格式应为 email----password----client_id----refresh_token")
    return {
        "raw": line.strip(),
        "email": email,
        "password": parts[1],
        "client_id": parts[2],
        "refresh_token": refresh_token,
    }


def normalize_email_values(text: str | list[str]) -> list[str]:
    raw_values = text if isinstance(text, list) else str(text or "").splitlines()
    values: list[str] = []
    for raw in raw_values:
        value = str(raw).strip()
        if not value:
            continue
        parse_email_record(value)
        values.append(value)
    return values


def mask_secret(value: str, keep: int = 8) -> str:
    value = str(value or "")
    if len(value) <= keep * 2 + 3:
        return value
    return value[:keep] + "..." + value[-keep:]


def record_email_status(email_record: str, **updates: Any) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.record_email_status(email_record, **updates)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def list_email_statuses(
    status_filter: str = "registered",
    query: str = "",
    page: Any = 1,
    page_size: Any = 20,
) -> dict[str, Any]:
    status_filter = (status_filter or "registered").strip().lower()
    try:
        with DATA_LOCK:
            data = store.list_emails(status_filter, query, page, page_size)
    except ValueError as exc:
        raise AppError(str(exc)) from exc

    for item in data["items"]:
        item["label"] = item.get("email") or ""
        item["preview"] = (
            f'{item.get("email") or ""} ---- '
            f'{item.get("client_id") or ""} ---- '
            f'{mask_secret(str(item.get("refresh_token") or ""))}'
        )
    return data


def update_email_register_status(values: list[str], register_status: str) -> dict[str, int]:
    try:
        with DATA_LOCK:
            return store.update_email_register_status(values, register_status)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def delete_email_statuses(values: list[str]) -> dict[str, int]:
    try:
        with DATA_LOCK:
            return store.delete_emails(values)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def account_export_line(item: dict[str, Any]) -> str:
    raw_account: dict[str, str] | None = None
    raw = str(item.get("raw") or "").strip()
    if raw:
        try:
            raw_account = parse_email_record(raw)
        except AppError:
            raw_account = None

    def field(name: str) -> str:
        return str(item.get(name) or (raw_account or {}).get(name) or "").strip()

    email = field("email").lower()
    password = field("password")
    client_id = field("client_id")
    refresh_token = field("refresh_token")

    if not email or not client_id or not refresh_token:
        return ""

    line = "----".join([email, password, client_id, refresh_token])
    try:
        parse_email_record(line)
    except AppError:
        return ""
    return line


def account_lines_text(rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    lines: list[str] = []
    emails: list[str] = []
    for row in rows:
        line = account_export_line(row)
        if not line:
            continue
        lines.append(line)
        email = str(row.get("email") or "").strip()
        if email:
            emails.append(email)
    return "\n".join(lines) + ("\n" if lines else ""), emails


def export_email_statuses(body: dict[str, Any]) -> dict[str, Any]:
    selected_values = body.get("values") or []
    if not isinstance(selected_values, list):
        raise AppError("请选择要导出的邮箱")

    selected_keys: list[str] = []
    seen: set[str] = set()
    for value in selected_values:
        key = email_key(str(value or ""))
        if key and key not in seen:
            selected_keys.append(key)
            seen.add(key)

    if not selected_keys:
        raise AppError("请选择要导出的邮箱")

    with DATA_LOCK:
        records = store.load_email_records()

    export_items: list[dict[str, Any]] = []
    skipped = 0
    for key in selected_keys:
        item = records.get(key)
        if not item:
            skipped += 1
            continue
        if item.get("register_status") not in {store.EMAIL_STATUS_REGISTERED, store.EMAIL_STATUS_RECEIVED}:
            skipped += 1
            continue
        export_items.append(item)

    if not export_items:
        raise AppError("选中的邮箱里没有可导出的已注册或已接码邮箱")

    text, exported_values = account_lines_text(export_items)
    exported_count = len(exported_values)
    if not exported_count:
        raise AppError("选中的邮箱缺少 client_id 或 refresh_token，无法导出")

    return {
        "count": exported_count,
        "text": text,
        "filename": f"email-accounts-{now_ts()}.csv",
        "mime": "text/plain;charset=utf-8",
        "skipped": skipped + (len(export_items) - exported_count),
    }


def list_items(kind: str, bucket: str, query: str = "", page: Any = 1, page_size: Any = 20) -> dict[str, Any]:
    if kind != "email":
        raise AppError("当前只支持邮箱资源")
    status = "unregistered" if bucket in {"unused", "unregistered"} else bucket
    return list_email_statuses(status, query, page, page_size)


def add_items(kind: str, bucket: str, text: str | list[str]) -> dict[str, Any]:
    if kind != "email":
        raise AppError("当前只支持写入邮箱")
    values = normalize_email_values(text)
    if not values:
        raise AppError("没有可添加的数据")
    try:
        with DATA_LOCK:
            status = "unregistered" if bucket in {"unused", "unregistered"} else bucket
            return store.add_emails(values, status)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def stats() -> dict[str, int]:
    with DATA_LOCK:
        return store.stats()


def gpt_login_account_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "raw": record.get("raw") or "",
        "email": record.get("email") or "",
        "password": record.get("password") or "",
        "client_id": record.get("client_id") or "",
        "refresh_token": record.get("refresh_token") or "",
        "status": record.get("register_status") or store.EMAIL_STATUS_UNREGISTERED,
        "register_status": record.get("register_status") or store.EMAIL_STATUS_UNREGISTERED,
        "is_reserved": bool(record.get("reserved_at")),
    }


def gpt_login_mail_pool() -> dict[str, Any]:
    with DATA_LOCK:
        return store.mail_pool_summary()


def gpt_login_claim_email() -> dict[str, Any] | None:
    with DATA_LOCK:
        record = store.claim_email("gpt-login")
    return gpt_login_account_payload(record)


def gpt_login_mark_email(email_addr: str, status: str) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            store.mark_gpt_login_email(email_addr, status)
            return store.mail_pool_summary()
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def gpt_login_reset_mail_pool() -> dict[str, Any]:
    with DATA_LOCK:
        store.release_email_reservations()
        return store.mail_pool_summary()


def gpt_login_sync_mail_pool() -> dict[str, Any]:
    with DATA_LOCK:
        source_sync = store.sync_source_files()
        pool = store.mail_pool_summary()
    pool["sourceSync"] = source_sync
    return pool


def save_codex_credential(body: dict[str, Any]) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.save_codex_credential(body)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def list_codex_credentials(query: str = "") -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.list_codex_credentials(query)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def get_codex_credential(record_id: str) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.get_codex_credential(record_id)
    except ValueError as exc:
        raise AppError(str(exc), 404) from exc


def delete_codex_credential(record_id: str) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.delete_codex_credential(record_id)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def codex_credential_id_from_path(path: str) -> str:
    prefix = "/api/codex-credentials/"
    if not path.startswith(prefix):
        return ""
    return urllib.parse.unquote(path[len(prefix) :].strip())


class AppHandler(BaseHTTPRequestHandler):
    server_version = "CodexResourceManager/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (iso_time(), fmt % args))

    def do_GET(self) -> None:
        try:
            path, query = self.parse_url()
            if path in {"/", "/index.html"}:
                self.send_file(INDEX_FILE, "text/html; charset=utf-8")
                return
            if path == "/api/stats":
                self.send_json({"code": 0, "data": stats()})
                return
            if path == "/api/codex-credentials":
                self.send_json({"code": 0, "data": list_codex_credentials(query.get("q", [""])[0])})
                return
            credential_id = codex_credential_id_from_path(path)
            if credential_id:
                self.send_json({"code": 0, "data": get_codex_credential(credential_id)})
                return
            if path == "/api/emails":
                data = list_email_statuses(
                    query.get("status", ["registered"])[0],
                    query.get("q", [""])[0],
                    query.get("page", ["1"])[0],
                    query.get("page_size", ["20"])[0],
                )
                self.send_json({"code": 0, "data": data})
                return
            if path == "/api/gpt-login/mail-pool":
                self.send_json({"code": 0, "data": gpt_login_mail_pool()})
                return
            if path == "/api/list":
                data = list_items(
                    query.get("kind", ["email"])[0],
                    query.get("bucket", ["unregistered"])[0],
                    query.get("q", [""])[0],
                    query.get("page", ["1"])[0],
                    query.get("page_size", ["20"])[0],
                )
                self.send_json({"code": 0, "data": data})
                return
            raise AppError("接口不存在", 404)
        except Exception as exc:
            self.send_error_json(exc)

    def do_POST(self) -> None:
        try:
            path, _ = self.parse_url()
            body = self.read_json_body()
            if path == "/api/list":
                data = add_items(str(body.get("kind") or "email"), str(body.get("bucket") or "unregistered"), body.get("text") or body.get("values") or "")
                self.send_json({"code": 0, "message": "已添加", "data": data})
                return
            if path == "/api/emails/export":
                data = export_email_statuses(body)
                self.send_json({"code": 0, "message": "邮箱已导出", "data": data})
                return
            if path == "/api/codex-credentials":
                data = save_codex_credential(body)
                self.send_json({"code": 0, "message": "Codex 凭据已保存", "data": data})
                return
            if path == "/api/gpt-login/mail-pool/claim":
                account = gpt_login_claim_email()
                self.send_json({"code": 0, "message": "邮箱已分配", "data": account})
                return
            if path == "/api/gpt-login/mail-pool/mark":
                data = gpt_login_mark_email(str(body.get("email") or ""), str(body.get("status") or ""))
                self.send_json({"code": 0, "message": "邮箱状态已更新", "data": data})
                return
            if path == "/api/gpt-login/mail-pool/reset":
                data = gpt_login_reset_mail_pool()
                self.send_json({"code": 0, "message": "邮箱占用状态已重置", "data": data})
                return
            if path == "/api/gpt-login/mail-pool/sync":
                data = gpt_login_sync_mail_pool()
                self.send_json({"code": 0, "message": "SQLite 数据源已同步", "data": data})
                return
            raise AppError("接口不存在", 404)
        except Exception as exc:
            self.send_error_json(exc)

    def do_PUT(self) -> None:
        try:
            path, _ = self.parse_url()
            if path == "/api/email-status":
                body = self.read_json_body()
                data = update_email_register_status(body.get("values") or [], str(body.get("status") or ""))
                self.send_json({"code": 0, "message": "邮箱状态已更新", "data": data})
                return
            raise AppError("接口不存在", 404)
        except Exception as exc:
            self.send_error_json(exc)

    def do_DELETE(self) -> None:
        try:
            path, _ = self.parse_url()
            body = self.read_json_body()
            if path == "/api/emails":
                data = delete_email_statuses(body.get("values") or [])
                self.send_json({"code": 0, "message": "邮箱已删除", "data": data})
                return
            credential_id = codex_credential_id_from_path(path)
            if credential_id:
                data = delete_codex_credential(credential_id)
                self.send_json({"code": 0, "message": "Codex 凭据已删除", "data": data})
                return
            raise AppError("接口不存在", 404)
        except Exception as exc:
            self.send_error_json(exc)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def parse_url(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise AppError("请求体不是有效 JSON")
        if not isinstance(payload, dict):
            raise AppError("请求体必须是 JSON 对象")
        return payload

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            raise AppError("页面文件不存在", 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, exc: Exception) -> None:
        if isinstance(exc, AppError):
            status = exc.status if 100 <= exc.status <= 599 else 400
            self.send_json({"code": status, "message": str(exc), "data": exc.payload}, status=status)
            return
        traceback.print_exc()
        self.send_json({"code": 500, "message": str(exc) or exc.__class__.__name__, "data": None}, status=500)


def run_server(host: str, port: int) -> None:
    ensure_storage()
    server = ThreadingHTTPServer((host, port), AppHandler)
    url_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    print(f"Codex 本地资源管理台已启动：http://{url_host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("服务已停止")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 本地资源管理台")
    parser.add_argument("--host", default="127.0.0.1", help="本地服务监听地址")
    parser.add_argument("--port", type=int, default=8060, help="本地服务端口")
    args = parser.parse_args()

    ensure_storage()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
