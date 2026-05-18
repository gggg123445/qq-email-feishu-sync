---
name: qq-email-scheduler
description: "Connect to QQ email via IMAP, read recent emails, extract Chinese schedule/meeting info, and auto-sync to Feishu docs."
version: 2.0.0
author: Hermes Agent
license: MIT
---

# QQ Email Scheduler + Feishu Sync

See [README.md](README.md) for full documentation.

## Quick Setup

1. Enable IMAP in QQ Mail settings, generate authorization code
2. Copy `.env.example` to `~/.hermes/.env` and fill in credentials
3. Run: `python scripts/qq_email.py schedule --days 7`

## Cron Job

```
创建 cron 任务：每天 00:00 运行 qq_email_daily_feishu.py
```
