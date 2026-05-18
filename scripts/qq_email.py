#!/usr/bin/env python3
"""QQ 邮箱日程提取工具

连接 QQ 邮箱 IMAP，读取邮件，提取中文日程/会议信息。

用法:
    python qq_email.py read [--count N]
    python qq_email.py unread
    python qq_email.py schedule [--days N]
    python qq_email.py search --from ADDR
    python qq_email.py search --subject KEYWORD
"""

import os
import sys
import re
import json
import imaplib
import email
import email.message
import email.header
import email.utils
from email.header import decode_header
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import argparse

# ── 配置 ─────────────────────────────────────────────────────────────
ACCOUNTS = {
    "qq": {
        "env_email": "QQ_EMAIL",
        "env_auth": "QQ_AUTH_CODE",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "label": "QQ邮箱",
    },
    "nudt": {
        "env_email": "NUDT_EMAIL",
        "env_auth": "NUDT_AUTH_CODE",
        "imap_host": "mail.nudt.edu.cn",
        "imap_port": 993,
        "label": "国防科大邮箱",
    },
}


def _get_all_accounts() -> List[Dict]:
    """从环境变量获取所有已配置的邮箱账户"""
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.hermes/.env"))

    accounts = []
    for key, cfg in ACCOUNTS.items():
        email_addr = os.getenv(cfg["env_email"])
        auth_code = os.getenv(cfg["env_auth"])
        if email_addr and auth_code:
            accounts.append({
                "key": key,
                "email": email_addr,
                "auth_code": auth_code,
                "imap_host": cfg["imap_host"],
                "imap_port": cfg["imap_port"],
                "label": cfg["label"],
            })
    return accounts


def _get_credentials(account: str = None) -> Tuple[str, str, str, int]:
    """获取指定账户的凭据，默认返回第一个可用账户"""
    accounts = _get_all_accounts()
    if not accounts:
        print("[ERROR] 未配置任何邮箱账户，请在 ~/.hermes/.env 中设置")
        sys.exit(1)

    if account:
        for acc in accounts:
            if acc["key"] == account:
                return acc["email"], acc["auth_code"], acc["imap_host"], acc["imap_port"]
        print(f"[ERROR] 未找到账户 '{account}'")
        sys.exit(1)

    acc = accounts[0]
    return acc["email"], acc["auth_code"], acc["imap_host"], acc["imap_port"]


def _connect(account: str = None) -> imaplib.IMAP4_SSL:
    """建立 IMAP SSL 连接并登录"""
    email_addr, auth_code, imap_host, imap_port = _get_credentials(account)

    try:
        imap = imaplib.IMAP4_SSL(imap_host, imap_port)
        imap.login(email_addr, auth_code)
        return imap
    except imaplib.IMAP4.error as e:
        print(f"[ERROR] 登录失败 ({email_addr}): {e}")
        print("  请检查：1) 密码/授权码是否正确  2) IMAP 服务是否已开启")
        sys.exit(1)


def _decode_header_value(value: str) -> str:
    """解码邮件头字段（支持中文编码）"""
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                result.append(part.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _decode_email_body(msg: email.message.Message) -> str:
    """解码邮件正文（支持 HTML 和纯文本，自动处理编码）"""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        body = payload.decode("utf-8", errors="replace")
                    break
            elif content_type == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html = payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        html = payload.decode("utf-8", errors="replace")
                    # 简单去 HTML 标签
                    body = re.sub(r'<[^>]+>', ' ', html)
                    body = re.sub(r'\s+', ' ', body).strip()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = payload.decode("utf-8", errors="replace")

    return body


def _parse_email(msg_data) -> Dict:
    """解析单封邮件为结构化字典"""
    msg = email.message_from_bytes(msg_data)

    subject = _decode_header_value(msg.get("Subject", ""))
    sender = _decode_header_value(msg.get("From", ""))
    date_str = msg.get("Date", "")
    to = _decode_header_value(msg.get("To", ""))

    body = _decode_email_body(msg)

    # 解析日期
    try:
        date_parsed = email.utils.parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        date_parsed = None

    return {
        "subject": subject,
        "from": sender,
        "to": to,
        "date": date_parsed.isoformat() if date_parsed else date_str,
        "body": body[:5000],  # 限制长度
    }


# ── 日程提取 ─────────────────────────────────────────────────────────
# 中文数字映射
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12}

_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _parse_relative_date(text: str, ref_date: datetime = None) -> Optional[str]:
    """解析相对日期（明天、下周二、本周五等）"""
    if ref_date is None:
        ref_date = datetime.now()

    # 明天/后天/大后天
    if "今天" in text:
        return ref_date.strftime("%Y-%m-%d")
    if "明天" in text:
        return (ref_date + timedelta(days=1)).strftime("%Y-%m-%d")
    if "后天" in text:
        return (ref_date + timedelta(days=2)).strftime("%Y-%m-%d")
    if "大后天" in text:
        return (ref_date + timedelta(days=3)).strftime("%Y-%m-%d")

    # 下周X / 本周X
    m = re.search(r'(下|本)周([\u4e00-\u9fff])', text)
    if m:
        prefix, day_char = m.group(1), m.group(2)
        target_wd = _WEEKDAY_MAP.get(day_char)
        if target_wd is not None:
            today_wd = ref_date.weekday()
            if prefix == "本":
                delta = (target_wd - today_wd) % 7
            else:  # 下
                delta = (target_wd - today_wd) % 7 + 7
            return (ref_date + timedelta(days=delta)).strftime("%Y-%m-%d")

    return None


def _extract_schedule(text: str, email_info: Dict = None) -> List[Dict]:
    """从邮件正文提取日程信息"""
    events = []
    ref_date = datetime.now()

    # ── 日期模式 ──
    date_patterns = [
        # 2026年5月20日 / 2026年05月20日
        (r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]',
         lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 2026-05-20 / 2026/05/20
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
         lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 5月20日/号（无年份，取当前年）
        (r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]',
         lambda m: f"{ref_date.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
    ]

    # ── 时间模式 ──
    time_patterns = [
        # 上午/下午 9:00 / 9点30分
        (r'(上午|下午|晚上|中午)\s*(\d{1,2})\s*[：:点时]\s*(\d{1,2})?\s*[分]?',
         lambda m: _parse_cn_time(m.group(1), m.group(2), m.group(3))),
        # 14:00 / 14：00
        (r'(?<!\d)(\d{1,2})\s*[：:]\s*(\d{2})(?!\d)',
         lambda m: f"{int(m.group(1)):02d}:{m.group(2)}"),
        # 9点
        (r'(?<!\d)(\d{1,2})\s*点(?!\d)',
         lambda m: f"{int(m.group(1)):02d}:00"),
    ]

    # ── 会议关键词 ──
    meeting_keywords = [
        "会议", "面试", "笔试", "面谈", "洽谈", "研讨", "汇报",
        "培训", "讲座", "答辩", "评审", "讨论会", "电话会",
        "腾讯会议", "飞书会议", "Zoom", "zoom", "Teams", "teams",
        "线上", "线下", "视频会议", "语音会议",
    ]

    location_patterns = [
        r'(?:地点|地址|会[议室]|在哪里|会议室)[：:\s]*([^\n,，。]{2,30})',
        r'(?:腾讯会议|Zoom|飞书会议|Teams)[：:\s]*(https?://\S+)',
        r'(?:会议号|会议ID|Meeting ID)[：:\s]*(\d[\d\s]{5,})',
    ]

    # 提取所有日期
    found_dates = []
    for pattern, formatter in date_patterns:
        for m in re.finditer(pattern, text):
            try:
                date_str = formatter(m)
                # 验证日期合法性
                datetime.strptime(date_str, "%Y-%m-%d")
                found_dates.append(date_str)
            except (ValueError, TypeError):
                continue

    # 相对日期
    rel_date = _parse_relative_date(text, ref_date)
    if rel_date:
        found_dates.append(rel_date)

    # 去重
    found_dates = list(dict.fromkeys(found_dates))

    # 提取所有时间
    found_times = []
    for pattern, formatter in time_patterns:
        for m in re.finditer(pattern, text):
            try:
                time_str = formatter(m)
                if time_str and re.match(r'\d{2}:\d{2}', time_str):
                    found_times.append(time_str)
            except Exception:
                continue
    found_times = list(dict.fromkeys(found_times))

    # 检测会议关键词
    is_meeting = any(kw in text for kw in meeting_keywords)

    # 提取地点
    locations = []
    for pattern in location_patterns:
        for m in re.finditer(pattern, text):
            locations.append(m.group(1).strip())
    location = locations[0] if locations else ""

    # 提取会议链接
    link_match = re.search(r'https?://\S*(?:meeting|zoom|teams|feishu|lark|vc)\S*', text, re.IGNORECASE)
    if link_match and not location:
        location = link_match.group(0)

    # 优先级判断
    priority = "normal"
    high_keywords = ["紧急", "ASAP", "立即", "马上", "面试", "笔试", "答辩"]
    if any(kw in text for kw in high_keywords):
        priority = "high"
    elif is_meeting:
        priority = "medium"

    # ── 生成事件 ──
    # 从邮件主题提取标题
    title = email_info.get("subject", "") if email_info else ""
    if not title or title == "(无主题)":
        first_line = text.split('\n')[0][:50]
        title = first_line

    source_email = email_info.get("from", "") if email_info else ""
    email_date_str = email_info.get("date", "") if email_info else ""

    # 解析邮件收到时间
    received_dt = None
    if email_date_str:
        try:
            received_dt = datetime.fromisoformat(email_date_str)
        except (ValueError, TypeError):
            pass

    # ── 事件1: 通知日程（邮件收到时间）──
    if received_dt:
        events.append({
            "title": f"收到: {title[:60]}",
            "date": received_dt.strftime("%Y-%m-%d"),
            "time": received_dt.strftime("%H:%M"),
            "location": "",
            "source_email": source_email,
            "priority": priority,
            "is_meeting": is_meeting,
            "event_type": "notification",
        })

    # ── 事件2: 预约日程（邮件中的截止/面试/笔试时间）──
    if found_dates or is_meeting:
        for date_str in (found_dates or []):
            time_str = found_times[0] if found_times else ""
            events.append({
                "title": title[:80],
                "date": date_str,
                "time": time_str,
                "location": location,
                "source_email": source_email,
                "priority": priority,
                "is_meeting": is_meeting,
                "event_type": "appointment",
            })

    return events


def _parse_cn_time(period: str, hour: str, minute: str) -> str:
    """解析中文时间表述"""
    h = int(hour)
    m = int(minute) if minute else 0

    if period in ("下午", "晚上") and h < 12:
        h += 12
    elif period == "中午" and h == 12:
        h = 12

    return f"{h:02d}:{m:02d}"


# ── 邮件读取 ─────────────────────────────────────────────────────────
def read_emails(count: int = 10, unread_only: bool = False,
                account: str = None) -> List[Dict]:
    """读取最近 N 封邮件"""
    imap = _connect(account)

    try:
        imap.select("INBOX")

        if unread_only:
            status, msg_ids = imap.search(None, "UNSEEN")
        else:
            status, msg_ids = imap.search(None, "ALL")

        if status != "OK":
            print("[ERROR] 搜索邮件失败")
            return []

        id_list = msg_ids[0].split()
        if not id_list:
            print("[INFO] 没有邮件")
            return []

        # 取最新的 N 封
        recent_ids = id_list[-count:]
        recent_ids.reverse()  # 最新在前

        emails = []
        for msg_id in recent_ids:
            status, data = imap.fetch(msg_id, "(RFC822)")
            if status == "OK":
                parsed = _parse_email(data[0][1])
                parsed["msg_id"] = msg_id.decode()
                emails.append(parsed)

        return emails

    finally:
        imap.logout()


def search_emails(from_addr: str = None, subject: str = None,
                  days: int = None, count: int = 50,
                  account: str = None) -> List[Dict]:
    """搜索邮件"""
    imap = _connect(account)

    try:
        imap.select("INBOX")

        # 构建 IMAP 搜索条件
        criteria = []
        if from_addr:
            criteria.append(f'FROM "{from_addr}"')
        if subject:
            criteria.append(f'SUBJECT "{subject}"')
        if days:
            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            criteria.append(f'SINCE {since_date}')

        search_str = " ".join(criteria) if criteria else "ALL"
        status, msg_ids = imap.search(None, search_str)

        if status != "OK":
            print("[ERROR] 搜索失败")
            return []

        id_list = msg_ids[0].split()
        if not id_list:
            print("[INFO] 未找到匹配的邮件")
            return []

        recent_ids = id_list[-count:]
        recent_ids.reverse()

        emails = []
        for msg_id in recent_ids:
            status, data = imap.fetch(msg_id, "(RFC822)")
            if status == "OK":
                parsed = _parse_email(data[0][1])
                emails.append(parsed)

        return emails

    finally:
        imap.logout()


def extract_schedules(days: int = 7) -> Dict:
    """从最近 N 天邮件中提取日程"""
    emails = search_emails(days=days)

    all_events = []
    for em in emails:
        events = _extract_schedule(em["body"], em)
        if events:
            all_events.extend(events)

    # 按日期排序
    all_events.sort(key=lambda e: (e["date"], e.get("time", "")))

    return {
        "total_emails_scanned": len(emails),
        "events_found": len(all_events),
        "events": all_events,
    }


# ── 格式化输出 ───────────────────────────────────────────────────────
def format_email(em: Dict, index: int = 0) -> str:
    """格式化单封邮件为可读文本"""
    lines = []
    if index:
        lines.append(f"--- 邮件 {index} ---")
    lines.append(f"主题: {em['subject']}")
    lines.append(f"发件人: {em['from']}")
    lines.append(f"日期: {em['date']}")
    # 正文前 300 字
    body_preview = em['body'][:300].replace('\n', ' ').strip()
    if len(em['body']) > 300:
        body_preview += "..."
    lines.append(f"正文: {body_preview}")
    return "\n".join(lines)


def format_schedule(result: Dict) -> str:
    """格式化日程提取结果"""
    lines = []
    lines.append(f"扫描邮件: {result['total_emails_scanned']} 封")
    lines.append(f"发现日程: {result['events_found']} 个")
    lines.append("")

    if not result["events"]:
        lines.append("未发现日程信息。")
        return "\n".join(lines)

    for i, evt in enumerate(result["events"], 1):
        priority_icon = {"high": "🔴", "medium": "🟡", "normal": "⚪"}.get(evt["priority"], "⚪")
        lines.append(f"{priority_icon} [{i}] {evt['title']}")
        lines.append(f"   日期: {evt['date']}  {evt['time'] or '时间待定'}")
        if evt["location"]:
            lines.append(f"   地点: {evt['location']}")
        if evt["source_email"]:
            lines.append(f"   来源: {evt['source_email']}")
        lines.append("")

    return "\n".join(lines)


def search_all_accounts(from_addr: str = None, subject: str = None,
                        days: int = None, count: int = 50) -> Tuple[List[Dict], List[str]]:
    """搜索所有已配置的邮箱账户，返回 (合并邮件列表, 账户标签列表)"""
    all_accounts = _get_all_accounts()
    all_emails = []
    labels = []

    for acc in all_accounts:
        try:
            emails = search_emails(
                from_addr=from_addr, subject=subject,
                days=days, count=count, account=acc["key"]
            )
            for em in emails:
                em["account"] = acc["key"]
                em["account_label"] = acc["label"]
            all_emails.extend(emails)
            labels.append(f"{acc['label']}({acc['email']}): {len(emails)}封")
        except Exception as e:
            labels.append(f"{acc['label']}({acc['email']}): 失败({e})")

    return all_emails, labels


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="QQ 邮箱日程提取工具")
    sub = parser.add_subparsers(dest="command")

    # read
    read_p = sub.add_parser("read", help="读取最近邮件")
    read_p.add_argument("--count", type=int, default=10, help="邮件数量")

    # unread
    unread_p = sub.add_parser("unread", help="读取未读邮件")
    unread_p.add_argument("--count", type=int, default=20, help="最大数量")

    # schedule
    sched_p = sub.add_parser("schedule", help="提取日程信息")
    sched_p.add_argument("--days", type=int, default=7, help="扫描最近N天")

    # search
    search_p = sub.add_parser("search", help="搜索邮件")
    search_p.add_argument("--from", dest="from_addr", help="发件人地址")
    search_p.add_argument("--subject", help="主题关键词")
    search_p.add_argument("--days", type=int, default=30, help="搜索最近N天")
    search_p.add_argument("--count", type=int, default=20, help="最大结果数")

    args = parser.parse_args()

    if args.command == "read":
        emails = read_emails(count=args.count)
        for i, em in enumerate(emails, 1):
            print(format_email(em, i))
            print()

    elif args.command == "unread":
        emails = read_emails(count=args.count, unread_only=True)
        if not emails:
            print("没有未读邮件。")
        else:
            print(f"未读邮件: {len(emails)} 封\n")
            for i, em in enumerate(emails, 1):
                print(format_email(em, i))
                print()

    elif args.command == "schedule":
        result = extract_schedules(days=args.days)
        print(format_schedule(result))
        # 同时输出 JSON 供 Agent 使用
        json_path = os.path.expanduser("~/.hermes/tmp/schedules.json")
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[JSON 已保存到 {json_path}]")

    elif args.command == "search":
        emails = search_emails(
            from_addr=args.from_addr,
            subject=args.subject,
            days=args.days,
            count=args.count,
        )
        if not emails:
            print("未找到匹配邮件。")
        else:
            print(f"找到 {len(emails)} 封邮件:\n")
            for i, em in enumerate(emails, 1):
                print(format_email(em, i))
                print()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
