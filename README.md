# 邮箱资源管理台

本目录只保留本地邮箱资源管理能力，以及供 `gpt-login` 调用的邮箱池接口。

## 启动

```bash
python codex_automation.py
```

默认地址：

```text
http://127.0.0.1:8060/
```

## 数据

```text
data/
  resources.sqlite3
```

邮箱状态：

```text
unregistered  未注册
registered    已注册
received      已接码
failed        失败
```

源文件同步：

```text
gpt-login/mail.csv              -> unregistered
data/emails_unused/mail.csv     -> registered
data/emails_used/mail.csv       -> received
```

## 接口

管理页使用：

```text
GET    /api/stats
GET    /api/emails
POST   /api/list
PUT    /api/email-status
DELETE /api/emails
POST   /api/emails/export
```

`gpt-login` 使用：

```text
GET  /api/gpt-login/mail-pool
POST /api/gpt-login/mail-pool/claim
POST /api/gpt-login/mail-pool/mark
POST /api/gpt-login/mail-pool/reset
POST /api/gpt-login/mail-pool/sync
```
