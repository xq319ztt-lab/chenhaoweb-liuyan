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
from datetime import datetime, timezone
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
SENT_LOG = BASE_DIR / "sent.log"           # 已发送记录，方便跟你的表格对账

ORDER = os.environ.get("ORDER", "sequential")   # sequential=按表格顺序轮流 | random=每次随机
LOOP = os.environ.get("LOOP", "").strip().lower() in {"1", "true", "yes"}   # 发完最后一条后是否回到开头重发
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


def call_api(method="GET", body=None, retries=0):
    """返回 (status_code, 解析后的 dict)。HTTP 错误也走正常返回，由调用方判断。

    retries 只作用于网络层失败（连不上、握手超时）——这类失败重发是安全的。
    服务端已经给出回应的情况（含 4xx/5xx）一律不重试。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = base_headers({"Content-Type": "application/json;charset=UTF-8"} if data else None)

    last = None
    for attempt in range(retries + 1):
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
            last = e
            if attempt < retries:
                log(f"[提示] 第 {attempt + 1} 次请求没连上（{e!r}），3 秒后重试")
                time.sleep(3)
    return 0, {"error": f"请求异常：{last!r}"}


def fetch_guestbook():
    """GET 留言簿，返回 (token, 已有的留言列表, 总数)。纯读操作，允许重试。"""
    status, data = call_api("GET", retries=3)
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


def normalize(text):
    """把文本压成可比较的形式：合并多余空白、统一大小写。"""
    return " ".join(str(text or "").split()).casefold()


def resume_from_board(rows, messages):
    """用留言板反推真实进度。

    返回 (应该从第几条继续的下标, {已发过的下标: 发布时间})。

    为什么需要它：state.json 只存在仓库里，一旦回写失败（比如仓库没给
    写权限、或推送冲突），进度就会永远停在原地——那时如果只用「这条发过
    就跳过」的查重逻辑，脚本会卡在第 1 条反复空转，一条也发不出去。
    留言板本身才是唯一可靠的进度账本，所以这里直接对着它算。
    """
    on_board = {}
    for item in messages:
        key = (normalize(item.get("name")), normalize(item.get("message")))
        on_board.setdefault(key, item.get("createdAt") or "时间未知")

    sent = {}
    for i, (name, message) in enumerate(rows):
        if (normalize(name), normalize(message)) in on_board:
            sent[i] = on_board[(normalize(name), normalize(message))]

    return (max(sent) + 1) if sent else 0, sent


def read_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return int(data.get("cursor", 0)), bool(data.get("finished", False))
        except Exception:  # noqa: BLE001
            pass
    return 0, False


def save_state(cursor, finished):
    STATE_FILE.write_text(
        json.dumps({"cursor": cursor, "finished": finished}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_sent_log(position, total, name, message, created_at):
    """追加一条发送记录，方便和你的表格逐条对账。"""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    flat = " ".join(str(message).split())
    with SENT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{stamp}\t{position}/{total}\t{name}\t{flat}\t{created_at or '-'}\n")


def next_index(total_rows):
    """返回 (本次下标或 None, 发送成功后应写入的游标, 是否已发完)。"""
    cursor, finished = read_state()

    if not LOOP and (finished or cursor >= total_rows):
        return None, cursor, True

    if ORDER == "random":
        return random.randrange(total_rows), cursor, False

    index = cursor % total_rows
    return index, index + 1, (not LOOP and index + 1 >= total_rows)


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    rows = load_table()
    index, next_cursor, finished = next_index(len(rows))
    if index is None:
        log(f"[完成] 表格里的 {len(rows)} 条已经全部发送完毕，本次不发任何内容。")
        log("[完成] 想让它们从头再来一遍的话，把环境变量 LOOP 设为 true。")
        return
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

    token, messages, total = fetch_guestbook()

    # 对着留言板核对真实进度：查重和纠偏一次搞定。
    # 如果这一条（或更靠后的若干条）已经在板上，说明之前发成功了但进度没记上，
    # 直接跳到实际该发的位置——既不重复，也不会卡在第 1 条空转。
    if not LOOP:
        real, sent = resume_from_board(rows, messages)
        if real > index:
            skipped = [i for i in sorted(sent) if index <= i < real]
            if skipped:
                log("[纠偏] 留言板上已经有第 " + "、".join(str(i + 1) for i in skipped)
                    + " 条，跳过不重发。")
                for i in skipped:
                    append_sent_log(i + 1, len(rows), rows[i][0], rows[i][1], sent[i])
            index = real
            if index >= len(rows):
                save_state(index, True)
                log(f"[完成] 表格里的 {len(rows)} 条已经全部发送完毕，本次不发任何内容。")
                return
            name, message = rows[index]
            next_cursor = index + 1
            finished = index + 1 >= len(rows)
            log(f"[信息] 本次发送第 {index + 1}/{len(rows)} 条")
            log(f"[信息] 称呼：{name}")
            log(f"[信息] 评论：{message}")
        if total > len(messages):
            log(f"[信息] 留言板共 {total} 条，本次用于核对的是最近 {len(messages)} 条。")

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

    save_state(next_cursor, finished)
    append_sent_log(index + 1, len(rows), name, message, saved.get("createdAt"))
    log(f"[成功] 已发布：{saved.get('name')} / {saved.get('message')}")
    log(f"[成功] 留言时间：{saved.get('createdAt')}，留言簿总数：{data.get('total')}")
    if finished:
        log(f"[完成] 这是表格里的最后一条，{len(rows)} 条已全部发送完毕，后续不会再发。")
    else:
        log(f"[成功] 下次将发送表格第 {next_cursor + 1} 条")


if __name__ == "__main__":
    main()
