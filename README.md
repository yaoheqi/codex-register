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

邮箱注册状态分为 `unregistered`（未注册）、`registered`（已注册）、`received`（已接码）、`failed`（失败）。`gpt-login` 邮箱池里的 `进行中` 是 `unregistered` 邮箱被写入 `reserved_at` 后的占用态，可通过管理页或 `/api/gpt-login/mail-pool/reset` 清回 `not_started`。自动接码任务只消耗 `registered` 邮箱，接码成功后进入 `received`。邮箱售出状态分为 `unsold`（未售出）和 `sold`（已售出），只统计并作用于 `registered` 与 `received` 邮箱。CDK 状态分为 `unused`（未使用）和 `used`（已使用）。

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

任务开始时第一步只按 CDK 查询远端状态，不会先提交邮箱。只要远端返回该 CDK 已有关联任务或绑定邮箱（包括 running、waiting_email_otp、completed 等状态），脚本会先用远端返回邮箱更新本地状态并把 CDK 标记为已使用，不再继续提交当前邮箱绑定；只有预查询明确未发现远端占用时，才会提交邮箱和 CDK 进入验证码流程。远端 confirmed completed/success=True 时，对应邮箱会登记为已接码。

资源查看页支持单个和批量更新状态：邮箱可在未注册、已注册、已接码、失败之间切换；CDK 可在未使用、已使用之间切换。

每次开始单个或批量任务前，会自动清空已有任务池记录；任务流只显示本轮新创建的任务。

已使用 CDK 会保存绑定邮箱和远端任务关系；已带 `last_cdk` 的已注册邮箱不会再次进入自动绑定调度，避免同一个邮箱被重复绑定。
