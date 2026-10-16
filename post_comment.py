#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时给 https://www.xuxuxiang.fun/ 底部留言板发一条留言。
纯标准库实现，不需要 pip install 任何东西。

站点接口协议（读自站点自己的 guestbook.js）：
  1) GET  <api>  -> {"messages": [...], "total": 18, "token": "..."}
  2) POST <api>  body: {"name","message","website","token","submissionId"}
     约束：name ≤ 20 字，message ≤ 200 字，website 必须为空（蜜罐字段）
     token 过期返回 code=TOKEN_EXPIRED，需要重新 GET 再发
     触发限流返回 429 + Retry-After 响应头
"""

import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ============================================================
# 配置（也可以在 GitHub Actions 里用环境变量覆盖）
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

API_URL = os.environ.get("GUESTBOOK_API", "https://www.xuxuxiang.fun/api/guestbook")
SITE_ORIGIN = os.environ.get("COMMENT_SITE", "https://www.xuxuxiang.fun").rstrip("/")

TABLE_CSV = BASE_DIR / "comments.csv"      # 你的「称呼 + 评论」表格
TABLE_JSON = BASE_DIR / "comments.json"    # 等价格式，两种放一个就行
STATE_FILE = BASE_DIR / "state.json"       # 记录发到表格第几条

ORDER = os.environ.get("ORDER", "sequential")   # sequential=按表格顺序轮流 | random=每次随机
JITTER_SECONDS = int(os.environ.get("JITTER_SECONDS", "300"))   # 随机延迟上限，0 = 不延迟
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

NAME_LIMIT = 20      # 站点限制：称呼最多 20 字
MESSAGE_LIMIT = 200  # 站点限制：正文最多 200 字
PLACEHOLDER_HINTS = ("示例", "请替换", "替换成")


def log(msg):
    print(msg, flush=True)


def fail(msg, code=1):
    print(f"[错误] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def clip(text, limit):
    """按字符数（不是字节）截断，中英文都兼容。"""
    chars = list(text)
    return text if len(chars) <= limit else "".join(chars[:limit]).rstrip() + "…"


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------
def base_headers(extra=None):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": SITE_ORIGIN,
        "Referer": SITE_ORIGIN + "/",
    }
    if extra:
        headers.update(extra)
    return headers


def call_api(method="GET", body=None):
    """返回 (status_code, 解析后的 dict)。HTTP 错误也走正常返回，由调用方判断。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = base_headers({"Content-Type": "application/json;charset=UTF-8"} if data else None)
    req = urllib.request.Request(API_URL, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {"error": raw[:300]}
        payload["_retry_after"] = e.headers.get("Retry-After")
        return e.code, payload
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"请求异常：{e!r}"}


def fetch_guestbook():
    """GET 留言簿，返回 (token, 已有的留言列表, 总数)。"""
    status, data = call_api("GET")
    if status != 200 or not isinstance(data.get("token"), str):
        fail(f"读取留言簿失败（HTTP {status}）：{data.get('error', data)}")
    messages = data.get("messages") or []
    total = data.get("total") if isinstance(data.get("total"), int) else len(messages)
    log(f"[信息] 留言簿当前共 {total} 条留言：")
    for item in messages[:3]:
        log(f"       · {item.get('name') or '路过的朋友'}：{item.get('message', '')[:24]}")
    return data["token"], messages, total


def submit(name, message, token):
    """提交一条留言，返回 (status, data)。"""
    body = {
        "name": name,
        "message": message,
        "website": "",                 # 蜜罐字段，必须留空
        "token": token,
        "submissionId": str(uuid.uuid4()),
    }
    return call_api("POST", body)


# ------------------------------------------------------------
# 表格
# ------------------------------------------------------------
def pick_field(row, *keys):
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
    return ""


def load_table():
    """读取「称呼 + 评论」表格，支持 csv 和 json。"""
    rows = []
    if TABLE_CSV.exists():
        with TABLE_CSV.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                name = pick_field(row, "称呼", "name", "昵称")
                message = pick_field(row, "评论", "留言", "message", "内容")
                if name or message:
                    rows.append((name, message))
    elif TABLE_JSON.exists():
        for item in json.loads(TABLE_JSON.read_text(encoding="utf-8")):
            rows.append((str(item.get("称呼") or item.get("name") or "").strip(),
                         str(item.get("评论") or item.get("message") or "").strip()))
    else:
        fail("找不到 comments.csv，请把你的「称呼 + 评论」表格存成这个文件名放在脚本同目录")

    clean = []
    for index, (name, message) in enumerate(rows, start=1):
        if any(hint in name or hint in message for hint in PLACEHOLDER_HINTS):
            fail(f"comments.csv 第 {index} 行仍是模板占位内容，请先换成你自己的表格")
        if not name or not message:
            log(f"[跳过] 第 {index} 行称呼或评论为空")
            continue
        if len(list(name)) > NAME_LIMIT:
            log(f"[提示] 第 {index} 行称呼超过 {NAME_LIMIT} 字，已截断")
        if len(list(message)) > MESSAGE_LIMIT:
            log(f"[提示] 第 {index} 行评论超过 {MESSAGE_LIMIT} 字，已截断（站点上限）")
        clean.append((clip(name, NAME_LIMIT), clip(message, MESSAGE_LIMIT)))

    if not clean:
        fail("表格里没有可用的行")
    return clean


def next_index(total_rows):
    """返回本次要用的下标，以及发送成功后应写入的游标。"""
    cursor = 0
    if STATE_FILE.exists():
        try:
            cursor = int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("cursor", 0))
        except Exception:  # noqa: BLE001
            cursor = 0

    if ORDER == "random":
        return random.randrange(total_rows), cursor
    index = cursor % total_rows
    return index, (index + 1) % total_rows


def save_cursor(cursor):
    STATE_FILE.write_text(json.dumps({"cursor": cursor}, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    rows = load_table()
    index, next_cursor = next_index(len(rows))
    name, message = rows[index]

    if JITTER_SECONDS > 0 and not DRY_RUN:
        delay = random.randint(0, JITTER_SECONDS)
        log(f"[信息] 随机延迟 {delay} 秒后再发送（避开整点整分，看起来更自然）")
        time.sleep(delay)

    log(f"[信息] 本次发送第 {index + 1}/{len(rows)} 条")
    log(f"[信息] 称呼：{name}")
    log(f"[信息] 评论：{message}")

    if DRY_RUN:
        log("[预览] DRY_RUN 已开启，不实际发送，也不推进进度")
        return

    token, _, _ = fetch_guestbook()

    status, data = submit(name, message, token)

    # token 过期：重新取一次再发一遍
    if data.get("code") == "TOKEN_EXPIRED":
        log("[信息] token 已过期，重新获取后再试一次")
        token, _, _ = fetch_guestbook()
        status, data = submit(name, message, token)

    # 被限流：本次不发，也不推进进度，下次再试
    if status == 429:
        wait = data.get("_retry_after") or "未知"
        log(f"[暂停] 站点限流了（429），需要等待 {wait} 秒。本次不发送，进度不推进。")
        log(f"[暂停] 站点提示：{data.get('error', '')}")
        return

    if status != 200:
        fail(f"发送失败（HTTP {status}）：{data.get('error', data)}")

    saved = data.get("message") or {}
    if not saved.get("id"):
        fail(f"留言没有得到确认，返回内容：{data}")

    save_cursor(next_cursor)
    log(f"[成功] 已发布：{saved.get('name')} / {saved.get('message')}")
    log(f"[成功] 留言时间：{saved.get('createdAt')}，留言簿总数：{data.get('total')}")
    log(f"[成功] 下次将发送表格第 {next_cursor + 1} 条")


if __name__ == "__main__":
    main()
