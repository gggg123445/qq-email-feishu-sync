#!/usr/bin/env python3
"""飞书论文助手 RAG 后端 (WebSocket 模式 — 不需要公网 IP)

机器人主动连接飞书服务器，无需内网穿透。

启动:
  python feishu_bot.py

前置:
  1. 飞书开放平台创建应用，开启机器人能力
  2. 事件与回调 -> 选择 "长连接" 模式（不是 webhook）
  3. 添加事件: im.message.receive_v1
  4. 发布应用
"""

import os
import sys
import json
import re
import logging
import argparse
import threading
from datetime import datetime
from typing import Dict, Optional

import requests

# 添加项目路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from agentic_rag.local_rag import LocalRAGSearcher, HF_MIRROR

# ── 配置 ─────────────────────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BOT_NAME = os.getenv("FEISHU_BOT_NAME", "论文助手")

# LLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.xiaomimimo.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("MIMO_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "mimo-v2.5-pro")

# RAG
INDEX_DIR = os.getenv("RAG_INDEX_DIR",
                      os.path.join(PROJECT_ROOT, "src", "agentic_rag", "feishu_rag_index"))
TOP_K = 5

# ── 日志 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_ROOT, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("feishu-bot")

# ── 全局状态 ─────────────────────────────────────────────────────────
_searcher: Optional[LocalRAGSearcher] = None
_tenant_token: Optional[str] = None
_token_expires: float = 0


def get_searcher() -> LocalRAGSearcher:
    global _searcher
    if _searcher is None:
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        log.info(f"加载 RAG 索引: {INDEX_DIR}")
        _searcher = LocalRAGSearcher(INDEX_DIR)
        log.info(f"索引加载完成: {_searcher.index.ntotal} 向量")
    return _searcher


def get_tenant_token() -> str:
    global _tenant_token, _token_expires
    if _tenant_token and datetime.now().timestamp() < _token_expires:
        return _tenant_token
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10
    )
    data = resp.json()
    _tenant_token = data.get("tenant_access_token", "")
    _token_expires = datetime.now().timestamp() + data.get("expire", 7200) - 300
    return _tenant_token


# ── RAG 搜索 + LLM 生成 ─────────────────────────────────────────────
def rag_answer(question: str) -> str:
    searcher = get_searcher()

    # 1. FAISS 搜索
    results = searcher.search(question, TOP_K)
    if not results:
        return "未在论文库中找到相关内容。请尝试换个关键词提问。"

    # 2. 构建上下文
    context_parts = []
    for i, r in enumerate(results, 1):
        context_parts.append(
            f"[论文{i}] {r['source']} (p.{r.get('page', '?')}, 相似度{r['score']:.2f})\n{r['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    # 3. LLM 生成
    prompt = f"""你是一个冠状动脉疾病（CAD-RADS）领域的学术研究助手。根据以下论文内容回答用户问题。

要求：
- 只基于提供的论文内容回答，不要编造
- 引用具体论文来源（作者+年份）
- 如果论文中没有相关信息，如实说明
- 使用中文回答

论文内容:
{context}

用户问题: {question}

回答:"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1500,
                "temperature": 0.3,
            },
            timeout=30,
        )
        data = resp.json()
        answer = data["choices"][0]["message"]["content"]
        return answer
    except Exception as e:
        log.error(f"LLM 失败: {e}")
        fallback = f"📚 搜索到 {len(results)} 篇相关论文（LLM 暂不可用）：\n\n"
        for i, r in enumerate(results, 1):
            fallback += f"**{i}. {r['source'][:50]}** (p.{r.get('page','?')}, {r['score']:.2f})\n"
            fallback += f"{r['text'][:250]}...\n\n"
        return fallback


# ── 飞书消息操作 ─────────────────────────────────────────────────────
def reply_message(message_id: str, text: str):
    token = get_tenant_token()
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"content": json.dumps({"text": text}), "msg_type": "text"},
        timeout=10,
    )
    log.info(f"回复 {message_id}: {resp.status_code}")


# ── 消息处理 ─────────────────────────────────────────────────────────
def handle_message(event):
    try:
        # 兼容 dict 和 lark-oapi 对象
        if hasattr(event, 'message'):
            message = event.message
        else:
            message = event.get("message", {})

        # 提取字段（兼容对象和字典）
        msg_type = getattr(message, "message_type", "") or (message.get("message_type", "") if isinstance(message, dict) else "")
        message_id = getattr(message, "message_id", "") or (message.get("message_id", "") if isinstance(message, dict) else "")
        content_str = getattr(message, "content", "") or (message.get("content", "") if isinstance(message, dict) else "")

        if msg_type != "text":
            reply_message(message_id, "目前只支持文本消息，请直接输入问题。")
            return

        content = json.loads(content_str)
        question = content.get("text", "").strip()

        # 去掉 @机器人 前缀
        question = re.sub(r'@_user_\d+\s*', '', question).strip()
        if not question:
            reply_message(message_id, "请输入问题，例如：\n- CAD-RADS 2.0 有什么改进？\n- 深度学习冠脉狭窄检测最新方法？")
            return

        log.info(f"问题: {question[:80]}")

        # RAG 回答
        answer = rag_answer(question)
        reply_text = f"{answer}\n\n---\n📚 来源: 飞书论文库 | 模型: {LLM_MODEL}"

        reply_message(message_id, reply_text)
        log.info(f"已回复: {answer[:60]}...")

    except Exception as e:
        log.error(f"处理消息异常: {e}", exc_info=True)


# ── WebSocket 长连接模式 ─────────────────────────────────────────────
def start_websocket():
    """使用 lark-oapi 的 WebSocket 客户端连接飞书"""
    import lark_oapi as lark
    from lark_oapi.ws.client import Client as WsClient

    def on_message(data):
        # lark-oapi P2ImMessageReceiveV1 对象
        event_data = getattr(data, 'event', data)
        threading.Thread(
            target=handle_message,
            args=(event_data,),
            daemon=True
        ).start()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    cli = WsClient(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    log.info("WebSocket 长连接启动，等待消息...")
    cli.start()


# ── 主入口 ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="飞书论文助手 (WebSocket 模式)")
    parser.add_argument("--webhook", action="store_true",
                        help="使用 webhook 模式（需要公网 IP）")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    # 检查配置
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        log.error("请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        log.error("  export FEISHU_APP_ID=cli_xxxx")
        log.error("  export FEISHU_APP_SECRET=xxxx")
        sys.exit(1)

    # 预加载索引
    try:
        get_searcher()
    except Exception as e:
        log.error(f"RAG 索引加载失败: {e}")
        log.info("请先运行: python src/agentic_rag/feishu_paper_sync.py sync --space-id <id>")
        sys.exit(1)

    if args.webhook:
        # Webhook 模式（需要公网 IP）
        from flask import Flask, request as flask_request, jsonify as flask_jsonify
        app = Flask(__name__)

        @app.route("/webhook", methods=["POST"])
        def webhook():
            data = flask_request.json
            if "challenge" in data:
                return flask_jsonify({"challenge": data["challenge"]})
            event = data.get("event", {})
            threading.Thread(target=handle_message, args=(event,), daemon=True).start()
            return flask_jsonify({"ok": True})

        @app.route("/health", methods=["GET"])
        def health():
            return flask_jsonify({"status": "ok", "vectors": get_searcher().index.ntotal})

        log.info(f"Webhook 模式: http://0.0.0.0:{args.port}/webhook")
        app.run(host="0.0.0.0", port=args.port)
    else:
        # WebSocket 模式（不需要公网 IP）
        start_websocket()


if __name__ == "__main__":
    main()
