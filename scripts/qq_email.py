     1|#!/usr/bin/env python3
     2|"""QQ 邮箱日程提取工具
     3|
     4|连接 QQ 邮箱 IMAP，读取邮件，提取中文日程/会议信息。
     5|
     6|用法:
     7|    python qq_email.py read [--count N]
     8|    python qq_email.py unread
     9|    python qq_email.py schedule [--days N]
    10|    python qq_email.py search --from ADDR
    11|    python qq_email.py search --subject KEYWORD
    12|"""
    13|
    14|import os
    15|import sys
    16|import re
    17|import json
    18|import imaplib
    19|import email
    20|import email.message
    21|import email.header
    22|import email.utils
    23|from email.header import decode_header
    24|from datetime import datetime, timedelta
    25|from typing import List, Dict, Optional, Tuple
    26|import argparse
    27|
    28|# ── 配置 ─────────────────────────────────────────────────────────────
    29|IMAP_HOST = "imap.qq.com"
    30|IMAP_PORT = 993
    31|SMTP_HOST = "smtp.qq.com"
    32|SMTP_PORT = 465
    33|
    34|
    35|def _get_credentials() -> Tuple[str, str]:
    36|    """从环境变量获取 QQ 邮箱凭据"""
    37|    from dotenv import load_dotenv
    38|    load_dotenv(os.path.expanduser("~/.hermes/.env"))
    39|
    40|    email_addr = os.getenv("QQ_EMAIL")
    41|    auth_code = os.getenv("QQ_AUTH_CODE")
    42|
    43|    if not email_addr or not auth_code:
    44|        print("[ERROR] 请在 ~/.hermes/.env 中设置 QQ_EMAIL 和 QQ_AUTH_CODE")
    45|        print("  QQ_EMAIL=your_qq@qq.com")
    46|        print("  QQ_AUTH_CODE=你的授权码（不是QQ密码）")
    47|        sys.exit(1)
    48|
    49|    return email_addr, auth_code
    50|
    51|
    52|def _connect() -> imaplib.IMAP4_SSL:
    53|    """建立 IMAP SSL 连接并登录"""
    54|    email_addr, auth_code = _get_credentials()
    55|
    56|    try:
    57|        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    58|        imap.login(email_addr, auth_code)
    59|        return imap
    60|    except imaplib.IMAP4.error as e:
    61|        print(f"[ERROR] 登录失败: {e}")
    62|        print("  请检查：1) 授权码是否正确  2) IMAP 服务是否已开启")
    63|        sys.exit(1)
    64|
    65|
    66|def _decode_header_value(value: str) -> str:
    67|    """解码邮件头字段（支持中文编码）"""
    68|    if not value:
    69|        return ""
    70|    decoded_parts = decode_header(value)
    71|    result = []
    72|    for part, charset in decoded_parts:
    73|        if isinstance(part, bytes):
    74|            charset = charset or "utf-8"
    75|            try:
    76|                result.append(part.decode(charset, errors="replace"))
    77|            except (LookupError, UnicodeDecodeError):
    78|                result.append(part.decode("utf-8", errors="replace"))
    79|        else:
    80|            result.append(str(part))
    81|    return "".join(result)
    82|
    83|
    84|def _decode_email_body(msg: email.message.Message) -> str:
    85|    """解码邮件正文（支持 HTML 和纯文本，自动处理编码）"""
    86|    body = ""
    87|
    88|    if msg.is_multipart():
    89|        for part in msg.walk():
    90|            content_type = part.get_content_type()
    91|            if content_type == "text/plain":
    92|                payload = part.get_payload(decode=True)
    93|                if payload:
    94|                    charset = part.get_content_charset() or "utf-8"
    95|                    try:
    96|                        body = payload.decode(charset, errors="replace")
    97|                    except (LookupError, UnicodeDecodeError):
    98|                        body = payload.decode("utf-8", errors="replace")
    99|                    break
   100|            elif content_type == "text/html" and not body:
   101|                payload = part.get_payload(decode=True)
   102|                if payload:
   103|                    charset = part.get_content_charset() or "utf-8"
   104|                    try:
   105|                        html = payload.decode(charset, errors="replace")
   106|                    except (LookupError, UnicodeDecodeError):
   107|                        html = payload.decode("utf-8", errors="replace")
   108|                    # 简单去 HTML 标签
   109|                    body = re.sub(r'<[^>]+>', ' ', html)
   110|                    body = re.sub(r'\s+', ' ', body).strip()
   111|    else:
   112|        payload = msg.get_payload(decode=True)
   113|        if payload:
   114|            charset = msg.get_content_charset() or "utf-8"
   115|            try:
   116|                body = payload.decode(charset, errors="replace")
   117|            except (LookupError, UnicodeDecodeError):
   118|                body = payload.decode("utf-8", errors="replace")
   119|
   120|    return body
   121|
   122|
   123|def _parse_email(msg_data) -> Dict:
   124|    """解析单封邮件为结构化字典"""
   125|    msg = email.message_from_bytes(msg_data)
   126|
   127|    subject = _decode_header_value(msg.get("Subject", ""))
   128|    sender = _decode_header_value(msg.get("From", ""))
   129|    date_str = msg.get("Date", "")
   130|    to = _decode_header_value(msg.get("To", ""))
   131|
   132|    body = _decode_email_body(msg)
   133|
   134|    # 解析日期
   135|    try:
   136|        date_parsed = email.utils.parsedate_to_datetime(date_str)
   137|    except (ValueError, TypeError):
   138|        date_parsed = None
   139|
   140|    return {
   141|        "subject": subject,
   142|        "from": sender,
   143|        "to": to,
   144|        "date": date_parsed.isoformat() if date_parsed else date_str,
   145|        "body": body[:5000],  # 限制长度
   146|    }
   147|
   148|
   149|# ── 日程提取 ─────────────────────────────────────────────────────────
   150|# 中文数字映射
   151|_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
   152|           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
   153|           "十一": 11, "十二": 12}
   154|
   155|_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
   156|
   157|
   158|def _parse_relative_date(text: str, ref_date: datetime = None) -> Optional[str]:
   159|    """解析相对日期（明天、下周二、本周五等）"""
   160|    if ref_date is None:
   161|        ref_date = datetime.now()
   162|
   163|    # 明天/后天/大后天
   164|    if "今天" in text:
   165|        return ref_date.strftime("%Y-%m-%d")
   166|    if "明天" in text:
   167|        return (ref_date + timedelta(days=1)).strftime("%Y-%m-%d")
   168|    if "后天" in text:
   169|        return (ref_date + timedelta(days=2)).strftime("%Y-%m-%d")
   170|    if "大后天" in text:
   171|        return (ref_date + timedelta(days=3)).strftime("%Y-%m-%d")
   172|
   173|    # 下周X / 本周X
   174|    m = re.search(r'(下|本)周([\u4e00-\u9fff])', text)
   175|    if m:
   176|        prefix, day_char = m.group(1), m.group(2)
   177|        target_wd = _WEEKDAY_MAP.get(day_char)
   178|        if target_wd is not None:
   179|            today_wd = ref_date.weekday()
   180|            if prefix == "本":
   181|                delta = (target_wd - today_wd) % 7
   182|            else:  # 下
   183|                delta = (target_wd - today_wd) % 7 + 7
   184|            return (ref_date + timedelta(days=delta)).strftime("%Y-%m-%d")
   185|
   186|    return None
   187|
   188|
   189|def _extract_schedule(text: str, email_info: Dict = None) -> List[Dict]:
   190|    """从邮件正文提取日程信息"""
   191|    events = []
   192|    ref_date = datetime.now()
   193|
   194|    # ── 日期模式 ──
   195|    date_patterns = [
   196|        # 2026年5月20日 / 2026年05月20日
   197|        (r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]',
   198|         lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
   199|        # 2026-05-20 / 2026/05/20
   200|        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
   201|         lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
   202|        # 5月20日/号（无年份，取当前年）
   203|        (r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]',
   204|         lambda m: f"{ref_date.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"),
   205|    ]
   206|
   207|    # ── 时间模式 ──
   208|    time_patterns = [
   209|        # 上午/下午 9:00 / 9点30分
   210|        (r'(上午|下午|晚上|中午)\s*(\d{1,2})\s*[：:点时]\s*(\d{1,2})?\s*[分]?',
   211|         lambda m: _parse_cn_time(m.group(1), m.group(2), m.group(3))),
   212|        # 14:00 / 14：00
   213|        (r'(?<!\d)(\d{1,2})\s*[：:]\s*(\d{2})(?!\d)',
   214|         lambda m: f"{int(m.group(1)):02d}:{m.group(2)}"),
   215|        # 9点
   216|        (r'(?<!\d)(\d{1,2})\s*点(?!\d)',
   217|         lambda m: f"{int(m.group(1)):02d}:00"),
   218|    ]
   219|
   220|    # ── 会议关键词 ──
   221|    meeting_keywords = [
   222|        "会议", "面试", "笔试", "面谈", "洽谈", "研讨", "汇报",
   223|        "培训", "讲座", "答辩", "评审", "讨论会", "电话会",
   224|        "腾讯会议", "飞书会议", "Zoom", "zoom", "Teams", "teams",
   225|        "线上", "线下", "视频会议", "语音会议",
   226|    ]
   227|
   228|    location_patterns = [
   229|        r'(?:地点|地址|会[议室]|在哪里|会议室)[：:\s]*([^\n,，。]{2,30})',
   230|        r'(?:腾讯会议|Zoom|飞书会议|Teams)[：:\s]*(https?://\S+)',
   231|        r'(?:会议号|会议ID|Meeting ID)[：:\s]*(\d[\d\s]{5,})',
   232|    ]
   233|
   234|    # 提取所有日期
   235|    found_dates = []
   236|    for pattern, formatter in date_patterns:
   237|        for m in re.finditer(pattern, text):
   238|            try:
   239|                date_str = formatter(m)
   240|                # 验证日期合法性
   241|                datetime.strptime(date_str, "%Y-%m-%d")
   242|                found_dates.append(date_str)
   243|            except (ValueError, TypeError):
   244|                continue
   245|
   246|    # 相对日期
   247|    rel_date = _parse_relative_date(text, ref_date)
   248|    if rel_date:
   249|        found_dates.append(rel_date)
   250|
   251|    # 去重
   252|    found_dates = list(dict.fromkeys(found_dates))
   253|
   254|    # 提取所有时间
   255|    found_times = []
   256|    for pattern, formatter in time_patterns:
   257|        for m in re.finditer(pattern, text):
   258|            try:
   259|                time_str = formatter(m)
   260|                if time_str and re.match(r'\d{2}:\d{2}', time_str):
   261|                    found_times.append(time_str)
   262|            except Exception:
   263|                continue
   264|    found_times = list(dict.fromkeys(found_times))
   265|
   266|    # 检测会议关键词
   267|    is_meeting = any(kw in text for kw in meeting_keywords)
   268|
   269|    # 提取地点
   270|    locations = []
   271|    for pattern in location_patterns:
   272|        for m in re.finditer(pattern, text):
   273|            locations.append(m.group(1).strip())
   274|    location = locations[0] if locations else ""
   275|
   276|    # 提取会议链接
   277|    link_match = re.search(r'https?://\S*(?:meeting|zoom|teams|feishu|lark|vc)\S*', text, re.IGNORECASE)
   278|    if link_match and not location:
   279|        location = link_match.group(0)
   280|
   281|    # 优先级判断
   282|    priority = "normal"
   283|    high_keywords = ["紧急", "ASAP", "立即", "马上", "面试", "笔试", "答辩"]
   284|    if any(kw in text for kw in high_keywords):
   285|        priority = "high"
   286|    elif is_meeting:
   287|        priority = "medium"
   288|
   289|    # 生成事件
   290|    if found_dates or is_meeting:
   291|        for date_str in (found_dates or [ref_date.strftime("%Y-%m-%d")]):
   292|            time_str = found_times[0] if found_times else ""
   293|
   294|            # 从主题或正文提取标题
   295|            title = email_info.get("subject", "") if email_info else ""
   296|            if not title or title == "(无主题)":
   297|                # 从正文前 50 字提取
   298|                first_line = text.split('\n')[0][:50]
   299|                title = first_line
   300|
   301|            events.append({
   302|                "title": title,
   303|                "date": date_str,
   304|                "time": time_str,
   305|                "location": location,
   306|                "source_email": email_info.get("from", "") if email_info else "",
   307|                "priority": priority,
   308|                "is_meeting": is_meeting,
   309|            })
   310|
   311|    return events
   312|
   313|
   314|def _parse_cn_time(period: str, hour: str, minute: str) -> str:
   315|    """解析中文时间表述"""
   316|    h = int(hour)
   317|    m = int(minute) if minute else 0
   318|
   319|    if period in ("下午", "晚上") and h < 12:
   320|        h += 12
   321|    elif period == "中午" and h == 12:
   322|        h = 12
   323|
   324|    return f"{h:02d}:{m:02d}"
   325|
   326|
   327|# ── 邮件读取 ─────────────────────────────────────────────────────────
   328|def read_emails(count: int = 10, unread_only: bool = False) -> List[Dict]:
   329|    """读取最近 N 封邮件"""
   330|    imap = _connect()
   331|
   332|    try:
   333|        imap.select("INBOX")
   334|
   335|        if unread_only:
   336|            status, msg_ids = imap.search(None, "UNSEEN")
   337|        else:
   338|            status, msg_ids = imap.search(None, "ALL")
   339|
   340|        if status != "OK":
   341|            print("[ERROR] 搜索邮件失败")
   342|            return []
   343|
   344|        id_list = msg_ids[0].split()
   345|        if not id_list:
   346|            print("[INFO] 没有邮件")
   347|            return []
   348|
   349|        # 取最新的 N 封
   350|        recent_ids = id_list[-count:]
   351|        recent_ids.reverse()  # 最新在前
   352|
   353|        emails = []
   354|        for msg_id in recent_ids:
   355|            status, data = imap.fetch(msg_id, "(RFC822)")
   356|            if status == "OK":
   357|                parsed = _parse_email(data[0][1])
   358|                parsed["msg_id"] = msg_id.decode()
   359|                emails.append(parsed)
   360|
   361|        return emails
   362|
   363|    finally:
   364|        imap.logout()
   365|
   366|
   367|def search_emails(from_addr: str = None, subject: str = None,
   368|                  days: int = None, count: int = 50) -> List[Dict]:
   369|    """搜索邮件"""
   370|    imap = _connect()
   371|
   372|    try:
   373|        imap.select("INBOX")
   374|
   375|        # 构建 IMAP 搜索条件
   376|        criteria = []
   377|        if from_addr:
   378|            criteria.append(f'FROM "{from_addr}"')
   379|        if subject:
   380|            criteria.append(f'SUBJECT "{subject}"')
   381|        if days:
   382|            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
   383|            criteria.append(f'SINCE {since_date}')
   384|
   385|        search_str = " ".join(criteria) if criteria else "ALL"
   386|        status, msg_ids = imap.search(None, search_str)
   387|
   388|        if status != "OK":
   389|            print("[ERROR] 搜索失败")
   390|            return []
   391|
   392|        id_list = msg_ids[0].split()
   393|        if not id_list:
   394|            print("[INFO] 未找到匹配的邮件")
   395|            return []
   396|
   397|        recent_ids = id_list[-count:]
   398|        recent_ids.reverse()
   399|
   400|        emails = []
   401|        for msg_id in recent_ids:
   402|            status, data = imap.fetch(msg_id, "(RFC822)")
   403|            if status == "OK":
   404|                parsed = _parse_email(data[0][1])
   405|                emails.append(parsed)
   406|
   407|        return emails
   408|
   409|    finally:
   410|        imap.logout()
   411|
   412|
   413|def extract_schedules(days: int = 7) -> Dict:
   414|    """从最近 N 天邮件中提取日程"""
   415|    emails = search_emails(days=days)
   416|
   417|    all_events = []
   418|    for em in emails:
   419|        events = _extract_schedule(em["body"], em)
   420|        if events:
   421|            all_events.extend(events)
   422|
   423|    # 按日期排序
   424|    all_events.sort(key=lambda e: (e["date"], e.get("time", "")))
   425|
   426|    return {
   427|        "total_emails_scanned": len(emails),
   428|        "events_found": len(all_events),
   429|        "events": all_events,
   430|    }
   431|
   432|
   433|# ── 格式化输出 ───────────────────────────────────────────────────────
   434|def format_email(em: Dict, index: int = 0) -> str:
   435|    """格式化单封邮件为可读文本"""
   436|    lines = []
   437|    if index:
   438|        lines.append(f"--- 邮件 {index} ---")
   439|    lines.append(f"主题: {em['subject']}")
   440|    lines.append(f"发件人: {em['from']}")
   441|    lines.append(f"日期: {em['date']}")
   442|    # 正文前 300 字
   443|    body_preview = em['body'][:300].replace('\n', ' ').strip()
   444|    if len(em['body']) > 300:
   445|        body_preview += "..."
   446|    lines.append(f"正文: {body_preview}")
   447|    return "\n".join(lines)
   448|
   449|
   450|def format_schedule(result: Dict) -> str:
   451|    """格式化日程提取结果"""
   452|    lines = []
   453|    lines.append(f"扫描邮件: {result['total_emails_scanned']} 封")
   454|    lines.append(f"发现日程: {result['events_found']} 个")
   455|    lines.append("")
   456|
   457|    if not result["events"]:
   458|        lines.append("未发现日程信息。")
   459|        return "\n".join(lines)
   460|
   461|    for i, evt in enumerate(result["events"], 1):
   462|        priority_icon = {"high": "🔴", "medium": "🟡", "normal": "⚪"}.get(evt["priority"], "⚪")
   463|        lines.append(f"{priority_icon} [{i}] {evt['title']}")
   464|        lines.append(f"   日期: {evt['date']}  {evt['time'] or '时间待定'}")
   465|        if evt["location"]:
   466|            lines.append(f"   地点: {evt['location']}")
   467|        if evt["source_email"]:
   468|            lines.append(f"   来源: {evt['source_email']}")
   469|        lines.append("")
   470|
   471|    return "\n".join(lines)
   472|
   473|
   474|# ── CLI ──────────────────────────────────────────────────────────────
   475|def main():
   476|    parser = argparse.ArgumentParser(description="QQ 邮箱日程提取工具")
   477|    sub = parser.add_subparsers(dest="command")
   478|
   479|    # read
   480|    read_p = sub.add_parser("read", help="读取最近邮件")
   481|    read_p.add_argument("--count", type=int, default=10, help="邮件数量")
   482|
   483|    # unread
   484|    unread_p = sub.add_parser("unread", help="读取未读邮件")
   485|    unread_p.add_argument("--count", type=int, default=20, help="最大数量")
   486|
   487|    # schedule
   488|    sched_p = sub.add_parser("schedule", help="提取日程信息")
   489|    sched_p.add_argument("--days", type=int, default=7, help="扫描最近N天")
   490|
   491|    # search
   492|    search_p = sub.add_parser("search", help="搜索邮件")
   493|    search_p.add_argument("--from", dest="from_addr", help="发件人地址")
   494|    search_p.add_argument("--subject", help="主题关键词")
   495|    search_p.add_argument("--days", type=int, default=30, help="搜索最近N天")
   496|    search_p.add_argument("--count", type=int, default=20, help="最大结果数")
   497|
   498|    args = parser.parse_args()
   499|
   500|    if args.command == "read":
   501|