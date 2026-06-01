# Codex 自动接码

本目录只保留新的自动化逻辑：提交邮箱和 CDK、读取邮箱验证码、提交邮箱验证码、轮询任务状态，以及一个本地单页管理界面。

## 启动

```bash
python codex_automation.py
```

默认打开：

```text
http://127.0.0.1:8060/
```

默认 Codex API 地址：

```text
https://www.hansaes.icu/
```

## 数据目录

```text
data/
  resources.sqlite3
  tasks.json
  config.json
```

邮箱和 CDK 统一写入 `resources.sqlite3`。邮箱旧文件按状态来源导入，并可通过同步接口重新并入 SQLite：

```text
gpt-login/mail.csv              -> unregistered（可用未注册）
data/emails_unused/mail.csv     -> registered（已注册）
data/emails_used/mail.csv       -> received（已接码）
data/cdks_unused/cdks.txt
data/cdks_used/cdks.txt
```

邮箱注册状态分为 `unregistered`（未注册）、`registered`（已注册）、`received`（已接码）。自动接码任务只消耗 `registered` 邮箱，接码成功后进入 `received`。邮箱售出状态分为 `unsold`（未售出）和 `sold`（已售出），只统计并作用于 `registered` 与 `received` 邮箱。CDK 状态分为 `unused`（未使用）和 `used`（已使用）。

供 `gpt-login` 调用的本地接口：

```text
GET  /api/gpt-login/mail-pool
POST /api/gpt-login/mail-pool/claim
POST /api/gpt-login/mail-pool/mark
POST /api/gpt-login/mail-pool/reset
POST /api/gpt-login/mail-pool/sync
```

## 接口模式

页面右上角可以选择：

- `legacy`：使用 `/api/submit-email`、`/api/submit-email-otp`、`/api/status`。
- `v1`：使用 `/api/v1/codex/start`、`/api/v1/codex/submit-email-code`、`/api/v1/codex/status`。
- `auto`：优先尝试 `v1`，接口不存在时回退到 `legacy`。

任务完成后，脚本会把对应邮箱登记为已注册或已接码，并把 CDK 标记为已使用；失败时邮箱和 CDK 保留为可继续处理的状态。
