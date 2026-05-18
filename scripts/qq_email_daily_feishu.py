#!/usr/bin/env python3
"""每日自动提取 QQ 邮箱日程，同步到飞书文档 + 飞书日历

配置方式（三选一）：
  1. 环境变量：~/.hermes/.env 中设置 FEISHU_DOC_TOKEN, FEISHU_WORK_DIR
  2. 命令行参数：--doc-token, --work-dir
  3. 直接修改下方默认值
"""

import os
import sys
import subprocess
import json
import argparse
import re
import shlex
from datetime import datetime, timedelta

# ── 默认配置（可通过环境变量或命令行覆盖）──
DEFAULT_DOC_TOKEN = os.getenv("FEISHU_DOC_TOKEN", "")
DEFAULT_WORK_DIR = os.getenv("FEISHU_WORK_DIR", "")
SCAN_DAYS = 7

# 添加 skill 脚本路径
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)


def _bat_escape(s: str) -> str:
    """Escape string for Windows batch file"""
    # 转义 cmd.exe 特殊字符: & | < > ^ %
    for ch in ['&', '|', '<', '>', '^']:
        s = s.replace(ch, f'^{ch}')
    s = s.replace('"', '""').replace('%', '%%')
    return s

# 日历事件去重记录（避免重复创建）
CALENDAR_RECORD_PATH = os.path.expanduser("~/.hermes/tmp/calendar_events_created.json")


def _load_calendar_record() -> set:
    """加载已创建的日历事件记录"""
    if os.path.isfile(CALENDAR_RECORD_PATH):
        with open(CALENDAR_RECORD_PATH, "r") as f:
            return set(json.load(f))
    return set()


def _save_calendar_record(record: set):
    """保存已创建的日历事件记录"""
    os.makedirs(os.path.dirname(CALENDAR_RECORD_PATH), exist_ok=True)
    with open(CALENDAR_RECORD_PATH, "w") as f:
        json.dump(list(record), f)


def _event_key(evt: dict) -> str:
    """生成事件唯一标识"""
    return f"{evt['title']}|{evt['date']}|{evt.get('time', '')}"


def extract_schedules(days: int = SCAN_DAYS):
    """提取近 N 天邮件中的日程"""
    from scripts.qq_email import search_emails, _extract_schedule

    emails = search_emails(days=days)

    all_events = []
    for em in emails:
        events = _extract_schedule(em["body"], em)
        if events:
            all_events.extend(events)

    # 去重（同标题+同日期+同时间只保留一个）
    seen = set()
    unique = []
    for evt in all_events:
        key = (evt["title"], evt["date"], evt.get("time", ""))
        if key not in seen:
            seen.add(key)
            unique.append(evt)

    # 过滤广告和垃圾
    spam_keywords = [
        "职位推荐", "征文邀请", "有奖调研", "积分", "推广",
        "订阅", "newsletter", "unsubscribe", "退订",
    ]
    spam_senders = [
        "zhaopin", "newsletter", "eefocus", "growthmail",
        "growth-mail", "info.eefocus", "cnkicfp",
    ]
    filtered = [
        e for e in unique
        if not any(kw in e["title"].lower() for kw in spam_keywords)
        and not any(kw in (e.get("source_email", "").lower()) for kw in spam_senders)
    ]

    # 按日期排序
    filtered.sort(key=lambda e: (e["date"], e.get("time", "")))

    return len(emails), filtered


def format_markdown(total_emails: int, events: list) -> str:
    """格式化为飞书 Markdown"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    lines = [
        "# QQ邮箱日程提取 - 近7日",
        "",
        f"自动更新时间: {now}",
        f"扫描范围: 最近7天邮件（{total_emails}封）",
        f"有效日程: {len(events)}项",
        "",
        "---",
        "",
    ]

    upcoming = [e for e in events if e["date"] >= today]
    past = [e for e in events if e["date"] < today]

    if upcoming:
        lines.append("## 待办日程")
        lines.append("")
        for i, evt in enumerate(upcoming, 1):
            priority_icon = {"high": "!!", "medium": "!", "normal": ""}.get(evt["priority"], "")
            prefix = f"[{priority_icon}] " if priority_icon else ""

            date_display = evt["date"]
            if date_display == today:
                date_display += "（今天）"
            elif date_display == tomorrow:
                date_display += "（明天）"

            lines.append(f"### {i}. {prefix}{evt['title']}")
            lines.append(f"- 日期: {date_display}")
            if evt.get("time"):
                lines.append(f"- 时间: {evt['time']}")
            if evt.get("location"):
                lines.append(f"- 地点: {evt['location']}")
            if evt.get("source_email"):
                lines.append(f"- 来源: {evt['source_email']}")
            lines.append("")

    if past:
        lines.append("## 已过期（留档参考）")
        lines.append("")
        for evt in past:
            lines.append(f"- {evt['date']} {evt.get('time', '')} {evt['title']}")
        lines.append("")

    if not events:
        lines.append("近7天未发现有效日程。")
        lines.append("")

    return "\n".join(lines)


def create_calendar_events(events: list, work_dir: str) -> tuple:
    """将日程事件创建到飞书日历

    返回: (成功数, 跳过数, 失败数, 详情列表)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    upcoming = [e for e in events if e["date"] >= today]

    if not upcoming:
        return 0, 0, 0, ["无待办日程，跳过日历创建"]

    # 加载已创建记录
    created_record = _load_calendar_record()

    # WSL 路径转换
    win_dir = work_dir
    if work_dir.startswith("/mnt/"):
        parts = work_dir.split("/")
        if len(parts) >= 3:
            drive = parts[2].upper()
            rest = "\\".join(parts[3:])
            win_dir = f"{drive}:\\{rest}"

    success, skipped, failed = 0, 0, 0
    details = []

    for evt in upcoming:
        key = _event_key(evt)

        # 去重：已创建过的跳过
        if key in created_record:
            skipped += 1
            details.append(f"  [SKIP] {evt['title']} ({evt['date']}) - 已创建过")
            continue

        # 构建时间
        time_str = evt.get("time", "")
        if time_str and re.match(r'\d{2}:\d{2}', time_str):
            start_iso = f"{evt['date']}T{time_str}:00+08:00"
            # 默认事件时长 1 小时
            h, m = map(int, time_str.split(":"))
            end_h, end_m = h + 1, m
            if end_h >= 24:
                end_h = 23
                end_m = 59
            end_iso = f"{evt['date']}T{end_h:02d}:{end_m:02d}:00+08:00"
        else:
            # 无具体时间，默认全天事件
            start_iso = f"{evt['date']}T09:00:00+08:00"
            end_iso = f"{evt['date']}T10:00:00+08:00"

        # 构建描述（纯 ASCII，避免特殊字符和 / 导致参数解析错误）
        desc_parts = []
        if evt.get("location"):
            loc = evt['location']
            if loc.isascii() and '@' not in loc:
                desc_parts.append(f"Location: {loc}")
        desc_parts.append(f"Priority: {evt['priority']}")
        description = ", ".join(desc_parts)

        # 转义特殊字符（保留中文标题，chcp 65001 已解决编码问题）
        summary = evt["title"][:80]  # 截断过长标题
        summary = summary.replace('"', "'")
        description = description.replace('"', "'")

        # 用临时 .bat 文件执行（避免 shell 转义问题）
        lines = [
            '@echo off',
            'chcp 65001 >nul',  # 切换到 UTF-8 编码，解决中文乱码
            f'cd /d {win_dir}',
            f'npx lark-cli calendar +create --summary "{_bat_escape(summary)}" --start "{start_iso}" --end "{end_iso}" --description "{_bat_escape(description)}" --as user',
        ]
        bat_content = '\r\n'.join(lines) + '\r\n'
        bat_path = os.path.join(work_dir, "_create_event.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)

        try:
            win_bat = bat_path
            if bat_path.startswith("/mnt/"):
                parts = bat_path.split("/")
                if len(parts) >= 3:
                    drive = parts[2].upper()
                    rest = "\\".join(parts[3:])
                    win_bat = f"{drive}:\\{rest}"
            result = subprocess.run(f'cmd.exe /c "{win_bat}"', shell=True, capture_output=True, timeout=20, cwd=work_dir)
            stdout = result.stdout.decode("utf-8", errors="replace")

            if result.returncode == 0:
                try:
                    resp = json.loads(stdout.strip().split("\n")[-1] if stdout else "{}")
                    if resp.get("ok", True):  # 空响应视为成功
                        success += 1
                        created_record.add(key)
                        details.append(f"  [OK] {evt['title']} ({evt['date']} {time_str})")
                    else:
                        failed += 1
                        details.append(f"  [FAIL] {evt['title']} - API错误")
                except (json.JSONDecodeError, IndexError):
                    # lark-cli 可能返回非 JSON 输出但实际成功
                    success += 1
                    created_record.add(key)
                    details.append(f"  [OK] {evt['title']} ({evt['date']} {time_str})")
            else:
                failed += 1
                stderr = result.stderr.decode("utf-8", errors="replace")
                details.append(f"  [FAIL] {evt['title']} - {stderr[:80]}")
        except subprocess.TimeoutExpired:
            failed += 1
            details.append(f"  [FAIL] {evt['title']} - 超时")
        except Exception as e:
            failed += 1
            details.append(f"  [FAIL] {evt['title']} - {e}")

    # 保存去重记录
    _save_calendar_record(created_record)

    return success, skipped, failed, details


def update_feishu_doc(md_content: str, doc_token: str, work_dir: str) -> tuple:
    """通过 lark-cli 更新飞书文档"""
    import re as _re

    md_path = os.path.join(work_dir, "qq_schedule_7days.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # WSL 环境：将 /mnt/... 转换为 D:\...
    win_dir = work_dir
    if work_dir.startswith("/mnt/"):
        parts = work_dir.split("/")
        if len(parts) >= 3:
            drive = parts[2].upper()
            rest = "\\".join(parts[3:])
            win_dir = f"{drive}:\\{rest}"

    cmd = f'cmd.exe /c "cd /d {win_dir} && npx lark-cli docs +update --doc {doc_token} --markdown @qq_schedule_7days.md --mode overwrite --as user"'

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "lark-cli timeout (30s)"
    except FileNotFoundError:
        cmd_bash = f"cd {work_dir} && npx lark-cli docs +update --doc {doc_token} --markdown @qq_schedule_7days.md --mode overwrite --as user"
        result = subprocess.run(cmd_bash, shell=True, capture_output=True, timeout=30)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

    if result.returncode != 0:
        return False, stderr

    try:
        resp = json.loads(stdout.strip().split("\n")[-1] if stdout else "{}")
        return resp.get("ok", False), stdout
    except (json.JSONDecodeError, IndexError):
        return True, stdout


def main():
    parser = argparse.ArgumentParser(description="QQ邮箱日程 -> 飞书文档 + 飞书日历")
    parser.add_argument("--doc-token", default=DEFAULT_DOC_TOKEN,
                        help="飞书文档 obj_token")
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR,
                        help="工作目录（存放临时 .md 文件）")
    parser.add_argument("--days", type=int, default=SCAN_DAYS,
                        help="扫描最近 N 天邮件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只提取日程，不同步飞书")
    parser.add_argument("--no-calendar", action="store_true",
                        help="不同步飞书日历（仅更新文档）")
    args = parser.parse_args()

    # 1. 提取日程
    total_emails, events = extract_schedules(days=args.days)

    # 2. 格式化
    md = format_markdown(total_emails, events)

    # 3. 输出
    if args.dry_run:
        print(md)
        return 0

    # 4. 同步飞书文档
    doc_ok = False
    doc_detail = ""
    if args.doc_token and args.work_dir:
        doc_ok, doc_detail = update_feishu_doc(md, args.doc_token, args.work_dir)

    # 5. 同步飞书日历
    cal_ok, cal_skip, cal_fail, cal_details = 0, 0, 0, []
    if not args.no_calendar and args.work_dir:
        cal_ok, cal_skip, cal_fail, cal_details = create_calendar_events(events, args.work_dir)

    # 6. 输出结果
    print(f"=== QQ邮箱日程同步结果 ===")
    print(f"邮件扫描: {total_emails} 封")
    print(f"有效日程: {len(events)} 个")
    print()

    if args.doc_token:
        status = "OK" if doc_ok else "FAIL"
        print(f"[飞书文档] {status}")
    else:
        print(f"[飞书文档] 跳过（未配置 FEISHU_DOC_TOKEN）")

    if not args.no_calendar:
        print(f"[飞书日历] 新增: {cal_ok} | 跳过(已存在): {cal_skip} | 失败: {cal_fail}")
        for d in cal_details:
            print(d)
    else:
        print(f"[飞书日历] 跳过（--no-calendar）")

    print()
    print(md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
