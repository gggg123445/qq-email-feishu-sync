# QQ Email Scheduler + Feishu Sync

Connect to QQ email via IMAP, extract Chinese schedule/meeting info from emails, and auto-sync to Feishu (飞书) documents.

## Features

- Read QQ email via IMAP SSL (imap.qq.com:993)
- Extract dates, times, locations, meeting info from Chinese emails
- Regex-based schedule extraction with support for:
  - Absolute dates: `2026年5月20日`, `2026-05-20`, `5月20日`
  - Relative dates: `明天`, `下周二`, `本周五`
  - Chinese time: `上午9:00`, `下午2:30`, `晚上8点`
  - Meeting keywords: 面试/笔试/会议/腾讯会议/Zoom/飞书会议
- Deduplication + spam filtering
- Auto-sync to Feishu docs via lark-cli
- Cron job support for daily updates

## Quick Start

### 1. Get QQ Email Authorization Code

1. Login to https://mail.qq.com
2. Settings -> Account -> Enable IMAP/SMTP
3. Generate authorization code (NOT your QQ password)

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Install Dependencies

```bash
# Python standard library only (imaplib, email, re, json)
# For Feishu sync:
npm install -g @larksuite/cli
```

### 4. Run

```bash
# Read recent emails
python scripts/qq_email.py read --count 10

# Extract schedules from last 7 days
python scripts/qq_email.py schedule --days 7

# Sync to Feishu
python scripts/qq_email_daily_feishu.py --doc-token YOUR_TOKEN --work-dir /path/to/workdir

# Dry run (no Feishu sync)
python scripts/qq_email_daily_feishu.py --dry-run
```

## Architecture

```
qq_email.py                    # Core: IMAP reader + schedule extractor
  ├── read_emails()            # Read N recent emails
  ├── search_emails()          # Search by sender/subject/date
  ├── extract_schedules()      # Extract schedule from emails
  └── _extract_schedule()      # Regex-based date/time/location extraction

qq_email_daily_feishu.py       # Daily sync: extract -> format -> Feishu
  ├── extract_schedules()      # Call qq_email.py
  ├── format_markdown()        # Format as Feishu markdown
  └── update_feishu()          # Push to Feishu via lark-cli
```

## Schedule Extraction Algorithm

1. **Date patterns**: regex for `YYYY年M月D日`, `YYYY-MM-DD`, `M月D日`
2. **Relative dates**: `明天`(+1), `后天`(+2), `下周X`(+7+offset), `本周X`(offset)
3. **Time patterns**: `上午/下午H:MM`, `HH:MM`, `H点`
4. **Meeting detection**: keyword matching for 会议/面试/笔试/Zoom/Teams
5. **Location extraction**: regex for `地点：xxx`, meeting URLs
6. **Priority**: 面试/笔试=high, 会议=medium, other=normal
7. **Post-processing**: dedup by (title, date, time), filter spam senders

## Feishu Integration

Uses [@larksuite/cli](https://github.com/larksuite/cli) (lark-cli):

```bash
# Create doc in wiki
npx lark-cli wiki +node-create --space-id SPACE --obj-type docx --title "Title" --as user

# Update doc content
npx lark-cli docs +update --doc TOKEN --markdown @file.md --mode overwrite --as user
```

## Cron Setup (Hermes Agent)

```python
cronjob(action='create',
    name='QQ邮箱日程-每日更新飞书',
    schedule='0 0 * * *',       # Every day at midnight
    no_agent=True,
    script='scripts/qq_email_daily_feishu.py',
    deliver='origin')
```

## QQ Email IMAP Settings

| Parameter | Value |
|-----------|-------|
| IMAP Host | imap.qq.com |
| IMAP Port | 993 (SSL/TLS) |
| SMTP Host | smtp.qq.com |
| SMTP Port | 465 (SSL) |
| Auth | Email + Authorization Code |

## Pitfalls

1. Authorization code != QQ password (generate in QQ Mail settings)
2. IMAP is disabled by default (enable in QQ Mail settings)
3. Chinese emails may use GB2312/GBK/UTF-8 encoding (auto-detected)
4. `import email` does NOT load submodules - must explicitly `import email.message`
5. WSL: lark-cli via cmd.exe needs `cd /d D:\path` (not UNC paths)

## License

MIT
