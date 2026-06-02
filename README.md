# Codex 本地资源管理台

本目录只保留本地资源管理能力：邮箱资源、GPT 登录池状态、Codex 凭据摘要与完整凭据读取。

## 启动

```bash
python codex_automation.py
```

默认打开：

```text
http://127.0.0.1:8060/
```

## 数据目录

```text
data/
  resources.sqlite3
```

邮箱写入 `resources.sqlite3`。旧邮箱文件按状态来源导入，并可通过同步接口重新并入 SQLite：

```text
gpt-login/mail.csv              -> unregistered（可用未注册）
data/emails_unused/mail.csv     -> registered（已注册）
data/emails_used/mail.csv       -> received（已接码）
```

邮箱注册状态分为 `unregistered`（未注册）、`registered`（已注册）、`received`（已接码）、`failed`（失败）。`gpt-login` 邮箱池里的 `进行中` 是 `unregistered` 邮箱被写入 `reserved_at` 后的占用态，可通过管理页或 `/api/gpt-login/mail-pool/reset` 清回 `not_started`。邮箱售出状态分为 `unsold`（未售出）和 `sold`（已售出），只统计并作用于 `registered` 与 `received` 邮箱。

## 本地接口

供 `gpt-login` 调用的邮箱池接口：

```text
GET  /api/gpt-login/mail-pool
POST /api/gpt-login/mail-pool/claim
POST /api/gpt-login/mail-pool/mark
POST /api/gpt-login/mail-pool/reset
POST /api/gpt-login/mail-pool/sync
```

合并后的 `gpt-login` 扩展会把 Codex RT 凭据同步到本地管理台：

```text
POST   /api/codex-credentials
GET    /api/codex-credentials
GET    /api/codex-credentials/:id
DELETE /api/codex-credentials/:id
```

列表接口只返回邮箱、账号、过期时间、token 类型等摘要；详情接口才返回完整 `credential`。

## 管理页

管理页支持：

- 写入邮箱账号。
- 按状态、售卖状态、邮箱或 `client_id` 搜索邮箱。
- 批量更新邮箱状态、导出 CSV、删除邮箱。
- 查看和手动调整 GPT 登录池状态。
- 查看、复制、下载、删除 Codex 凭据。
