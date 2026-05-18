#!/usr/bin/env python3
"""飞书论文库 -> 本地 FAISS 索引 增量同步

从飞书知识库下载 PDF 文件，用 PyMuPDF 解析文本，构建/增量更新 FAISS 向量索引。

用法:
    python feishu_paper_sync.py sync --space-id 7640524641248791499
    python feishu_paper_sync.py status
    python feishu_paper_sync.py search "CAD-RADS 2.0 改进"
"""

import os
import sys
import json
import hashlib
import subprocess
import argparse
import re
import time
from datetime import datetime
from typing import List, Dict, Optional

import numpy as np
import faiss
import pickle
import fitz  # pymupdf

# 添加 local_rag.py 所在目录
RAG_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RAG_DIR)

from local_rag import (
    load_embedding_model, generate_embeddings,
    DEFAULT_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, MIN_CHUNK_LEN,
    HF_MIRROR, _clean_text, chunk_text,
)

# ── 配置 ─────────────────────────────────────────────────────────────
INDEX_DIR = os.path.join(RAG_DIR, "feishu_rag_index")
PDF_CACHE_DIR = os.path.join(RAG_DIR, "feishu_pdf_cache")
DEFAULT_SPACE_ID = "7640524641248791499"


def _lark_cli(cmd: str, timeout: int = 30) -> dict:
    """执行 lark-cli 命令并返回 JSON 结果"""
    full_cmd = f'cmd.exe /c "npx lark-cli {cmd} --as user"'
    try:
        result = subprocess.run(full_cmd, shell=True, capture_output=True, timeout=timeout)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        # 找到第一个 { 开始的完整 JSON 块
        json_start = stdout.find("{")
        if json_start == -1:
            return {"ok": False, "error": "no JSON in output", "raw": stdout[:200]}
        json_str = stdout[json_start:]
        # 可能有多个 JSON 对象（分页），取最后一个包含 "data" 的
        # 或者直接尝试解析第一个
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # 找到最后一个完整的 JSON 对象
            for line in reversed(json_str.strip().split("\n")):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
            return {"ok": False, "error": "JSON parse failed", "raw": json_str[:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 飞书文件下载 ─────────────────────────────────────────────────────
def _download_file(obj_token: str, save_path: str) -> bool:
    """从飞书云盘下载文件"""
    win_dir = os.path.dirname(save_path)
    win_file = os.path.basename(save_path)
    # 转换为 Windows 路径
    if win_dir.startswith("/mnt/"):
        parts = win_dir.split("/")
        if len(parts) >= 3:
            drive = parts[2].upper()
            rest = "\\".join(parts[3:])
            win_dir = f"{drive}:\\{rest}"

    # 用 bat 文件执行下载（GBK 编码，cmd.exe 默认读取方式）
    bat_lines = [
        '@echo off',
        f'cd /d {win_dir}',
        f'npx lark-cli drive +download --file-token {obj_token} --output "{win_file}" --overwrite --as user',
    ]
    bat_path = os.path.join(os.path.dirname(save_path), "_download.bat")
    with open(bat_path, "w", encoding="gbk", errors="replace", newline="\r\n") as f:
        f.write("\r\n".join(bat_lines) + "\r\n")

    win_bat = bat_path
    if bat_path.startswith("/mnt/"):
        parts = bat_path.split("/")
        if len(parts) >= 3:
            drive = parts[2].upper()
            rest = "\\".join(parts[3:])
            win_bat = f"{drive}:\\{rest}"

    try:
        result = subprocess.run(
            f'cmd.exe /c "{win_bat}"', shell=True,
            capture_output=True, timeout=120, cwd=os.path.dirname(save_path)
        )
        # 检查文件是否下载成功
        return os.path.isfile(save_path) and os.path.getsize(save_path) > 0
    except subprocess.TimeoutExpired:
        return False


def list_wiki_files(space_id: str) -> List[Dict]:
    """获取飞书知识库中所有 PDF 文件节点"""
    # --page-all 返回多个 JSON 对象，直接 grep 提取
    full_cmd = (
        f'cmd.exe /c "npx lark-cli api GET '
        f'/open-apis/wiki/v2/spaces/{space_id}/nodes --page-all --as user"'
    )
    try:
        result = subprocess.run(full_cmd, shell=True, capture_output=True, timeout=60)
        stdout = result.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return []

    # 用正则从原始输出中提取节点信息
    files = []
    # 匹配每个节点块中的 obj_type 和 title
    blocks = re.findall(
        r'"obj_type"\s*:\s*"file".*?"title"\s*:\s*"([^"]+\.pdf)"',
        stdout, re.DOTALL
    )
    # 同时提取 obj_token 和 node_token
    tokens = re.findall(
        r'"node_token"\s*:\s*"([^"]+)".*?"obj_token"\s*:\s*"([^"]+)".*?"obj_type"\s*:\s*"file".*?"title"\s*:\s*"([^"]+\.pdf)"',
        stdout, re.DOTALL
    )

    if tokens:
        for node_token, obj_token, title in tokens:
            files.append({
                "node_token": node_token,
                "obj_token": obj_token,
                "title": title,
            })
    else:
        # fallback: 只提取标题
        for title in blocks:
            files.append({"node_token": "", "obj_token": "", "title": title})

    return files


# ── PDF 文本提取 ─────────────────────────────────────────────────────
def extract_text_from_pdf(pdf_path: str) -> List[Dict]:
    """提取 PDF 每一页的文本"""
    pages = []
    try:
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            text = page.get_text("text")
            if text.strip():
                pages.append({"page": i + 1, "text": text})
        doc.close()
    except Exception as e:
        print(f"  [WARN] 无法解析: {e}")
    return pages


def _file_hash(path: str) -> str:
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            sha1.update(chunk)
    return sha1.hexdigest()


# ── 索引构建 ─────────────────────────────────────────────────────────
def build_index(space_id: str, model_name: str = DEFAULT_MODEL,
                chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
                force_rebuild: bool = False) -> str:
    """从飞书知识库构建/增量更新 FAISS 索引"""
    os.makedirs(INDEX_DIR, exist_ok=True)
    os.makedirs(PDF_CACHE_DIR, exist_ok=True)

    # 1. 获取飞书 PDF 列表
    print(f"[INFO] 获取飞书知识库 PDF 列表 (space_id={space_id})...")
    feishu_files = list_wiki_files(space_id)
    print(f"[INFO] 找到 {len(feishu_files)} 个 PDF 文件")

    if not feishu_files:
        print("[ERROR] 未找到 PDF 文件")
        return ""

    # 2. 加载 manifest
    manifest_path = os.path.join(INDEX_DIR, "manifest.json")
    if force_rebuild or not os.path.isfile(manifest_path):
        manifest = {"next_id": 0, "pdfs": {}, "space_id": space_id}
    else:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    old_pdfs = manifest.get("pdfs", {})

    # 3. Diff：找出新增文件
    new_files = [f for f in feishu_files if f["title"] not in old_pdfs]
    deleted = [t for t in old_pdfs if t not in {f["title"] for f in feishu_files}]

    # 检查 obj_token 变化（文件被替换）
    token_map = {f["title"]: f for f in feishu_files}
    modified = []
    for title, info in old_pdfs.items():
        if title in token_map:
            if info.get("obj_token") != token_map[title]["obj_token"]:
                modified.append(token_map[title])

    changed = new_files + modified

    print(f"  新增: {len(new_files)}")
    print(f"  修改: {len(modified)}")
    print(f"  删除: {len(deleted)}")
    print(f"  未变: {len(feishu_files) - len(changed)}")

    if not changed and not deleted:
        print("[OK] 索引已是最新")
        return INDEX_DIR

    # 4. 加载/初始化 FAISS
    model = load_embedding_model(model_name)
    dim = model.get_sentence_embedding_dimension()

    if force_rebuild or not os.path.isfile(os.path.join(INDEX_DIR, "index.faiss")):
        base = faiss.IndexFlatIP(dim)
        index = faiss.IndexIDMap(base)
        documents = {}
    else:
        index = faiss.read_index(os.path.join(INDEX_DIR, "index.faiss"))
        with open(os.path.join(INDEX_DIR, "documents.pkl"), "rb") as f:
            documents = pickle.load(f)

    # 5. 删除旧文件的向量
    ids_to_remove = []
    for title in deleted + [f["title"] for f in modified]:
        info = old_pdfs.get(title, {})
        ids_to_remove.extend(info.get("faiss_ids", []))
        for rid in info.get("faiss_ids", []):
            documents.pop(rid, None)
        manifest["pdfs"].pop(title, None)

    if ids_to_remove:
        index.remove_ids(np.array(ids_to_remove, dtype=np.int64))
        print(f"  [DEL] 移除 {len(ids_to_remove)} 个旧向量")

    # 6. 下载新文件 + 解析 + embedding
    next_id = manifest.get("next_id", 0)
    total_new = 0

    for file_info in changed:
        title = file_info["title"]
        obj_token = file_info["obj_token"]
        cache_path = os.path.join(PDF_CACHE_DIR, title)

        print(f"  [SYNC] {title[:60]}...", end=" ")

        # 下载文件（如果本地没有或已被替换）
        if not os.path.isfile(cache_path):
            ok = _download_file(obj_token, cache_path)
            if not ok:
                print("下载失败")
                continue
            time.sleep(1)  # 避免频率限制

        # 解析 PDF
        pages = extract_text_from_pdf(cache_path)
        if not pages:
            print("(无文本)")
            manifest["pdfs"][title] = {
                "obj_token": obj_token,
                "hash": "",
                "chunk_count": 0,
                "faiss_ids": [],
            }
            continue

        # 分块
        doc_chunks = []
        for page_info in pages:
            chunks = chunk_text(page_info["text"], chunk_size, overlap)
            for ci, chunk in enumerate(chunks):
                doc_chunks.append({
                    "text": chunk,
                    "source": title,
                    "page": page_info["page"],
                    "source_type": "feishu_wiki",
                })

        if not doc_chunks:
            print("(无有效文本块)")
            continue

        # embedding
        texts = [c["text"] for c in doc_chunks]
        embeddings = generate_embeddings(model, texts)

        # 分配 ID 并添加到 FAISS
        ids = list(range(next_id, next_id + len(doc_chunks)))
        index.add_with_ids(embeddings, np.array(ids, dtype=np.int64))

        for fid, chunk in zip(ids, doc_chunks):
            documents[fid] = chunk

        manifest["pdfs"][title] = {
            "obj_token": obj_token,
            "hash": _file_hash(cache_path),
            "chunk_count": len(doc_chunks),
            "faiss_ids": ids,
        }

        next_id += len(doc_chunks)
        total_new += len(doc_chunks)
        print(f"{len(doc_chunks)} 块")

    manifest["next_id"] = next_id

    # 7. 保存
    faiss.write_index(index, os.path.join(INDEX_DIR, "index.faiss"))
    with open(os.path.join(INDEX_DIR, "documents.pkl"), "wb") as f:
        pickle.dump(documents, f)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(INDEX_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "model_name": model_name,
            "dimension": dim,
            "source": "feishu_wiki",
            "space_id": space_id,
            "total_chunks": index.ntotal,
            "total_docs": len(manifest["pdfs"]),
            "built_at": datetime.now().isoformat(),
        }, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] 索引更新完成: {INDEX_DIR}")
    print(f"  总向量: {index.ntotal}")
    print(f"  本次新增: {total_new}")

    return INDEX_DIR


# ── 搜索 ─────────────────────────────────────────────────────────────
def search(query: str, top_k: int = 5) -> List[Dict]:
    """在飞书论文索引中搜索"""
    from local_rag import LocalRAGSearcher
    searcher = LocalRAGSearcher(INDEX_DIR)
    results = searcher.search(query, top_k)

    print(f"\n{'='*60}")
    print(f"查询: {query}")
    print(f"来源: 飞书论文库")
    print(f"{'='*60}")

    for i, r in enumerate(results, 1):
        print(f"\n--- 结果 {i} (相似度: {r['score']:.3f}) ---")
        print(f"来源: {r['source']} p.{r.get('page', '?')}")
        print(r['text'][:500])

    return results


def show_status():
    """显示索引状态"""
    manifest_path = os.path.join(INDEX_DIR, "manifest.json")
    if not os.path.isfile(manifest_path):
        print("[INFO] 索引不存在，请先运行 sync")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    meta_path = os.path.join(INDEX_DIR, "meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

    pdfs = manifest.get("pdfs", {})
    total_chunks = sum(d.get("chunk_count", 0) for d in pdfs.values())

    print(f"索引目录: {INDEX_DIR}")
    print(f"来源: 飞书知识库 (space_id={manifest.get('space_id', '?')})")
    print(f"模型: {meta.get('model_name', '?')}")
    print(f"PDF 数: {len(pdfs)}")
    print(f"文本块总数: {total_chunks}")
    print(f"FAISS 向量数: {meta.get('total_chunks', '?')}")
    print(f"上次更新: {meta.get('built_at', '?')}")


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="飞书论文库 RAG 索引同步")
    sub = parser.add_subparsers(dest="command")

    sync_p = sub.add_parser("sync", help="同步飞书 PDF 到本地索引")
    sync_p.add_argument("--space-id", default=DEFAULT_SPACE_ID)
    sync_p.add_argument("--model", default=DEFAULT_MODEL)
    sync_p.add_argument("--rebuild", action="store_true")

    sub.add_parser("status", help="查看索引状态")

    search_p = sub.add_parser("search", help="搜索论文")
    search_p.add_argument("query")
    search_p.add_argument("--top-k", type=int, default=5)

    args = parser.parse_args()

    if args.command == "sync":
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        build_index(args.space_id, args.model, force_rebuild=args.rebuild)
    elif args.command == "status":
        show_status()
    elif args.command == "search":
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        search(args.query, args.top_k)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
