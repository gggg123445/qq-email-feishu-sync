     1|---
     2|name: qq-email-scheduler
     3|description: "Connect to QQ email via IMAP, read recent emails, extract Chinese schedule/meeting info, and auto-sync to Feishu docs. Supports cron-based daily updates. Use when user wants to check QQ mail, extract meetings, or sync schedules to Feishu."
     4|version: 2.0.0
     5|author: Hermes Agent
     6|license: MIT
     7|metadata:
     8|  hermes:
     9|    tags: [email, qq, imap, calendar, schedule, 日程, 邮箱, feishu, 飞书, cron]
    10|    related_skills: [feishu-cli, feishu-integration]
    11|---
    12|
    13|# QQ 邮箱日程提取 + 飞书自动同步
    14|
    15|连接 QQ 邮箱 IMAP，读取邮件，从中提取中文会议/日程信息，支持定时同步到飞书文档。
    16|
    17|## 功能概览
    18|
    19|- 读取 QQ 邮箱邮件（最近 N 封 / 未读 / 按发件人/主题搜索）
    20|- 从中文邮件中自动提取日期、时间、地点、会议信息
    21|- 去重 + 过滤广告/垃圾邮件
    22|- 格式化为 Markdown 写入飞书文档
    23|- 支持 cron 定时任务每日自动更新
    24|
    25|## 前置条件
    26|
    27|### 1. QQ 邮箱授权码（不是 QQ 密码）
    28|
    29|1. 登录 https://mail.qq.com
    30|2. 设置 -> 账户 -> POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务
    31|3. 开启 IMAP/SMTP 服务
    32|4. 生成**授权码**（16 位字母）
    33|
    34|### 2. 环境变量
    35|
    36|在 `~/.hermes/.env` 中添加：
    37|
    38|```
    39|QQ_EMAIL=your_qq_number@qq.com
    40|QQ_AUTH_CODE=your_auth_code_here
    41|```
    42|
    43|### 3. 飞书配置（可选，用于自动同步）
    44|
    45|在 `~/.hermes/.env` 中添加：
    46|
    47|```
    48|FEISHU_DOC_TOKEN=your_feishu_doc_token
    49|FEISHU_WORK_DIR=/mnt/d/your/path
    50|```
    51|
    52|飞书文档 token 从 URL 获取：`https://www.feishu.cn/wiki/{node_token}` -> 用 lark-cli 获取 obj_token。
    53|
    54|### 4. 依赖
    55|
    56|```bash
    57|# Python 标准库即可（imaplib, email, re, json）
    58|# 飞书同步需要 @larksuite/cli
    59|npm install -g @larksuite/cli
    60|```
    61|
    62|## QQ 邮箱 IMAP 参数
    63|
    64|| 参数 | 值 |
    65||------|-----|
    66|| IMAP 服务器 | imap.qq.com |
    67|| IMAP 端口 | 993 (SSL/TLS) |
    68|| SMTP 服务器 | smtp.qq.com |
    69|| SMTP 端口 | 465 (SSL) |
    70|| 认证方式 | 邮箱地址 + 授权码 |
    71|
    72|## 使用方式
    73|
    74|### 命令行
    75|
    76|```bash
    77|SKILL_DIR=~/.hermes/skills/productivity/qq-email-scheduler
    78|
    79|# 读取最近 10 封邮件
    80|python $SKILL_DIR/scripts/qq_email.py read --count 10
    81|
    82|# 读取未读邮件
    83|python $SKILL_DIR/scripts/qq_email.py unread
    84|
    85|# 提取最近 7 天邮件中的日程信息
    86|python $SKILL_DIR/scripts/qq_email.py schedule --days 7
    87|
    88|# 按发件人搜索
    89|python $SKILL_DIR/scripts/qq_email.py search --from "boss@company.com"
    90|
    91|# 按主题搜索
    92|python $SKILL_DIR/scripts/qq_email.py search --subject "会议"
    93|```
    94|
    95|### 每日自动同步到飞书
    96|
    97|```bash
    98|# 运行每日同步脚本（提取近7天日程 -> 写入飞书文档）
    99|python ~/.hermes/scripts/qq_email_daily_schedule.py
   100|```
   101|
   102|### 设置 cron 定时任务
   103|
   104|在 Hermes Agent 中执行：
   105|```
   106|创建 cron 任务：每天 00:00 运行 qq_email_daily_schedule.py
   107|```
   108|
   109|Agent 会调用 `cronjob(action='create')` 创建定时任务。
   110|
   111|### Agent 对话触发
   112|
   113|直接说：
   114|- "看看我的 QQ 邮箱有什么新邮件"
   115|- "从最近的邮件里提取日程安排"
   116|- "把日程同步到飞书"
   117|- "有没有会议邀请邮件"
   118|
   119|## 技术原理
   120|
   121|### IMAP 连接流程
   122|
   123|```
   124|imaplib.IMAP4_SSL("imap.qq.com", 993)  -- 建立 SSL 连接
   125|imap.login(email, auth_code)            -- 授权码登录
   126|imap.select("INBOX")                    -- 选择收件箱
   127|imap.search(None, "ALL")                -- 搜索邮件 ID
   128|imap.fetch(msg_id, "(RFC822)")          -- 获取邮件内容
   129|email.message_from_bytes(data)          -- 解析邮件对象
   130|```
   131|
   132|### 中文邮件日程提取算法
   133|
   134|三级正则匹配 + 启发式规则：
   135|
   136|1. **日期提取**：
   137|   - 绝对日期：`2026年5月20日`、`2026-05-20`、`5月20日`
   138|   - 相对日期：`明天`、`后天`、`下周二`、`本周五`
   139|   - 映射：中文星期 -> weekday offset，`下`前缀 -> +7 天
   140|
   141|2. **时间提取**：
   142|   - 中文时间：`上午9:00`、`下午2:30`、`晚上8点`
   143|   - 24小时制：`14:00`、`14：00`（全角冒号兼容）
   144|   - 中文时段映射：下午/晚上 + 小时 < 12 -> +12
   145|
   146|3. **会议检测**：
   147|   - 关键词：会议/面试/笔试/腾讯会议/Zoom/飞书会议/Teams
   148|   - 地点提取：`地点：xxx`、`会议室：xxx`、会议链接 URL
   149|   - 优先级：面试/笔试=高，会议/讨论=中，其他=普通
   150|
   151|4. **后处理**：
   152|   - 去重：同标题+同日期+同时间只保留一条
   153|   - 过滤：发件人含 zhaopin/newsletter/eefocus/growthmail 的广告丢弃
   154|   - 分类：按日期排序，标注今天/明天/已过期
   155|
   156|### 飞书同步流程
   157|
   158|```
   159|1. IMAP 读取近 N 天邮件
   160|2. 正则提取日程 -> 去重 -> 过滤广告
   161|3. 格式化为 Markdown（待办 + 已过期分组）
   162|4. 写入临时 .md 文件
   163|5. cmd.exe /c "npx lark-cli docs +update --doc TOKEN --markdown @file --mode overwrite --as user"
   164|6. 返回更新结果
   165|```
   166|
   167|## 输出格式
   168|
   169|```json
   170|{
   171|  "total_emails_scanned": 17,
   172|  "events_found": 5,
   173|  "events": [
   174|    {
   175|      "title": "招商银行面试通知",
   176|      "date": "2026-05-20",
   177|      "time": "14:00",
   178|      "location": "",
   179|      "source_email": "95555@message.cmbchina.com",
   180|      "priority": "high",
   181|      "is_meeting": true
   182|    }
   183|  ]
   184|}
   185|```
   186|
   187|## Pitfalls
   188|
   189|1. **授权码不是 QQ 密码**：必须在 QQ 邮箱设置中生成专门的授权码
   190|2. **IMAP 服务需要手动开启**：QQ 邮箱默认关闭 IMAP
   191|3. **邮件编码**：中文邮件可能用 GB2312/GBK/UTF-8，脚本自动检测
   192|4. **Python email 子模块必须显式导入**：
   193|   ```python
   194|   import email
   195|   import email.message  # 必须！否则 AttributeError
   196|   import email.header
   197|   import email.utils
   198|   ```
   199|5. **WSL cmd.exe UNC 路径**：飞书更新必须 `cd /d D:\path` 而非 WSL 路径
   200|6. **地点字段误提取**：正则可能匹配正文中的无关文字，建议限定前 200 字
   201|7. **重复邮件**：同一事件可能收到多封不同发件人的通知，需去重
   202|8. **飞书 lark-cli 需要用户登录**：首次使用需 `npx lark-cli auth login --domain drive`
   203|