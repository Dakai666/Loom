#!/usr/bin/env python3
"""
opencli_fetch.py — OpenCLI adapter 統一封裝

職責：將 opencli 各 news adapter 的輸出標準化為與 fetch_news.py 相容的 JSON 格式。
所有 adapter 都是直接 subprocess 呼叫 opencli CLI，零額外依賴。

注意：opencli 的 exit code 有 bug（成功時可能非零），一律用 pipe 擷取 stdout 判斷。
"""

import subprocess
import json
import sys
import os
import re
from datetime import datetime
from typing import Any


def run(cmd: list[str], timeout: int = 30) -> str:
    """執行 opencli 指令，返回 stdout。失敗時返回空字串。"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
        # opencli exit code bug：成功輸出時可能非零，一律用 stdout 是否為有效 JSON 判斷
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        return ""


def parse_json_output(raw: str) -> list[dict]:
    """嘗試解析 JSON，失敗時返回空串列。"""
    if not raw:
        return []
    try:
        # 跳過 opencli 可能输出的 debug/warning lines
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("["):
                return json.loads("\n".join(lines[i:]))
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def norm_hackernews(items: list[dict]) -> list[dict]:
    """標準化 HackerNews 輸出"""
    out = []
    for it in items:
        out.append({
            "source": "Hacker News",
            "title": it.get("title", ""),
            "url": it.get("url") or it.get("hn_url", ""),
            "hn_url": f"https://news.ycombinator.com/item?id={it.get('id', '')}",
            "heat": f"{it.get('score', 0)} points",
            "time": it.get("time", "") or it.get("published", ""),
            "_raw_rank": it.get("rank"),
            "_raw_author": it.get("author"),
        })
    return out


def norm_producthunt(items: list[dict]) -> list[dict]:
    """標準化 Product Hunt 輸出"""
    out = []
    for it in items:
        out.append({
            "source": "Product Hunt",
            "title": f"{it.get('name', '')} — {it.get('tagline', '')}",
            "url": it.get("url", ""),
            "heat": f"PH Launch",
            "time": "Today",
            "_raw_author": it.get("author"),
        })
    return out


def norm_arxiv(items: list[dict]) -> list[dict]:
    """標準化 arXiv 輸出"""
    out = []
    for it in items:
        title = it.get("title", "")
        authors = it.get("authors", "")
        published = it.get("published", "")
        url = it.get("url", "")
        # 截取標題前 80 字
        short = title[:80] + ("..." if len(title) > 80 else "")
        out.append({
            "source": "arXiv",
            "title": short,
            "url": url,
            "heat": f"arXiv",
            "time": published[:10] if published else "",
            "_raw_authors": authors,
        })
    return out


def norm_devto(items: list[dict]) -> list[dict]:
    """標準化 Dev.to 輸出"""
    out = []
    for it in items:
        tags = ", ".join(it.get("tags", [])) if isinstance(it.get("tags"), list) else ""
        out.append({
            "source": "DEV.to",
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "heat": f"❤️ {it.get('reactions', 0)} | 💬 {it.get('comments', 0)}",
            "time": "Today",
            "_raw_author": it.get("author"),
            "_tags": tags,
        })
    return out


def norm_reddit(items: list[dict]) -> list[dict]:
    """標準化 Reddit 輸出"""
    out = []
    for it in items:
        out.append({
            "source": f"Reddit {it.get('subreddit', '')}",
            "title": it.get("title", ""),
            "url": it.get("url", "") or f"https://reddit.com{it.get('permalink', '')}",
            "heat": f"{it.get('score', 0)} pts | {it.get('comments', 0)} comments",
            "time": "Hot",
            "_raw_subreddit": it.get("subreddit"),
        })
    return out


def norm_v2ex(items: list[dict]) -> list[dict]:
    """標準化 V2EX 輸出"""
    out = []
    for it in items:
        out.append({
            "source": "V2EX",
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "heat": f"{it.get('replies', 0)} replies",
            "time": it.get("time", "") or "Hot",
        })
    return out


# ─────────────────────────────────────────────
# Fetcher 函式（與 fetch_news.py 的介面相容）
# ─────────────────────────────────────────────

def fetch_hackernews(limit: int = 10, keyword: str = None) -> list[dict]:
    cmd = ["opencli", "hackernews", "top", "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("title", "").lower()]
    return norm_hackernews(items[:limit])


def fetch_producthunt(limit: int = 10, keyword: str = None) -> list[dict]:
    cmd = ["opencli", "producthunt", "today", "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    return norm_producthunt(items[:limit])


def fetch_arxiv(keyword: str = "LLM", limit: int = 10, keyword_filter: str = None) -> list[dict]:
    query = keyword_filter or keyword or "machine learning"
    cmd = ["opencli", "arxiv", "search", query, "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    if keyword_filter:
        kw = keyword_filter.lower()
        items = [i for i in items if kw in i.get("title", "").lower()]
    return norm_arxiv(items[:limit])


def fetch_devto(limit: int = 10, keyword: str = None) -> list[dict]:
    cmd = ["opencli", "devto", "top", "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("title", "").lower()]
    return norm_devto(items[:limit])


def fetch_reddit(limit: int = 10, keyword: str = None) -> list[dict]:
    cmd = ["opencli", "reddit", "hot", "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("title", "").lower()]
    return norm_reddit(items[:limit])


def fetch_v2ex(limit: int = 10, keyword: str = None) -> list[dict]:
    cmd = ["opencli", "v2ex", "hot", "--limit", str(limit), "--format", "json"]
    items = parse_json_output(run(cmd))
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("title", "").lower()]
    return norm_v2ex(items[:limit])


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="OpenCLI news fetcher — 標準化輸出")
    parser.add_argument("--source", default="all",
                        help="Source key: hackernews | producthunt | arxiv | devto | reddit | v2ex | all")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--keyword", help="關鍵字過濾")
    parser.add_argument("--arxiv-query", default="machine learning",
                        help="arXiv 搜尋關鍵字（預設: machine learning）")
    args = parser.parse_args()

    source_map = {
        "hackernews":  lambda: fetch_hackernews(args.limit, args.keyword),
        "producthunt": lambda: fetch_producthunt(args.limit, args.keyword),
        "arxiv":       lambda: fetch_arxiv(keyword="ML", keyword_filter=args.keyword, limit=args.limit),
        "devto":       lambda: fetch_devto(args.limit, args.keyword),
        "reddit":      lambda: fetch_reddit(args.limit, args.keyword),
        "v2ex":        lambda: fetch_v2ex(args.limit, args.keyword),
    }

    results = []

    if args.source == "all":
        for fetcher in source_map.values():
            try:
                results.extend(fetcher())
            except Exception:
                pass
    elif args.source in source_map:
        results = source_map[args.source]()
    else:
        print(json.dumps({"error": f"Unknown source: {args.source}"}))
        sys.exit(1)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
