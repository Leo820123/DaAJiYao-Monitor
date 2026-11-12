# -*- coding: utf-8 -*-
"""Track only the current pinned top-level reply and its author's nested replies."""
import csv
import hashlib
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
import requests
from playwright.sync_api import sync_playwright

URL = "https://www.bilibili.com/opus/1219141791464292387"
OPUS_ID = re.search(r"/opus/(\d+)", URL).group(1)
INTERVAL = 120
HOME = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PROFILE = HOME / "browser_profile"
DB = HOME / "history.sqlite3"
CSV = HOME / "up_pinned_history.csv"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(HOME / "monitor.log", encoding="utf-8")])
log = logging.getLogger("monitor")
TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def get(session, url, params=None):
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError("B站错误 %s：%s" % (payload.get("code"), payload.get("message")))
            return payload.get("data") or {}
        except (requests.RequestException, ValueError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))

def browser(pw, wait_for_login=True):
    errors = []
    for channel in ("chrome", "msedge"):
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), channel=channel, headless=False,
                viewport={"width": 1250, "height": 850}, locale="zh-CN")
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            if wait_for_login:
                print("请在弹出浏览器中登录有充电权限的 B 站账号，确认评论区可见。")
                input("登录完成后回到这里按 Enter：")
            return context
        except Exception as exc:
            errors.append("%s: %s" % (channel, exc))
    raise RuntimeError("请安装 Chrome 或 Edge。 " + "; ".join(errors))

def make_session(ctx):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                      "Referer": URL, "Origin": "https://www.bilibili.com",
                      "Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9"})
    for cookie in ctx.cookies():
        if "bilibili.com" in cookie.get("domain", ""):
            s.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
    return s

def metadata(s):
    data = get(s, "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", {"id": OPUS_ID})
    item = data.get("item") or {}
    basic = item.get("basic") or {}
    author = (item.get("modules") or {}).get("module_author") or {}
    oid = basic.get("rid_str") or basic.get("comment_id_str") or basic.get("rid")
    kind = basic.get("comment_type") or basic.get("comment_type_id")
    uid = author.get("mid") or (author.get("user") or {}).get("mid")
    if not (oid and kind and uid):
        raise RuntimeError("无法解析评论区 oid、评论类型或 UP 主 UID；可能是充电权限或 B 站接口变化")
    return str(oid), int(kind), str(uid), author.get("name", "")

def wbi_key(s):
    d = get(s, "https://api.bilibili.com/x/web-interface/nav").get("wbi_img") or {}
    if not d.get("img_url") or not d.get("sub_url"):
        raise RuntimeError("无法读取 WBI 签名")
    raw = "".join(u.rsplit("/", 1)[-1].split(".")[0] for u in (d["img_url"], d["sub_url"]))
    return "".join(raw[i] for i in TAB)[:32]

def sign(params, key):
    p = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    p["w_rid"] = hashlib.md5((urlencode(p) + key).encode()).hexdigest()
    return p

def rid(item):
    return str(item.get("rpid_str") or item.get("rpid") or "")

def find_pin(d):
    candidates = list(d.get("top_replies") or [])
    for container in ("upper", "admin", "top"):
        h = d.get(container)
        if isinstance(h, dict):
            for field in ("top", "top_reply", "reply"):
                item = h.get(field)
                if isinstance(item, dict):
                    candidates.append(item)
            if rid(h):
                candidates.append(h)
    for item in candidates:
        if isinstance(item, dict) and rid(item):
            return item
    return None

def pinned(s, oid, kind, key):
    data = get(s, "https://api.bilibili.com/x/v2/reply/wbi/main",
               sign({"oid": oid, "type": kind, "mode": 3, "web_location": 1315875}, key))
    item = find_pin(data)
    if item is None:
        raise RuntimeError("接口未明确返回置顶评论，本轮跳过，不把普通热评当作置顶")
    return item

def nested(s, oid, kind, root, progress=None):
    seen = set()
    page = 1
    while True:
        data = get(s, "https://api.bilibili.com/x/v2/reply/reply",
                   {"oid": oid, "type": kind, "root": root, "pn": page, "ps": 20})
        batch = data.get("replies") or []
        if not batch:
            break
        added = 0
        for item in batch:
            identifier = rid(item)
            if identifier and identifier not in seen:
                seen.add(identifier)
                added += 1
                yield item
        count = (data.get("page") or {}).get("count")
        if progress:
            progress(page, len(seen), int(count) if count is not None else None)
        if not added or (count is not None and len(seen) >= int(count)) or len(batch) < 20:
            break
        page += 1
        time.sleep(1)

def database():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, kind TEXT, root_id TEXT, uid TEXT, name TEXT,
        content TEXT, posted_at TEXT, seen_at TEXT, parent_id TEXT, source TEXT, last_seen TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pin_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, root_id TEXT, detected_at TEXT)""")
    conn.commit()
    return conn

def store(conn, item, kind, root):
    identifier = rid(item)
    if not identifier:
        return
    user = item.get("member") or {}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    timestamp = item.get("ctime")
    posted = datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="seconds") if timestamp else ""
    conn.execute("""INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET content=excluded.content, last_seen=excluded.last_seen""",
        (identifier, kind, root, str(item.get("mid") or user.get("mid") or ""),
         user.get("uname", ""), (item.get("content") or {}).get("message", ""),
         posted, now, str(item.get("parent") or ""), URL + "#reply" + root, now))

def export(conn):
    with CSV.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["评论ID","类别","置顶评论ID","作者UID","作者昵称","正文","发布时间","首次检测时间","父评论ID","动态链接","最近检测时间"])
        for row in conn.execute("SELECT * FROM records ORDER BY posted_at,id"):
            writer.writerow(row)

def check(s, conn, oid, kind, uid, key, progress=None):
    """Save pinned root, author replies and the precise comments they answer.

    Never save unrelated users. Parent references that are absent from the API
    are counted as unresolved rather than attributed to a different comment.
    """
    top = pinned(s, oid, kind, key)
    root = rid(top)
    previous = conn.execute("SELECT root_id FROM pin_history ORDER BY id DESC LIMIT 1").fetchone()
    if not previous or previous[0] != root:
        conn.execute("INSERT INTO pin_history(root_id,detected_at) VALUES (?,?)",
                     (root, datetime.now().astimezone().isoformat(timespec="seconds")))
        log.info("已切换置顶评论：%s", root)
    existing = {row[0] for row in conn.execute(
        "SELECT id FROM records WHERE root_id=? AND kind='UP主楼中楼'", (root,))}
    store(conn, top, "置顶主评论", root)
    all_replies = {}
    author_replies = []
    total = matches = added = 0
    def report(page, seen, page_count):
        if progress:
            progress(page, seen, page_count, matches, added)
    for reply in nested(s, oid, kind, root, progress=report):
        total += 1
        identifier = rid(reply)
        if not identifier:
            continue
        all_replies[identifier] = reply
        if str(reply.get("mid") or (reply.get("member") or {}).get("mid") or "") == uid:
            author_replies.append(reply)
            matches += 1
            store(conn, reply, "UP主楼中楼", root)
            if identifier not in existing:
                existing.add(identifier)
                added += 1

    context_ids = set()
    unresolved = set()
    # The API's parent field is the actual comment being answered, while root
    # denotes the thread. The root may itself be another user's original comment.
    for reply in author_replies:
        parent = str(reply.get("parent") or root)
        if parent in ("0", ""):
            parent = root
        if parent == rid(reply):
            unresolved.add(parent)
            continue
        if parent == root:
            context_ids.add(root)
        elif parent in all_replies:
            context_ids.add(parent)
            origin = all_replies[parent]
            if str(origin.get("mid") or (origin.get("member") or {}).get("mid") or "") != uid:
                store(conn, origin, "被UP主回复的用户原评论", root)
        else:
            # Do not substitute the pinned root for a missing user reply.
            unresolved.add(parent)
    conn.commit()
    export(conn)
    log.info("置顶 %s，扫描 %d 条，UP 主 %d 条，新保存 %d 条，关联原评论 %d 条，未定位原评论 %d 条",
             root, total, matches, added, len(context_ids), len(unresolved))
    return {"root": root, "scanned": total, "matched": matches,
            "added": added, "context": len(context_ids), "unresolved": len(unresolved)}

def main():
    conn = database()
    try:
        with sync_playwright() as pw:
            ctx = browser(pw)
            try:
                s = make_session(ctx)
                oid, kind, uid, name = metadata(s)
                log.info("UP主 %s UID=%s，评论区 oid=%s type=%s", name, uid, oid, kind)
                key = wbi_key(s)
                iteration = 0
                while True:
                    try:
                        if iteration and iteration % 60 == 0:
                            s = make_session(ctx)
                            key = wbi_key(s)
                        check(s, conn, oid, kind, uid, key)
                    except Exception:
                        log.exception("本轮检查失败，原记录保留")
                    iteration += 1
                    time.sleep(INTERVAL)
            finally:
                ctx.close()
    except KeyboardInterrupt:
        print("监控已停止")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
