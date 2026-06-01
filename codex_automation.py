# -*- coding: utf-8 -*-
"""
Codex 自动接码脚本。

核心流程：
1. 提交邮箱和 CDK。
2. 从邮箱接口读取六位验证码。
3. 提交邮箱验证码，并轮询任务状态。

运行：
    python codex_automation.py
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import json
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import resource_store as store


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

CDK_UNUSED_FILE = DATA_DIR / "cdks_unused" / "cdks.txt"
CDK_USED_FILE = DATA_DIR / "cdks_used" / "cdks.txt"
TASKS_FILE = DATA_DIR / "tasks.json"
CONFIG_FILE = DATA_DIR / "config.json"
INDEX_FILE = ROOT / "index.html"

MAIL_API_URL = "https://mail.az0.cn/core.php"
MAILBOXES = [
    {"id": "INBOX", "label": "收件箱"},
    {"id": "Junk", "label": "垃圾箱"},
]

SENDER_HINTS = [
    "openai",
    "chatgpt",
    "noreply",
    "microsoftonline",
    "account-security",
    "no-reply",
]
SUBJECT_HINTS = [
    "verification",
    "verify",
    "code",
    "验证码",
    "确认",
    "sign-in",
    "sign in",
    "登录",
    "one-time",
]
DIRECT_CODE_KEYS = [
    "code",
    "verification_code",
    "verificationCode",
    "verify_code",
    "verifyCode",
    "otp",
]
TEXT_KEYS = [
    "subject",
    "from",
    "sender",
    "to",
    "body",
    "text",
    "html",
    "content",
    "snippet",
    "preview",
    "message",
]
TIME_KEYS = [
    "date",
    "time",
    "timestamp",
    "created_at",
    "createdAt",
    "received_at",
    "receivedAt",
    "sent_at",
    "sentAt",
    "internalDate",
]

DEFAULT_CONFIG = {
    "codex_base_url": "https://www.hansaes.icu/",
    "api_mode": "legacy",
    "request_timeout": 30,
    "mail_poll_timeout": 170,
    "mail_poll_interval": 3,
    "status_poll_timeout": 600,
    "status_poll_interval": 5,
    "code_valid_window_ms": 10 * 60 * 1000,
}

DATA_LOCK = threading.RLock()
TASK_LOCK = threading.RLock()
TASK_CREATE_LOCK = threading.RLock()

ASYNC_WORKER_MIN = 3
ASYNC_WORKER_MAX = 10
TASK_EXECUTOR = ThreadPoolExecutor(max_workers=ASYNC_WORKER_MAX, thread_name_prefix="codex-worker")
TASK_FUTURES: set[Future[Any]] = set()
TASK_FUTURES_LOCK = threading.RLock()
TASK_EXECUTOR_WARMED = False


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


def iso_time_ms(value_ms: int | float | None) -> str:
    if not value_ms:
        return "无时间"
    return _dt.datetime.fromtimestamp(float(value_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")


def describe_mail_time(candidate: dict[str, Any], not_before_ms: int | None) -> str:
    mail_time = candidate.get("time")
    if not mail_time:
        return "邮件时间=无，邮箱接口未返回时间，无法严格确认是否最新"
    if not_before_ms:
        delta = (int(mail_time) - int(not_before_ms)) / 1000
        if delta >= 0:
            return f"邮件时间={iso_time_ms(mail_time)}，比提交邮箱晚 {delta:.1f}s"
        return f"邮件时间={iso_time_ms(mail_time)}，比提交邮箱早 {abs(delta):.1f}s"
    return f"邮件时间={iso_time_ms(mail_time)}"


def is_fresh_candidate(candidate: dict[str, Any], not_before_ms: int | None, tolerance_ms: int = 60_000) -> bool:
    mail_time = candidate.get("time")
    if not mail_time:
        return False
    if not not_before_ms:
        return True
    return int(mail_time) >= int(not_before_ms) - tolerance_ms


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init_db()
    if not TASKS_FILE.exists():
        write_json(TASKS_FILE, [])
    if not CONFIG_FILE.exists():
        write_json(CONFIG_FILE, DEFAULT_CONFIG)


def read_json(path: Path, default: Any) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return default
        return json.loads(text)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value:
            lines.append(value)
    return lines


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if content:
        content += "\n"
    path.write_text(content, encoding="utf-8")


def email_key(email: str) -> str:
    return str(email or "").strip().lower()


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "已"}


def build_email_status_record(account: dict[str, str], current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or {}
    return {
        "email": account["email"],
        "raw": account.get("raw") or "",
        "password": account.get("password") or "",
        "client_id": account.get("client_id") or "",
        "refresh_token": account.get("refresh_token") or "",
        "is_registered": bool_value(current.get("is_registered")),
        "has_received_code": bool_value(current.get("has_received_code")),
        "is_sold": bool_value(current.get("is_sold")),
        "registered_at": current.get("registered_at"),
        "code_received_at": current.get("code_received_at"),
        "sold_at": current.get("sold_at"),
        "last_cdk": current.get("last_cdk") or "",
        "last_task_id": current.get("last_task_id") or "",
        "created_at": current.get("created_at") or now_ts(),
        "updated_at": current.get("updated_at") or now_ts(),
    }


def parse_optional_email_record(line: str) -> dict[str, str] | None:
    value = str(line or "").strip()
    if not value or value.startswith("#"):
        return None
    try:
        return parse_email_record(value)
    except AppError:
        return None


def email_unavailable_for_automation(status: dict[str, Any] | None) -> bool:
    if not status:
        return False
    register_status = str(status.get("register_status") or "").lower()
    return register_status != store.EMAIL_STATUS_REGISTERED or bool_value(status.get("is_sold"))


def active_email_keys(active_emails: set[str] | None = None) -> set[str]:
    values = active_emails if active_emails is not None else active_values()[0]
    keys: set[str] = set()
    for value in values:
        account = parse_optional_email_record(value)
        if account:
            keys.add(email_key(account["email"]))
    return keys


def available_registered_accounts(active_emails: set[str] | None = None) -> list[dict[str, str]]:
    active_raw = active_emails if active_emails is not None else active_values()[0]
    active_keys = active_email_keys(active_raw)
    accounts = store.available_email_accounts(active_keys)
    return [account for account in accounts if account.get("raw") not in active_raw]


def record_email_status(email_record: str, **updates: Any) -> dict[str, Any]:
    try:
        with DATA_LOCK:
            return store.record_email_status(email_record, **updates)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def list_email_statuses(
    status_filter: str = "registered",
    sale_filter: str = "unsold",
    query: str = "",
    page: Any = 1,
    page_size: Any = 20,
) -> dict[str, Any]:
    status_filter = (status_filter or "registered").strip().lower()
    sale_filter = (sale_filter or "unsold").strip().lower()
    try:
        with DATA_LOCK:
            data = store.list_emails(status_filter, sale_filter, query, page, page_size)
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


def update_email_sale_status(values: list[str], is_sold: bool) -> dict[str, int]:
    try:
        with DATA_LOCK:
            return store.update_email_sale(values, is_sold)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def delete_email_statuses(values: list[str]) -> dict[str, int]:
    try:
        with DATA_LOCK:
            return store.delete_emails(values)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


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
        if item.get("register_status") == store.EMAIL_STATUS_UNREGISTERED:
            skipped += 1
            continue
        export_items.append(item)

    if not export_items:
        raise AppError("选中的邮箱里没有可导出的已注册或已接码邮箱")

    values = [str(item.get("email") or "") for item in export_items if item.get("email")]
    updated = update_email_sale_status(values, True)
    lines = [str(item.get("raw") or item.get("email") or "") for item in export_items]
    return {
        "count": len(lines),
        "text": "\n".join(lines),
        "filename": f"emails-selected-{now_ts()}.txt",
        "sale": updated,
        "skipped": skipped,
    }


def item_file(kind: str, bucket: str) -> Path:
    if kind not in {"email", "cdk"}:
        raise AppError("类型只能是 email 或 cdk")
    if bucket not in {"unused", "used"}:
        raise AppError("列表只能是 unused 或 used")
    if kind == "email":
        raise AppError("邮箱已由 SQLite 邮箱状态表维护，请使用 /api/emails")
    return CDK_UNUSED_FILE if bucket == "unused" else CDK_USED_FILE


def load_config() -> dict[str, Any]:
    with DATA_LOCK:
        config = DEFAULT_CONFIG | read_json(CONFIG_FILE, {})
    return normalize_config(config)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    result = DEFAULT_CONFIG.copy()
    result.update(config or {})
    result["codex_base_url"] = str(result.get("codex_base_url") or DEFAULT_CONFIG["codex_base_url"]).rstrip("/")
    mode = str(result.get("api_mode") or "legacy").lower()
    result["api_mode"] = mode if mode in {"legacy", "v1", "auto"} else "legacy"
    for key in [
        "request_timeout",
        "mail_poll_timeout",
        "mail_poll_interval",
        "status_poll_timeout",
        "status_poll_interval",
        "code_valid_window_ms",
    ]:
        try:
            result[key] = int(result[key])
        except (TypeError, ValueError):
            result[key] = DEFAULT_CONFIG[key]
    return result


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    with DATA_LOCK:
        current = normalize_config(DEFAULT_CONFIG | read_json(CONFIG_FILE, {}))
        allowed = set(DEFAULT_CONFIG)
        for key, value in (patch or {}).items():
            if key in allowed:
                current[key] = value
        current = normalize_config(current)
        write_json(CONFIG_FILE, current)
        return current


def parse_email_record(line: str) -> dict[str, str]:
    parts = [part.strip() for part in line.strip().split("----")]
    if len(parts) < 4:
        raise AppError("邮箱格式应为 email----password----client_id----refresh_token")
    email = parts[0]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise AppError(f"邮箱格式不正确：{email}")
    return {
        "raw": line.strip(),
        "email": email,
        "password": parts[1],
        "client_id": parts[2],
        "refresh_token": "----".join(parts[3:]).strip(),
    }


def normalize_values(kind: str, text: str | list[str]) -> list[str]:
    if isinstance(text, list):
        raw_values = text
    else:
        raw_values = str(text or "").splitlines()

    values: list[str] = []
    for raw in raw_values:
        value = str(raw).strip()
        if not value:
            continue
        if kind == "email":
            parse_email_record(value)
        elif kind == "cdk":
            if len(value) < 3:
                raise AppError(f"CDK 过短：{value}")
        else:
            raise AppError("类型只能是 email 或 cdk")
        values.append(value)
    return values


def display_item(kind: str, value: str, index: int) -> dict[str, Any]:
    if kind == "email":
        try:
            account = parse_email_record(value)
            label = account["email"]
            preview = f'{account["email"]} ---- {account["client_id"]} ---- {mask_secret(account["refresh_token"])}'
        except AppError:
            label = value.split("----", 1)[0]
            preview = mask_secret(value)
    else:
        label = value
        preview = mask_secret(value)
    return {"index": index, "value": value, "label": label, "preview": preview}


def mask_secret(value: str, keep: int = 8) -> str:
    value = str(value or "")
    if len(value) <= keep * 2 + 3:
        return value
    return value[:keep] + "..." + value[-keep:]


def list_items(kind: str, bucket: str, query: str = "", page: Any = 1, page_size: Any = 20) -> dict[str, Any]:
    if kind == "email":
        status = "unregistered" if bucket == "unused" else "registered"
        return list_email_statuses(status, "all", query, page, page_size)
    if kind != "cdk":
        raise AppError("类型只能是 email 或 cdk")
    if bucket not in {"unused", "used"}:
        raise AppError("列表只能是 unused 或 used")
    needle = (query or "").strip().lower()
    page_number, size = store.normalize_page(page, page_size)
    with DATA_LOCK:
        lines = store.list_cdks(bucket, needle)
    total = len(lines)
    offset = (page_number - 1) * size
    page_values = lines[offset : offset + size]
    items = []
    for index, value in enumerate(page_values, start=offset):
        items.append(display_item(kind, value, index))
    return {
        "kind": kind,
        "bucket": bucket,
        "count": len(items),
        "total": total,
        "page": page_number,
        "page_size": size,
        "total_pages": max(1, (total + size - 1) // size),
        "items": items,
    }


def add_items(kind: str, bucket: str, text: str | list[str]) -> dict[str, Any]:
    values = normalize_values(kind, text)
    if not values:
        raise AppError("没有可添加的数据")
    try:
        with DATA_LOCK:
            if kind == "email":
                status = "unregistered" if bucket in {"unused", "unregistered"} else bucket
                return store.add_emails(values, status)
            if kind == "cdk":
                return store.add_cdks(values, bucket)
    except ValueError as exc:
        raise AppError(str(exc)) from exc
    raise AppError("类型只能是 email 或 cdk")


def delete_items(kind: str, bucket: str, values: list[str]) -> dict[str, Any]:
    if not values:
        raise AppError("请选择要删除的数据")
    if kind != "cdk":
        raise AppError("邮箱删除请直接在 SQLite 管理端处理")
    try:
        with DATA_LOCK:
            return store.delete_cdks(values, bucket)
    except ValueError as exc:
        raise AppError(str(exc)) from exc


def move_value(kind: str, from_bucket: str, to_bucket: str, value: str) -> None:
    if kind != "cdk":
        raise AppError("当前只支持移动 CDK 状态")
    with DATA_LOCK:
        store.move_cdk(value, to_bucket)


def stats() -> dict[str, int]:
    active_emails, active_cdks = active_values()
    data = store.stats(active_email_keys(active_emails), active_cdks)
    data.update(task_executor_stats())
    return data


def load_tasks() -> list[dict[str, Any]]:
    with TASK_LOCK:
        tasks = read_json(TASKS_FILE, [])
        return tasks if isinstance(tasks, list) else []


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    with TASK_LOCK:
        write_json(TASKS_FILE, tasks)


def update_task(task_id: str, **fields: Any) -> dict[str, Any]:
    with TASK_LOCK:
        tasks = load_tasks()
        found: dict[str, Any] | None = None
        for task in tasks:
            if task.get("id") == task_id:
                task.update(fields)
                task["updated_at"] = now_ts()
                found = task
                break
        if found is None:
            raise AppError("任务不存在", 404)
        save_tasks(tasks)
        return found


def warm_task_executor() -> None:
    """预热后台任务线程池，确保服务启动后至少保留 3 个可复用工作线程。"""

    global TASK_EXECUTOR_WARMED
    with TASK_FUTURES_LOCK:
        if TASK_EXECUTOR_WARMED:
            return
        gate = threading.Event()
        futures = [TASK_EXECUTOR.submit(gate.wait) for _ in range(ASYNC_WORKER_MIN)]
        gate.set()
        for future in futures:
            future.result(timeout=5)
        TASK_EXECUTOR_WARMED = True


def submit_automation_task(task_id: str) -> Future[Any]:
    warm_task_executor()
    future = TASK_EXECUTOR.submit(run_automation_task, task_id)
    with TASK_FUTURES_LOCK:
        TASK_FUTURES.add(future)
    future.add_done_callback(lambda item: finish_automation_future(task_id, item))
    return future


def finish_automation_future(task_id: str, future: Future[Any]) -> None:
    with TASK_FUTURES_LOCK:
        TASK_FUTURES.discard(future)
    try:
        future.result()
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        try:
            update_task(task_id, status="failed", message=message, completed_at=now_ts())
            append_task_log(task_id, "后台线程异常：" + message)
        except Exception:
            traceback.print_exc()


def task_executor_stats() -> dict[str, int]:
    with TASK_FUTURES_LOCK:
        return {
            "worker_min": ASYNC_WORKER_MIN,
            "worker_max": ASYNC_WORKER_MAX,
            "worker_threads": len(getattr(TASK_EXECUTOR, "_threads", [])),
            "worker_pending": sum(1 for future in TASK_FUTURES if not future.done()),
        }


def shutdown_task_executor(wait: bool = False) -> None:
    TASK_EXECUTOR.shutdown(wait=wait, cancel_futures=False)


def append_task_log(task_id: str, message: str) -> None:
    with TASK_LOCK:
        tasks = load_tasks()
        for task in tasks:
            if task.get("id") == task_id:
                logs = task.setdefault("logs", [])
                logs.append({"time": iso_time(), "message": message})
                task["logs"] = logs[-120:]
                task["message"] = message
                task["updated_at"] = now_ts()
                save_tasks(tasks)
                return


def get_task(task_id: str) -> dict[str, Any]:
    for task in load_tasks():
        if task.get("id") == task_id:
            return task
    raise AppError("任务不存在", 404)


def active_values() -> tuple[set[str], set[str]]:
    emails: set[str] = set()
    cdks: set[str] = set()
    for task in load_tasks():
        if task.get("status") in {"completed", "failed", "canceled"}:
            continue
        if task.get("email_record"):
            emails.add(task["email_record"])
        if task.get("cdk"):
            cdks.add(task["cdk"])
    return emails, cdks


def pick_unused_pair(email_value: str | None = None, cdk_value: str | None = None) -> tuple[str, str]:
    with DATA_LOCK:
        cdks = store.cdk_values("unused")
    active_emails, active_cdks = active_values()
    statuses = store.load_email_records()

    if email_value:
        account = parse_email_record(email_value)
        if email_unavailable_for_automation(statuses.get(email_key(account["email"]))):
            raise AppError(f"邮箱不是可接码的已注册状态：{account['email']}")
        with DATA_LOCK:
            store.add_emails([email_value], "registered")
        email_record = email_value
    else:
        account = next(iter(available_registered_accounts(active_emails)), None)
        email_record = account["raw"] if account else ""

    if cdk_value:
        cdk = cdk_value.strip()
    else:
        cdk = next((item for item in cdks if item not in active_cdks), "")

    if not email_record:
        raise AppError("没有可用已注册邮箱，请先导入已注册邮箱或完成 gpt-login 注册")
    if not cdk:
        raise AppError("没有可用 CDK，请先添加未用 CDK")
    return email_record, cdk


def build_automation_task(email_record: str, cdk: str) -> dict[str, Any]:
    account = parse_email_record(email_record)
    first_log = f"任务已创建：邮箱={account['email']}，CDK={cdk}"
    return {
        "id": uuid.uuid4().hex,
        "codex_task_id": None,
        "email": account["email"],
        "email_record": email_record,
        "cdk": cdk,
        "email_code": None,
        "code_source": None,
        "code_mail_time": None,
        "code_freshness": None,
        "email_request_at_ms": None,
        "status": "queued",
        "message": first_log,
        "status_payload": None,
        "created_at": now_ts(),
        "updated_at": now_ts(),
        "completed_at": None,
        "logs": [{"time": iso_time(), "message": first_log}],
    }


def create_automation_task(email_value: str | None = None, cdk_value: str | None = None) -> dict[str, Any]:
    with TASK_CREATE_LOCK:
        email_record, cdk = pick_unused_pair(email_value, cdk_value)
        task = build_automation_task(email_record, cdk)
        with TASK_LOCK:
            tasks = load_tasks()
            tasks.insert(0, task)
            save_tasks(tasks)
    submit_automation_task(task["id"])
    return task


def create_automation_batch() -> dict[str, Any]:
    with TASK_CREATE_LOCK:
        with DATA_LOCK:
            cdks = store.cdk_values("unused")
        active_emails, active_cdks = active_values()
        available_emails = [item["raw"] for item in available_registered_accounts(active_emails)]
        available_cdks = [item for item in cdks if item not in active_cdks]
        count = min(len(available_emails), len(available_cdks))
        if count <= 0:
            if not available_emails and not available_cdks:
                raise AppError("没有可调度资源，请先写入邮箱和 CDK")
            if not available_emails:
                raise AppError("可调度邮箱不足，请先导入已注册邮箱或完成 gpt-login 注册")
            raise AppError("可调度 CDK 不足，请先添加 CDK")

        new_tasks = [
            build_automation_task(available_emails[index], available_cdks[index])
            for index in range(count)
        ]
        with TASK_LOCK:
            tasks = load_tasks()
            tasks = new_tasks + tasks
            save_tasks(tasks)

    for task in new_tasks:
        submit_automation_task(task["id"])

    if len(available_emails) == len(available_cdks):
        stop_reason = "邮箱和 CDK 已全部配对"
    elif len(available_emails) < len(available_cdks):
        stop_reason = "已注册邮箱不足"
    else:
        stop_reason = "CDK 不足"

    return {
        "created": len(new_tasks),
        "worker_max": ASYNC_WORKER_MAX,
        "stop_reason": stop_reason,
        "emails_scheduled": len(new_tasks),
        "cdks_scheduled": len(new_tasks),
        "emails_left": max(0, len(available_emails) - len(new_tasks)),
        "cdks_left": max(0, len(available_cdks) - len(new_tasks)),
        "tasks": new_tasks,
    }


def http_request(method: str, url: str, payload: Any | None = None, timeout: int = 30) -> tuple[int, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise AppError(format_api_error(text) or f"HTTP {exc.code}", exc.code, text)
    except urllib.error.URLError as exc:
        raise AppError(f"请求失败：{exc.reason}", 502)
    except TimeoutError:
        raise AppError("请求超时", 504)


def http_json(method: str, url: str, payload: Any | None = None, timeout: int = 30) -> Any:
    status, text = http_request(method, url, payload, timeout)
    if status < 200 or status >= 300:
        raise AppError(format_api_error(text) or f"HTTP {status}", status, text)
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise AppError(f"接口返回不是 JSON：{text[:200]}", 502, text)


def format_api_error(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text[:800]
    if isinstance(payload, dict):
        for key in ["message", "error", "detail"]:
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = format_api_error(value)
                if nested:
                    return nested
        try:
            return json.dumps(payload, ensure_ascii=False)[:800]
        except TypeError:
            return str(payload)[:800]
    return str(payload)[:800]


def unwrap_codex_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Codex 接口返回格式不正确", 502, payload)
    if "code" in payload and ("data" in payload or "message" in payload):
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = 500
        if code != 0:
            raise AppError(str(payload.get("message") or "Codex 接口返回失败"), code, payload)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload


class CodexClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config["codex_base_url"]).rstrip("/")
        self.api_mode = str(config.get("api_mode") or "legacy")
        self.timeout = int(config.get("request_timeout") or 30)

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _call(self, mode: str, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        return unwrap_codex_response(http_json(method, self._url(path), payload, self.timeout))

    def _auto_call(self, v1_args: tuple[str, str, str, Any | None], legacy_args: tuple[str, str, str, Any | None]) -> dict[str, Any]:
        if self.api_mode == "v1":
            return self._call(*v1_args)
        if self.api_mode == "legacy":
            return self._call(*legacy_args)
        try:
            return self._call(*v1_args)
        except AppError as exc:
            if exc.status not in {404, 405}:
                raise
            return self._call(*legacy_args)

    def start(self, cdk: str, email_addr: str) -> dict[str, Any]:
        payload = {"cdk": cdk, "email": email_addr}
        return self._auto_call(
            ("v1", "POST", "/api/v1/codex/start", payload),
            ("legacy", "POST", "/api/submit-email", payload),
        )

    def submit_email_code(self, task_id: str, email_code: str) -> dict[str, Any]:
        payload = {"task_id": task_id, "email_code": email_code}
        return self._auto_call(
            ("v1", "POST", "/api/v1/codex/submit-email-code", payload),
            ("legacy", "POST", "/api/submit-email-otp", payload),
        )

    def status(self, task_id: str | None = None, cdk: str | None = None) -> dict[str, Any]:
        if not task_id and not cdk:
            raise AppError("task_id 和 cdk 至少填写一个")
        if self.api_mode == "v1":
            query = urllib.parse.urlencode({"task_id": task_id} if task_id else {"cdk": cdk})
            return self._call("v1", "GET", f"/api/v1/codex/status?{query}", None)
        if self.api_mode == "legacy":
            if task_id:
                query = urllib.parse.urlencode({"task_id": task_id})
                return self._call("legacy", "GET", f"/api/status?{query}", None)
            query = urllib.parse.urlencode({"cdk": cdk})
            return self._call("legacy", "GET", f"/api/user-status?{query}", None)

        query = urllib.parse.urlencode({"task_id": task_id} if task_id else {"cdk": cdk})
        try:
            return self._call("v1", "GET", f"/api/v1/codex/status?{query}", None)
        except AppError as exc:
            if exc.status not in {404, 405}:
                raise
            if task_id:
                legacy_query = urllib.parse.urlencode({"task_id": task_id})
                return self._call("legacy", "GET", f"/api/status?{legacy_query}", None)
            legacy_query = urllib.parse.urlencode({"cdk": cdk})
            return self._call("legacy", "GET", f"/api/user-status?{legacy_query}", None)


def build_mail_body(account: dict[str, str], mailbox: str) -> dict[str, str]:
    return {
        "action": "mail_all",
        "email": account["email"],
        "client_id": account["client_id"],
        "refresh_token": account["refresh_token"],
        "mailbox": mailbox or "INBOX",
        "response_type": "json",
    }


def fetch_mailbox_messages(account: dict[str, str], mailbox: str, timeout: int = 30) -> str:
    _, text = http_request("POST", MAIL_API_URL, build_mail_body(account, mailbox), timeout)
    return text


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            return int(value)
        if value > 1e9:
            return int(value * 1000)
        return None
    if isinstance(value, str):
        raw = value.strip()
        if re.match(r"^\d{13}$", raw):
            return int(raw)
        if re.match(r"^\d{10}$", raw):
            return int(raw) * 1000
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed:
                return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            normalized = raw.replace("Z", "+00:00")
            parsed = _dt.datetime.fromisoformat(normalized)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None
    return None


def normalize_six_digit_code(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    compact = re.sub(r"[\s-]", "", raw)
    if re.match(r"^\d{6}$", compact):
        return compact
    match = re.search(r"(?<!\d)\d{6}(?!\d)", raw)
    return match.group(0) if match else None


def collect_mail_text(node: Any) -> str:
    parts: list[str] = []
    if not isinstance(node, dict):
        return ""
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    for value in node.values():
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def extract_mail_timestamp(node: Any) -> int | None:
    if not isinstance(node, dict):
        return None
    for key in TIME_KEYS:
        if key in node:
            ts = parse_timestamp(node[key])
            if ts:
                return ts
    return None


def is_verification_mail_text(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    sender_hit = any(hint.lower() in lower for hint in SENDER_HINTS)
    subject_hit = any(hint.lower() in lower for hint in SUBJECT_HINTS)
    return sender_hit or subject_hit


def find_codes_in_text(text: str) -> list[str]:
    if not text:
        return []
    found: set[str] = set()
    patterns = [
        re.compile(
            r"(?:temporary\s+openai\s+login\s+code|temporary\s+verification\s+code|"
            r"verification\s+code\s+to\s+continue|verification\s+code|one[-\s]?time\s+code|"
            r"login\s+code|验证码)[:：\s\S]{0,120}?(\d{3}[\s-]?\d{3})",
            re.I,
        ),
        re.compile(r"(?:code|验证码|verification|verify|otp)[:\s#-]*(\d{3}[\s-]?\d{3})", re.I),
        re.compile(r"(?:code|验证码|verification|verify|otp)\s*(?:is|为|是)?\s*[:：#-]?\s*(\d{3}[\s-]?\d{3})", re.I),
        re.compile(r"(?<![#A-Fa-f0-9])\d{6}(?![A-Fa-f0-9])"),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            code = normalize_six_digit_code(match.group(1) if match.groups() else match.group(0))
            if code:
                found.add(code)
    return list(found)


def score_candidate(candidate: dict[str, Any], code_valid_window_ms: int, not_before_ms: int | None) -> int:
    current_ms = int(time.time() * 1000)
    score = 0
    if candidate.get("from_api_field"):
        score += 200
    if candidate.get("is_verification"):
        score += 120
    if candidate.get("mailbox") == "Junk":
        score += 30

    mail_time = candidate.get("time")
    if mail_time is not None:
        if not_before_ms and mail_time < not_before_ms - 60_000:
            score -= 300
        age = current_ms - int(mail_time)
        if 0 <= age <= code_valid_window_ms:
            score += 80 - min(80, int(age / 1000))
        elif age < 0:
            score += 20
        else:
            score -= 200
    elif candidate.get("is_verification"):
        score += 40
    else:
        score -= 80
    return score


def extract_verification_candidates(
    raw_text: str,
    mailbox: str = "INBOX",
    not_before_ms: int | None = None,
    code_valid_window_ms: int = 10 * 60 * 1000,
) -> list[dict[str, Any]]:
    try:
        root: Any = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError):
        root = None

    mailbox_label = next((box["label"] for box in MAILBOXES if box["id"] == mailbox), mailbox)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(entry: dict[str, Any]) -> None:
        code = normalize_six_digit_code(entry.get("code"))
        if not code:
            return
        key = f"{code}|{entry.get('time') or ''}|{mailbox}"
        if key in seen:
            return
        seen.add(key)
        record = {
            "code": code,
            "time": entry.get("time"),
            "mailbox": mailbox,
            "mailbox_label": mailbox_label,
            "is_verification": bool(entry.get("is_verification")),
            "from_api_field": bool(entry.get("from_api_field")),
        }
        record["score"] = score_candidate(record, code_valid_window_ms, not_before_ms)
        if record["score"] >= 0:
            candidates.append(record)

    def read_direct_code(node: Any, inherited_time: int | None = None) -> None:
        if not isinstance(node, dict):
            return
        merged = collect_mail_text(node)
        mail_time = extract_mail_timestamp(node) or inherited_time
        for key in DIRECT_CODE_KEYS:
            if key not in node:
                continue
            code = normalize_six_digit_code(node[key])
            if code:
                add_candidate(
                    {
                        "code": code,
                        "time": mail_time,
                        "is_verification": True or is_verification_mail_text(merged),
                        "from_api_field": True,
                    }
                )

    def walk(node: Any, inherited_time: int | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_time)
            return
        if not isinstance(node, dict):
            return

        read_direct_code(node, inherited_time)
        merged = collect_mail_text(node)
        mail_time = extract_mail_timestamp(node) or inherited_time
        is_verification = is_verification_mail_text(merged)
        for code in find_codes_in_text(merged):
            if not mail_time and not is_verification:
                continue
            if mail_time:
                current_ms = int(time.time() * 1000)
                age = current_ms - mail_time
                if age < -60_000 or age > code_valid_window_ms:
                    continue
                if not_before_ms and mail_time < not_before_ms - 60_000:
                    continue
            add_candidate(
                {
                    "code": code,
                    "time": mail_time,
                    "is_verification": is_verification,
                    "from_api_field": False,
                }
            )

        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, mail_time or inherited_time)

    if root is not None:
        walk(root, extract_mail_timestamp(root))
    elif isinstance(raw_text, str) and raw_text.strip():
        is_verification = is_verification_mail_text(raw_text)
        if is_verification:
            for code in find_codes_in_text(raw_text):
                add_candidate(
                    {
                        "code": code,
                        "time": None,
                        "is_verification": True,
                        "from_api_field": False,
                    }
                )

    candidates.sort(key=lambda item: (item.get("score") or 0, item.get("time") or 0), reverse=True)
    return candidates


def fetch_verification_from_mailboxes(
    account: dict[str, str],
    not_before_ms: int | None,
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any]]:
    candidate_lists: list[list[dict[str, Any]]] = []
    errors: list[str] = []
    mailbox_stats: list[dict[str, Any]] = []
    for box in MAILBOXES:
        try:
            raw_text = fetch_mailbox_messages(account, box["id"], int(config["request_timeout"]))
            candidates = extract_verification_candidates(
                raw_text,
                mailbox=box["id"],
                not_before_ms=not_before_ms,
                code_valid_window_ms=int(config["code_valid_window_ms"]),
            )
            candidate_lists.append(candidates)
            latest_time = max((int(item["time"]) for item in candidates if item.get("time")), default=None)
            mailbox_stats.append(
                {
                    "mailbox": box["id"],
                    "label": box["label"],
                    "count": len(candidates),
                    "latest_time": latest_time,
                }
            )
        except Exception as exc:  # 邮箱接口异常不直接结束，继续尝试其他邮箱。
            errors.append(str(exc))
            mailbox_stats.append(
                {
                    "mailbox": box["id"],
                    "label": box["label"],
                    "count": 0,
                    "latest_time": None,
                    "error": str(exc)[:180],
                }
            )
    merged = [item for group in candidate_lists for item in group]
    fresh = [item for item in merged if is_fresh_candidate(item, not_before_ms)]
    stale = [item for item in merged if item.get("time") and not is_fresh_candidate(item, not_before_ms)]
    untimed = [item for item in merged if not item.get("time")]

    best: dict[str, Any] | None = None
    if fresh:
        fresh.sort(key=lambda item: (int(item.get("time") or 0), int(item.get("score") or 0)), reverse=True)
        best = fresh[0]
        best["freshness"] = "fresh"
    elif untimed:
        untimed.sort(key=lambda item: (int(item.get("score") or 0), int(item.get("time") or 0)), reverse=True)
        best = untimed[0]
        best["freshness"] = "untimed_fallback"

    meta = {
        "candidate_count": len(merged),
        "fresh_count": len(fresh),
        "stale_count": len(stale),
        "untimed_count": len(untimed),
        "mailboxes": mailbox_stats,
        "baseline_time": not_before_ms,
        "latest_candidate_time": max((int(item["time"]) for item in merged if item.get("time")), default=None),
    }
    return best, errors, meta


def wait_for_email_code(
    task_id: str,
    account: dict[str, str],
    not_before_ms: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.time() + int(config["mail_poll_timeout"])
    round_no = 0
    append_task_log(
        task_id,
        f"开始轮询邮箱验证码：邮箱={account['email']}，优先选择 {iso_time_ms(not_before_ms)} 之后的新邮件",
    )
    while time.time() < deadline:
        round_no += 1
        best, errors, meta = fetch_verification_from_mailboxes(account, not_before_ms, config)
        mailbox_hint = "；".join(
            f"{box['label']}候选{box['count']}个"
            + (f"，最新{iso_time_ms(box['latest_time'])}" if box.get("latest_time") else "")
            + (f"，异常：{box['error']}" if box.get("error") else "")
            for box in meta["mailboxes"]
        )
        if best:
            source = best.get("mailbox_label") or best.get("mailbox") or "邮箱"
            freshness = str(best.get("freshness") or "")
            freshness_hint = (
                "新邮件候选"
                if freshness == "fresh"
                else "无邮件时间候选，仅按接口返回与关键词评分选择"
            )
            append_task_log(
                task_id,
                (
                    f"第{round_no}轮取码：候选{meta['candidate_count']}个，"
                    f"新候选{meta['fresh_count']}个，旧候选{meta['stale_count']}个，"
                    f"无时间候选{meta['untimed_count']}个；选择验证码={best['code']}，"
                    f"来源={source}，{describe_mail_time(best, not_before_ms)}，"
                    f"score={best.get('score')}，判定={freshness_hint}"
                ),
            )
            return best
        hint = (
            f"第{round_no}轮取码：暂无可用新验证码；候选{meta['candidate_count']}个，"
            f"新候选{meta['fresh_count']}个，旧候选{meta['stale_count']}个，"
            f"无时间候选{meta['untimed_count']}个；{mailbox_hint or '邮箱无候选'}"
        )
        if meta.get("latest_candidate_time"):
            hint += f"；最新候选邮件时间={iso_time_ms(meta['latest_candidate_time'])}"
        if errors and round_no % 3 == 0:
            hint += f"；邮箱接口异常：{errors[0][:120]}"
        append_task_log(task_id, hint)
        time.sleep(max(1, int(config["mail_poll_interval"])))
    raise AppError("等待邮箱验证码超时")


def normalize_remote_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").lower()
    if status:
        return status
    if payload.get("success") is True:
        return "completed"
    return "running"


def norm_text(value: Any) -> str:
    return str(value or "").strip().lower()


def is_completed_payload(payload: dict[str, Any]) -> bool:
    return normalize_remote_status(payload) == "completed" or payload.get("success") is True


def completed_status_matches_account(payload: dict[str, Any], email_addr: str, cdk: str) -> tuple[bool, str | None]:
    if norm_text(payload.get("cdk")) != norm_text(cdk):
        return False, None

    target_email = norm_text(email_addr)
    root_task_id = payload.get("task_id") or payload.get("id")
    if norm_text(payload.get("email")) == target_email and is_completed_payload(payload):
        return True, str(root_task_id or "")

    for key in ["emails", "tasks"]:
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if norm_text(item.get("email")) == target_email and is_completed_payload(item):
                return True, str(item.get("task_id") or root_task_id or "")

    return False, None


def reconcile_completed_by_cdk(
    task_id: str,
    client: CodexClient,
    account: dict[str, str],
    cdk: str,
    reason: str,
) -> bool:
    append_task_log(task_id, f"远端返回异常：{reason}；开始按 CDK 反查状态")
    try:
        status_data = client.status(cdk=cdk)
    except Exception as exc:
        append_task_log(task_id, f"CDK 反查失败：{str(exc) or exc.__class__.__name__}")
        return False

    matched, remote_task_id = completed_status_matches_account(status_data, account["email"], cdk)
    if not matched:
        append_task_log(
            task_id,
            (
                "CDK 反查未确认成功："
                f"返回CDK={status_data.get('cdk') or '-'}，"
                f"返回邮箱={status_data.get('email') or '-'}，"
                f"状态={status_data.get('status') or '-'}，"
                f"success={status_data.get('success')}"
            ),
        )
        return False

    update_task(
        task_id,
        codex_task_id=remote_task_id or task_id,
        status_payload=status_data,
        message="CDK 反查确认远端已完成",
    )
    append_task_log(
        task_id,
        f"CDK 反查确认成功：邮箱={account['email']}，CDK={cdk}，远端任务={remote_task_id or '-'}",
    )
    finish_task_by_status(task_id, "completed", account["raw"], cdk)
    return True


def run_automation_task(task_id: str) -> None:
    config = load_config()
    client = CodexClient(config)
    task = get_task(task_id)
    account = parse_email_record(task["email_record"])
    cdk = task["cdk"]

    try:
        update_task(task_id, status="starting", message="正在提交邮箱和 CDK")
        not_before_ms = int(time.time() * 1000)
        update_task(task_id, email_request_at_ms=not_before_ms)
        append_task_log(
            task_id,
            (
                f"正在提交邮箱和 CDK：邮箱={account['email']}，CDK={cdk}，"
                f"API={client.base_url}，模式={client.api_mode}，取码基准={iso_time_ms(not_before_ms)}"
            ),
        )
        try:
            start_data = client.start(cdk, account["email"])
        except AppError as exc:
            if reconcile_completed_by_cdk(task_id, client, account, cdk, str(exc)):
                return
            raise
        remote_task_id = start_data.get("task_id") or start_data.get("id") or task_id
        remote_status = normalize_remote_status(start_data)
        update_task(
            task_id,
            codex_task_id=remote_task_id,
            status="waiting_email_otp" if remote_status == "waiting_email_otp" else remote_status,
            status_payload=start_data,
        )
        append_task_log(
            task_id,
            (
                f"Codex 任务已创建：远端任务={remote_task_id}，远端状态={remote_status}，"
                f"邮箱={account['email']}，CDK={cdk}，消息={start_data.get('message') or '-'}"
            ),
        )

        if remote_status == "waiting_email_otp":
            best_code = wait_for_email_code(task_id, account, not_before_ms, config)
            code = str(best_code["code"])
            code_source = best_code.get("mailbox_label") or best_code.get("mailbox")
            update_task(
                task_id,
                status="email_code_found",
                email_code=code,
                code_source=code_source,
                code_mail_time=best_code.get("time"),
                code_freshness=describe_mail_time(best_code, not_before_ms),
                message="已获取邮箱验证码",
            )
            record_email_status(
                task["email_record"],
                has_received_code=True,
                last_cdk=cdk,
                last_task_id=task_id,
            )
            append_task_log(
                task_id,
                (
                    f"正在提交邮箱验证码：邮箱={account['email']}，CDK={cdk}，"
                    f"验证码={code}，来源={code_source or '-'}，{describe_mail_time(best_code, not_before_ms)}"
                ),
            )
            submit_data = client.submit_email_code(str(remote_task_id), code)
            remote_status = normalize_remote_status(submit_data)
            update_task(task_id, status=remote_status, status_payload=submit_data)
            append_task_log(
                task_id,
                (
                    f"邮箱验证码已提交：远端状态={remote_status}，邮箱={account['email']}，"
                    f"CDK={cdk}，消息={submit_data.get('message') or '-'}"
                ),
            )

        if remote_status in {"completed", "failed"}:
            finish_task_by_status(task_id, remote_status, task["email_record"], cdk)
            return

        poll_remote_status(task_id, client, str(remote_task_id), cdk, config)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        update_task(task_id, status="failed", message=message, completed_at=now_ts())
        append_task_log(task_id, "任务失败：" + message)


def finish_task_by_status(task_id: str, status: str, email_record: str, cdk: str) -> None:
    account = parse_email_record(email_record)
    if status == "completed":
        task = get_task(task_id)
        status_patch: dict[str, Any] = {
            "is_registered": True,
            "last_cdk": cdk,
            "last_task_id": task_id,
        }
        if task.get("email_code"):
            status_patch["has_received_code"] = True
        record_email_status(email_record, **status_patch)
        move_value("cdk", "unused", "used", cdk)
        update_task(task_id, status="completed", completed_at=now_ts(), message="任务已完成，邮箱已登记为已注册，CDK 已移入已使用列表")
        append_task_log(task_id, f"任务已完成：邮箱={account['email']} 已登记为已注册，CDK={cdk} 已移入已使用列表")
    else:
        update_task(task_id, status="failed", completed_at=now_ts())
        append_task_log(task_id, f"远端任务失败：邮箱={account['email']} 不写入已注册列表，CDK={cdk} 保留在未使用列表")


def poll_remote_status(task_id: str, client: CodexClient, remote_task_id: str, cdk: str, config: dict[str, Any]) -> None:
    deadline = time.time() + int(config["status_poll_timeout"])
    while time.time() < deadline:
        data = client.status(task_id=remote_task_id)
        status = normalize_remote_status(data)
        update_task(task_id, status=status, status_payload=data)
        append_task_log(task_id, data.get("message") or f"远端状态：{status}")
        if status in {"completed", "failed"}:
            task = get_task(task_id)
            finish_task_by_status(task_id, status, task["email_record"], cdk)
            return
        time.sleep(max(1, int(config["status_poll_interval"])))
    update_task(task_id, status="failed", completed_at=now_ts(), message="轮询远端状态超时")
    append_task_log(task_id, "轮询远端状态超时")


def filtered_tasks(query: str = "") -> list[dict[str, Any]]:
    needle = (query or "").lower().strip()
    tasks = load_tasks()
    if not needle:
        return tasks
    result = []
    for task in tasks:
        text = json.dumps(task, ensure_ascii=False).lower()
        if needle in text:
            result.append(task)
    return result


def clear_task_logs(query: str = "") -> dict[str, int]:
    needle = (query or "").lower().strip()
    with TASK_LOCK:
        tasks = load_tasks()
        cleared = 0
        for task in tasks:
            if needle and needle not in json.dumps(task, ensure_ascii=False).lower():
                continue
            if task.get("logs"):
                task["logs"] = []
                task["updated_at"] = now_ts()
                cleared += 1
        save_tasks(tasks)
    return {"cleared": cleared}


def reconcile_failed_completed_tasks(query: str = "") -> dict[str, int]:
    needle = (query or "").lower().strip()
    client = CodexClient(load_config())
    candidates = []
    for task in load_tasks():
        if task.get("status") != "failed":
            continue
        if needle and needle not in json.dumps(task, ensure_ascii=False).lower():
            continue
        if not task.get("email_record") or not task.get("cdk"):
            continue
        candidates.append(task)

    reconciled = 0
    checked = 0
    for task in candidates:
        checked += 1
        try:
            account = parse_email_record(str(task["email_record"]))
            if reconcile_completed_by_cdk(
                str(task["id"]),
                client,
                account,
                str(task["cdk"]),
                "手动纠偏 failed 任务",
            ):
                reconciled += 1
        except Exception as exc:
            try:
                append_task_log(str(task["id"]), f"纠偏检查异常：{str(exc) or exc.__class__.__name__}")
            except Exception:
                traceback.print_exc()
    return {"checked": checked, "reconciled": reconciled}


def gpt_login_account_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    status = "completed"
    if record.get("register_status") == store.EMAIL_STATUS_UNREGISTERED:
        status = "in_progress" if record.get("reserved_at") else "not_started"
    return {
        "raw": record.get("raw") or "",
        "email": record.get("email") or "",
        "password": record.get("password") or "",
        "client_id": record.get("client_id") or "",
        "refresh_token": record.get("refresh_token") or "",
        "status": status,
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


class AppHandler(BaseHTTPRequestHandler):
    server_version = "CodexAutomation/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (iso_time(), fmt % args))

    def do_GET(self) -> None:
        try:
            path, query = self.parse_url()
            if path in {"/", "/index.html"}:
                self.send_file(INDEX_FILE, "text/html; charset=utf-8")
                return
            if path == "/api/config":
                self.send_json({"code": 0, "data": load_config()})
                return
            if path == "/api/stats":
                self.send_json({"code": 0, "data": stats()})
                return
            if path == "/api/emails":
                data = list_email_statuses(
                    query.get("status", ["registered"])[0],
                    query.get("sale", ["unsold"])[0],
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
                    query.get("kind", [""])[0],
                    query.get("bucket", [""])[0],
                    query.get("q", [""])[0],
                    query.get("page", ["1"])[0],
                    query.get("page_size", ["20"])[0],
                )
                self.send_json({"code": 0, "data": data})
                return
            if path == "/api/tasks":
                self.send_json({"code": 0, "data": filtered_tasks(query.get("q", [""])[0])})
                return
            if path == "/api/task":
                self.send_json({"code": 0, "data": get_task(query.get("id", [""])[0])})
                return
            if path == "/api/codex-status":
                client = CodexClient(load_config())
                data = client.status(
                    task_id=query.get("task_id", [""])[0] or None,
                    cdk=query.get("cdk", [""])[0] or None,
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
                data = add_items(str(body.get("kind") or ""), str(body.get("bucket") or ""), body.get("text") or body.get("values") or "")
                self.send_json({"code": 0, "message": "已添加", "data": data})
                return
            if path == "/api/emails/export":
                data = export_email_statuses(body)
                self.send_json({"code": 0, "message": "邮箱已导出并标记为已售出", "data": data})
                return
            if path == "/api/automation/start":
                task = create_automation_task(body.get("email") or None, body.get("cdk") or None)
                self.send_json({"code": 0, "message": "任务已启动", "data": task})
                return
            if path == "/api/automation/start-all":
                data = create_automation_batch()
                self.send_json({"code": 0, "message": "批量任务已启动", "data": data})
                return
            if path == "/api/automation/reconcile":
                data = reconcile_failed_completed_tasks(str(body.get("q") or ""))
                self.send_json({"code": 0, "message": "纠偏检查完成", "data": data})
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
            if path == "/api/config":
                self.send_json({"code": 0, "message": "设置已保存", "data": save_config(self.read_json_body())})
                return
            if path == "/api/email-sale":
                body = self.read_json_body()
                data = update_email_sale_status(body.get("values") or [], bool_value(body.get("is_sold")))
                self.send_json({"code": 0, "message": "邮箱卖出状态已更新", "data": data})
                return
            raise AppError("接口不存在", 404)
        except Exception as exc:
            self.send_error_json(exc)

    def do_DELETE(self) -> None:
        try:
            path, _ = self.parse_url()
            body = self.read_json_body()
            if path == "/api/list":
                data = delete_items(str(body.get("kind") or ""), str(body.get("bucket") or ""), body.get("values") or [])
                self.send_json({"code": 0, "message": "已删除", "data": data})
                return
            if path == "/api/emails":
                data = delete_email_statuses(body.get("values") or [])
                self.send_json({"code": 0, "message": "邮箱已删除", "data": data})
                return
            if path == "/api/task-logs":
                data = clear_task_logs(str(body.get("q") or ""))
                self.send_json({"code": 0, "message": "日志已清空", "data": data})
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
    warm_task_executor()
    server = ThreadingHTTPServer((host, port), AppHandler)
    url_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    print(f"Codex 自动化服务已启动：http://{url_host}:{port}/")
    print(f"后台任务线程池已启动：最小 {ASYNC_WORKER_MIN} 个，最大 {ASYNC_WORKER_MAX} 个")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("服务已停止")
    finally:
        server.server_close()
        shutdown_task_executor(wait=False)


def run_once_and_wait() -> None:
    ensure_storage()
    task = create_automation_task()
    print(json.dumps({"message": "任务已启动", "task": task}, ensure_ascii=False, indent=2))
    while True:
        current = get_task(task["id"])
        if current.get("status") in {"completed", "failed", "canceled"}:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            return
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex 自动接码脚本")
    parser.add_argument("--host", default="127.0.0.1", help="本地服务监听地址")
    parser.add_argument("--port", type=int, default=8060, help="本地服务端口")
    parser.add_argument("--base-url", help="Codex API 基础地址，例如 https://www.hansaes.icu/")
    parser.add_argument("--api-mode", choices=["legacy", "v1", "auto"], help="接口模式")
    parser.add_argument("--run-once", action="store_true", help="只执行一次自动流程，不启动页面服务")
    args = parser.parse_args()

    ensure_storage()
    patch: dict[str, Any] = {}
    if args.base_url:
        patch["codex_base_url"] = args.base_url
    if args.api_mode:
        patch["api_mode"] = args.api_mode
    if patch:
        save_config(patch)

    if args.run_once:
        try:
            run_once_and_wait()
        finally:
            shutdown_task_executor(wait=True)
    else:
        run_server(args.host, args.port)


if __name__ == "__main__":
    main()
