     1|#!/usr/bin/env python3
     2|"""每日自动提取 QQ 邮箱日程并同步到飞书文档
     3|
     4|配置方式（三选一）：
     5|  1. 环境变量：~/.hermes/.env 中设置 FEISHU_DOC_TOKEN, FEISHU_WORK_DIR
     6|  2. 命令行参数：--doc-token, --work-dir
     7|  3. 直接修改下方默认值
     8|"""
     9|
    10|import os
    11|import sys
    12|import subprocess
    13|import json
    14|import argparse
    15|from datetime import datetime, timedelta
    16|
    17|# ── 默认配置（可通过环境变量或命令行覆盖）──
    18|DEFAULT_DOC_TOKEN = os.getenv("FEISHU_DOC_TOKEN", "")
    19|DEFAULT_WORK_DIR = os.getenv("FEISHU_WORK_DIR", "")
    20|SCAN_DAYS = 7
    21|
    22|# 添加 skill 脚本路径
    23|SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    24|sys.path.insert(0, SKILL_DIR)
    25|
    26|
    27|def extract_schedules(days: int = SCAN_DAYS):
    28|    """提取近 N 天邮件中的日程"""
    29|    from scripts.qq_email import search_emails, _extract_schedule
    30|
    31|    emails = search_emails(days=days)
    32|
    33|    all_events = []
    34|    for em in emails:
    35|        events = _extract_schedule(em["body"], em)
    36|        if events:
    37|            all_events.extend(events)
    38|
    39|    # 去重（同标题+同日期+同时间只保留一个）
    40|    seen = set()
    41|    unique = []
    42|    for evt in all_events:
    43|        key = (evt["title"], evt["date"], evt.get("time", ""))
    44|        if key not in seen:
    45|            seen.add(key)
    46|            unique.append(evt)
    47|
    48|    # 过滤广告和垃圾
    49|    spam_keywords = [
    50|        "职位推荐", "征文邀请", "有奖调研", "积分", "推广",
    51|        "订阅", "newsletter", "unsubscribe", "退订",
    52|    ]
    53|    spam_senders = [
    54|        "zhaopin", "newsletter", "eefocus", "growthmail",
    55|        "growth-mail", "info.eefocus", "cnkicfp",
    56|    ]
    57|    filtered = [
    58|        e for e in unique
    59|        if not any(kw in e["title"].lower() for kw in spam_keywords)
    60|        and not any(kw in (e.get("source_email", "").lower()) for kw in spam_senders)
    61|    ]
    62|
    63|    # 按日期排序
    64|    filtered.sort(key=lambda e: (e["date"], e.get("time", "")))
    65|
    66|    return len(emails), filtered
    67|
    68|
    69|def format_markdown(total_emails: int, events: list) -> str:
    70|    """格式化为飞书 Markdown"""
    71|    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    72|    today = datetime.now().strftime("%Y-%m-%d")
    73|    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    74|
    75|    lines = [
    76|        "# QQ邮箱日程提取 - 近7日",
    77|        "",
    78|        f"自动更新时间: {now}",
    79|        f"扫描范围: 最近7天邮件（{total_emails}封）",
    80|        f"有效日程: {len(events)}项",
    81|        "",
    82|        "---",
    83|        "",
    84|    ]
    85|
    86|    upcoming = [e for e in events if e["date"] >= today]
    87|    past = [e for e in events if e["date"] < today]
    88|
    89|    if upcoming:
    90|        lines.append("## 待办日程")
    91|        lines.append("")
    92|        for i, evt in enumerate(upcoming, 1):
    93|            priority_icon = {"high": "!!", "medium": "!", "normal": ""}.get(evt["priority"], "")
    94|            prefix = f"[{priority_icon}] " if priority_icon else ""
    95|
    96|            date_display = evt["date"]
    97|            if date_display == today:
    98|                date_display += "（今天）"
    99|            elif date_display == tomorrow:
   100|                date_display += "（明天）"
   101|
   102|            lines.append(f"### {i}. {prefix}{evt['title']}")
   103|            lines.append(f"- 日期: {date_display}")
   104|            if evt.get("time"):
   105|                lines.append(f"- 时间: {evt['time']}")
   106|            if evt.get("location"):
   107|                lines.append(f"- 地点: {evt['location']}")
   108|            if evt.get("source_email"):
   109|                lines.append(f"- 来源: {evt['source_email']}")
   110|            lines.append("")
   111|
   112|    if past:
   113|        lines.append("## 已过期（留档参考）")
   114|        lines.append("")
   115|        for evt in past:
   116|            lines.append(f"- {evt['date']} {evt.get('time', '')} {evt['title']}")
   117|        lines.append("")
   118|
   119|    if not events:
   120|        lines.append("近7天未发现有效日程。")
   121|        lines.append("")
   122|
   123|    return "\n".join(lines)
   124|
   125|
   126|def update_feishu(md_content: str, doc_token: str, work_dir: str) -> tuple:
   127|    """通过 lark-cli 更新飞书文档"""
   128|    md_path = os.path.join(work_dir, "qq_schedule_7days.md")
   129|    with open(md_path, "w", encoding="utf-8") as f:
   130|        f.write(md_content)
   131|
   132|    # WSL 环境：将 /mnt/d/... 转换为 D:\...
   133|    win_dir = work_dir
   134|    if work_dir.startswith("/mnt/"):
   135|        parts = work_dir.split("/")
   136|        if len(parts) >= 3:
   137|            drive = parts[2].upper()
   138|            rest = "\\".join(parts[3:])
   139|            win_dir = f"{drive}:\\{rest}"
   140|
   141|    cmd = f'cmd.exe /c "cd /d {win_dir} && npx lark-cli docs +update --doc {doc_token} --markdown @qq_schedule_7days.md --mode overwrite --as user"'
   142|
   143|    try:
   144|        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
   145|        stdout = result.stdout.decode("utf-8", errors="replace")
   146|        stderr = result.stderr.decode("utf-8", errors="replace")
   147|    except subprocess.TimeoutExpired:
   148|        return False, "lark-cli timeout (30s)"
   149|    except FileNotFoundError:
   150|        # 非 WSL 环境，直接用 bash
   151|        cmd_bash = f"cd {work_dir} && npx lark-cli docs +update --doc {doc_token} --markdown @qq_schedule_7days.md --mode overwrite --as user"
   152|        result = subprocess.run(cmd_bash, shell=True, capture_output=True, timeout=30)
   153|        stdout = result.stdout.decode("utf-8", errors="replace")
   154|        stderr = result.stderr.decode("utf-8", errors="replace")
   155|
   156|    if result.returncode != 0:
   157|        return False, stderr
   158|
   159|    try:
   160|        resp = json.loads(stdout.strip().split("\n")[-1] if stdout else "{}")
   161|        return resp.get("ok", False), stdout
   162|    except (json.JSONDecodeError, IndexError):
   163|        return True, stdout
   164|
   165|
   166|def main():
   167|    parser = argparse.ArgumentParser(description="QQ邮箱日程 -> 飞书文档 每日同步")
   168|    parser.add_argument("--doc-token", default=DEFAULT_DOC_TOKEN,
   169|                        help="飞书文档 obj_token")
   170|    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR,
   171|                        help="工作目录（存放临时 .md 文件）")
   172|    parser.add_argument("--days", type=int, default=SCAN_DAYS,
   173|                        help="扫描最近 N 天邮件")
   174|    parser.add_argument("--dry-run", action="store_true",
   175|                        help="只提取日程，不同步飞书")
   176|    args = parser.parse_args()
   177|
   178|    # 1. 提取日程
   179|    total_emails, events = extract_schedules(days=args.days)
   180|
   181|    # 2. 格式化
   182|    md = format_markdown(total_emails, events)
   183|
   184|    # 3. 输出
   185|    if args.dry_run or not args.doc_token or not args.work_dir:
   186|        print(md)
   187|        if not args.doc_token:
   188|            print("\n[提示] 未配置 FEISHU_DOC_TOKEN，仅输出本地结果")
   189|        return 0
   190|
   191|    # 4. 同步飞书
   192|    ok, detail = update_feishu(md, args.doc_token, args.work_dir)
   193|
   194|    if ok:
   195|        print(f"[OK] 日程已更新到飞书")
   196|        print(f"  邮件扫描: {total_emails} 封")
   197|        print(f"  有效日程: {len(events)} 个")
   198|        print()
   199|        print(md)
   200|    else:
   201|        print(f"[ERROR] 飞书更新失败: {detail}")
   202|        print()
   203|        print("日程内容（本地备份）:")
   204|        print(md)
   205|
   206|    return 0 if ok else 1
   207|
   208|
   209|if __name__ == "__main__":
   210|    sys.exit(main())
   211|