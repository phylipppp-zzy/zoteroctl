#!/usr/bin/env python3
"""zoteroctl: 通过 Zotero Web API v3 管理个人库（查询、新增、修改、删除）。

仅依赖 Python 标准库。所有写操作默认只预览，加 --apply 才真正执行；
删除还需要 --confirm-key 与目标 key 一致。

配置目录查找顺序：环境变量 ZOTEROCTL_HOME；从当前目录向上找到的第一个 .zoteroctl/
目录（类似 git 找 .git，便于把配置限定在某个工作目录内）；最后是 ~/.config/zoteroctl。
凭据可用环境变量 ZOTERO_API_KEY / ZOTERO_LIBRARY_ID / ZOTERO_LIBRARY_TYPE 临时覆盖。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

API_BASE = os.environ.get("ZOTEROCTL_API_BASE", "https://api.zotero.org")


def find_config_dir() -> Path:
    if os.environ.get("ZOTEROCTL_HOME"):
        return Path(os.environ["ZOTEROCTL_HOME"])
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / ".zoteroctl").is_dir():
            return d / ".zoteroctl"
    return Path.home() / ".config" / "zoteroctl"


CONFIG_DIR = find_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
SNAPSHOT_DIR = CONFIG_DIR / "snapshots"
PAGE = 100
MAX_BATCH = 10
FORBIDDEN_FIELDS = {"key", "version", "itemType", "dateAdded", "dateModified", "tags", "collections"}


class ZoteroError(SystemExit):
    pass


def die(msg: str) -> None:
    raise ZoteroError(f"错误：{msg}")


def out(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def dry_run(action: str, detail: Any) -> None:
    print(f"[预览] {action}（未执行，确认后加 --apply）")
    out(detail)


# ---------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_backoff_until = 0.0


class Resp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    for env, key in (("ZOTERO_API_KEY", "api_key"), ("ZOTERO_LIBRARY_ID", "library_id"),
                     ("ZOTERO_LIBRARY_TYPE", "library_type")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("library_type", "user")
    return cfg


def raw_request(method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, api_key: str | None = None) -> Resp:
    global _backoff_until
    hdrs = {"Zotero-API-Version": "3", "User-Agent": "zoteroctl/1.0"}
    if api_key:
        hdrs["Zotero-API-Key"] = api_key
    hdrs.update(headers or {})
    for attempt in range(5):
        wait = _backoff_until - time.time()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with _opener.open(req, timeout=120) as r:
                resp = Resp(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            resp = Resp(e.code, dict(e.headers or {}), e.read() or b"")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            die(f"网络错误 {method} {url}: {getattr(e, 'reason', e)}")
        backoff = resp.header("Backoff")
        if backoff:
            _backoff_until = time.time() + float(backoff)
        if resp.status in (429, 503) and attempt < 4:
            time.sleep(float(resp.header("Retry-After") or 2 ** (attempt + 1)))
            continue
        return resp
    return resp


class Client:
    def __init__(self) -> None:
        cfg = load_config()
        if not cfg.get("api_key") or not cfg.get("library_id"):
            die(f"未配置凭据。请运行 `zoteroctl configure`（写入 {CONFIG_FILE}）或设置 ZOTERO_API_KEY/ZOTERO_LIBRARY_ID。")
        self.key = cfg["api_key"]
        lt = cfg["library_type"]
        if lt not in ("user", "group"):
            die("library_type 只能是 user 或 group")
        self.prefix = f"/{lt}s/{cfg['library_id']}"

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        global_path = path.startswith(("/keys", "/items/new", "/itemTypes", "/itemFields", "/creatorFields"))
        u = API_BASE + (path if global_path else self.prefix + path)
        if params:
            u += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return u

    def call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             payload: Any = None, headers: dict[str, str] | None = None,
             raw_body: bytes | None = None, ok: tuple[int, ...] = (200,)) -> Resp:
        hdrs = dict(headers or {})
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        resp = raw_request(method, self.url(path, params), headers=hdrs, body=body, api_key=self.key)
        if resp.status not in ok:
            text = resp.body.decode("utf-8", "replace")[:800]
            if resp.status == 412:
                die(f"版本冲突（412）：对象在读取后已被修改。请重新读取后再计算变更。{text}")
            if resp.status == 404:
                die(f"对象不存在（404）：{method} {path}")
            die(f"HTTP {resp.status} {method} {path}: {text}")
        return resp

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params or None).json()

    def get_all(self, path: str, **params: Any) -> list[Any]:
        rows: list[Any] = []
        start = 0
        while True:
            resp = self.call("GET", path, params={**params, "limit": PAGE, "start": start})
            batch = resp.json()
            rows.extend(batch)
            total = int(resp.header("Total-Results") or len(rows))
            start += len(batch)
            if not batch or start >= total:
                return rows

    def item(self, key: str) -> dict[str, Any]:
        return self.get(f"/items/{key}")

    def collection(self, key: str) -> dict[str, Any]:
        return self.get(f"/collections/{key}")

    def write_items(self, objs: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self.call("POST", "/items", payload=objs,
                         headers={"Zotero-Write-Token": secrets.token_hex(16)})
        return check_multi(resp.json())

    def write_collections(self, objs: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self.call("POST", "/collections", payload=objs,
                         headers={"Zotero-Write-Token": secrets.token_hex(16)})
        return check_multi(resp.json())


class WebDAV:
    """Zotero 的 WebDAV 文件同步格式：<url>/zotero/<KEY>.zip（内含文件）+ <KEY>.prop（mtime 与 md5）。"""

    def __init__(self, cfg: dict[str, str]):
        # webdav_dir 是 configure-webdav 实测确定的 zotero 目录；缺省时按 Zotero 规则在地址后加 zotero/
        self.base = cfg.get("webdav_dir") or cfg["webdav_url"].rstrip("/") + "/zotero/"
        token = base64.b64encode(f"{cfg['webdav_user']}:{cfg['webdav_password']}".encode()).decode()
        self.auth = {"Authorization": f"Basic {token}"}

    def req(self, method: str, name: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> Resp:
        return raw_request(method, self.base + name, body=body, headers={**self.auth, **(headers or {})})

    def check(self) -> Resp:
        return self.req("PROPFIND", "", headers={"Depth": "0"})

    def upload(self, key: str, path: Path, md5: str, mtime_ms: int) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, arcname=path.name)
        r = self.req("PUT", f"{key}.zip", body=buf.getvalue(), headers={"Content-Type": "application/zip"})
        if r.status not in (200, 201, 204):
            die(f"WebDAV 上传 {key}.zip 失败：HTTP {r.status} {r.body[:200]!r}")
        prop = f'<properties version="1"><mtime>{mtime_ms}</mtime><hash>{md5}</hash></properties>'
        r = self.req("PUT", f"{key}.prop", body=prop.encode(), headers={"Content-Type": "text/xml"})
        if r.status not in (200, 201, 204):
            die(f"WebDAV 上传 {key}.prop 失败：HTTP {r.status}")

    def download(self, key: str) -> tuple[str, bytes] | None:
        r = self.req("GET", f"{key}.zip")
        if r.status == 404:
            return None
        if r.status != 200:
            die(f"WebDAV 下载 {key}.zip 失败：HTTP {r.status}")
        with zipfile.ZipFile(io.BytesIO(r.body)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                die(f"{key}.zip 是空压缩包")
            return names[0], zf.read(names[0])


def get_webdav() -> WebDAV | None:
    cfg = load_config()
    return WebDAV(cfg) if cfg.get("webdav_url") else None


def check_multi(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("failed"):
        out(result["failed"])
        die("部分或全部对象写入失败，见上方 failed。请先回读确认哪些已成功，再处理失败项。")
    return result


# ---------------------------------------------------------------- helpers


def brief_item(obj: dict[str, Any]) -> dict[str, Any]:
    d = obj.get("data", {})
    row = {
        "key": obj.get("key"),
        "version": obj.get("version"),
        "itemType": d.get("itemType"),
        "title": d.get("title") or (d.get("note", "")[:80] if d.get("itemType") == "note" else None),
    }
    if d.get("parentItem"):
        row["parentItem"] = d["parentItem"]
    else:
        row["collections"] = d.get("collections", [])
        row["numChildren"] = obj.get("meta", {}).get("numChildren", 0)
    row["tags"] = [t["tag"] for t in d.get("tags", [])]
    if d.get("itemType") == "attachment":
        row.update(linkMode=d.get("linkMode"), contentType=d.get("contentType"), filename=d.get("filename"))
    if d.get("deleted"):
        row["deleted"] = True
    return row


def read_json_file(path: str) -> Any:
    p = Path(path)
    if not p.is_file():
        die(f"找不到 JSON 文件：{path}（--fields/add 只接收文件路径）")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"{path} 不是合法 JSON：{e}")


# Zotero API 只允许对 PDF、EPUB、网页快照附件请求 /children，其他附件（如 .xpi、.docx）返回 400 和这段文字。
CHILDREN_UNSUPPORTED = "/children can only be called on PDF, EPUB, and snapshot attachments"


def children_of(c: Client, obj: dict[str, Any]) -> list[dict[str, Any]]:
    """列出对象的子对象（含回收站中的）。obj 是 /items/KEY 返回的对象。

    只有“对象是附件且 API 返回上面那段 400”时按“没有子对象”处理，其余错误照常报错。
    """
    key = obj["key"]
    path = f"/items/{key}/children"
    if obj["data"].get("itemType") == "attachment":
        resp = c.call("GET", path, params={"includeTrashed": 1, "limit": PAGE, "start": 0}, ok=(200, 400))
        if resp.status == 400:
            text = resp.body.decode("utf-8", "replace")
            if CHILDREN_UNSUPPORTED not in text:
                die(f"HTTP 400 GET {path}: {text[:800]}")
            return []
        rows = resp.json()
        if int(resp.header("Total-Results") or len(rows)) <= len(rows):
            return rows
    return c.get_all(path, includeTrashed=1)


def require_confirm(args: argparse.Namespace, key: str) -> None:
    if args.apply and args.confirm_key != key:
        die(f"删除需要 --confirm-key {key} 与目标 key 完全一致。")


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- commands


def cmd_configure(args: argparse.Namespace) -> None:
    global CONFIG_DIR, CONFIG_FILE
    if args.here:
        CONFIG_DIR = Path.cwd() / ".zoteroctl"
        CONFIG_FILE = CONFIG_DIR / "config.json"
    key = clean_secret(args.api_key or getpass.getpass("Zotero API key（https://www.zotero.org/settings/keys）: "))
    if not key:
        die("未输入 API key")
    resp = raw_request("GET", f"{API_BASE}/keys/current", api_key=key)
    if resp.status != 200:
        die(f"API key 校验失败：HTTP {resp.status} {resp.body.decode('utf-8', 'replace')[:300]}")
    info = resp.json()
    lib_type = args.library_type
    lib_id = args.library_id or (str(info["userID"]) if lib_type == "user" else None)
    if not lib_id:
        die("group 库需要 --library-id")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}  # 保留已有的 WebDAV 配置
    cfg.update(api_key=key, library_type=lib_type, library_id=lib_id)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_FILE, 0o600)
    print(f"已写入 {CONFIG_FILE}（权限 600）")
    out({"username": info.get("username"), "userID": info.get("userID"),
         "library": f"{lib_type}s/{lib_id}", "access": info.get("access")})


def clean_secret(raw: str) -> str:
    """去掉终端“括号粘贴”标记（ESC[200~ / ESC[201~）、其他 ANSI 序列和控制字符，如回车。"""
    text = re.sub(r"\x1b\[[0-9;]*[~A-Za-z]", "", raw)
    text = re.sub(r"\[20[01]~", "", text)  # ESC 被终端吞掉时残留的标记
    return "".join(ch for ch in text if ch.isprintable())


def cmd_configure_webdav(args: argparse.Namespace) -> None:
    url = args.url or input("WebDAV 地址（Zotero 设置里“网址”一栏的内容，例如 https://dav.jianguoyun.com/dav/）: ").strip()
    user = args.user or input("WebDAV 用户名: ").strip()
    if args.password_stdin:
        raw = sys.stdin.readline()
    else:
        raw = getpass.getpass("WebDAV 密码（与 Zotero 设置中的一致）: ")
    password = clean_secret(raw)
    if password != raw:
        print(f"已去掉粘贴时混入的控制字符：输入长度 {len(raw)} → 实际密码长度 {len(password)}", file=sys.stderr)
    if password == user:
        die("输入的密码与用户名完全相同，多半是剪贴板里还是用户名（从密码框复制常常不生效）。请重新复制或手动输入密码。")
    if not url.startswith("https://") and not url.startswith("http://"):
        url = "https://" + url
    # Zotero 会在设置里填写的地址后自动加 zotero/。用户可能填的是不带 zotero 的地址，
    # 也可能照抄了界面显示的完整路径，所以两种目录都试，以含 lastsync.txt（Zotero 同步过）的为准。
    root = url.rstrip("/")
    candidates = [root + "/zotero/"]
    if root.endswith("/zotero"):
        candidates.insert(0, root + "/")
    found = []
    for d in candidates:
        trial = WebDAV({"webdav_dir": d, "webdav_user": user, "webdav_password": password})
        r = trial.check()
        if r.status == 401:
            hint = "；密码首尾有空格" if password != password.strip() else ""
            die(f"WebDAV 认证失败（HTTP 401，{d}）。本次输入：用户名 {user!r}，密码长度 {len(password)}{hint}。"
                "请在 Zotero 设置里点密码框右侧的眼睛图标核对密码。")
        if r.status in (200, 207):
            synced = trial.req("GET", "lastsync.txt").status == 200 or trial.req("GET", "lastsync").status == 200
            found.append((synced, d))
        print(f"检查 {d}：HTTP {r.status}", file=sys.stderr)
    if not found:
        die(f"WebDAV 上找不到 Zotero 目录（试过 {candidates}）。请先在 Zotero 桌面端完成一次文件同步。")
    synced_dirs = [d for ok, d in found if ok]
    webdav_dir = synced_dirs[0] if synced_dirs else found[0][1]
    if not synced_dirs:
        print("注意：目录存在但没有 lastsync.txt，Zotero 可能还没在这里同步过文件。", file=sys.stderr)
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    cfg.update(webdav_url=url, webdav_user=user, webdav_password=password, webdav_dir=webdav_dir)
    trial = WebDAV(cfg)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_FILE, 0o600)
    print(f"已写入 WebDAV 配置：{trial.base}（{CONFIG_FILE}，权限 600）")


def cmd_status(args: argparse.Namespace) -> None:
    c = Client()
    info = c.call("GET", "/keys/current").json()
    items = c.call("GET", "/items", params={"limit": 1})
    top = c.call("GET", "/items/top", params={"limit": 1})
    cols = c.call("GET", "/collections", params={"limit": 1})
    out({
        "library": c.prefix.strip("/"),
        "username": info.get("username"),
        "access": info.get("access"),
        "libraryVersion": int(items.header("Last-Modified-Version") or 0),
        "items_total": int(items.header("Total-Results") or 0),
        "top_level_items": int(top.header("Total-Results") or 0),
        "collections": int(cols.header("Total-Results") or 0),
        "file_storage": "webdav: " + get_webdav().base if get_webdav() else "zotero",
        "config": str(CONFIG_FILE),
    })


def cmd_collections(args: argparse.Namespace) -> None:
    c = Client()
    rows = c.get_all("/collections")
    data = [{"key": r["key"], "name": r["data"]["name"], "parentCollection": r["data"].get("parentCollection") or None,
             "numItems": r.get("meta", {}).get("numItems", 0), "numCollections": r.get("meta", {}).get("numCollections", 0),
             "version": r["version"]} for r in rows]
    if args.full:
        data = rows
    out(sorted(data, key=lambda r: (r.get("parentCollection") or "", r.get("name", ""))) if not args.full else data)


def cmd_list(args: argparse.Namespace) -> None:
    c = Client()
    path = "/items" if args.all else "/items/top"
    params: dict[str, Any] = {}
    if args.query:
        params.update(q=args.query, qmode=args.qmode)
    if args.collection:
        path = f"/collections/{args.collection}/items" + ("" if args.all else "/top")
    if args.tag:
        params["tag"] = args.tag
    if args.include_trashed:
        params["includeTrashed"] = 1
    rows = c.get_all(path, **params)
    out(rows if args.full else [brief_item(r) for r in rows])


def cmd_get(args: argparse.Namespace) -> None:
    c = Client()
    resp = raw_request("GET", c.url(f"/items/{args.key}"), api_key=c.key)
    if resp.status == 404:
        col = c.collection(args.key)
        out({"object": "collection", **col})
        return
    if resp.status != 200:
        die(f"HTTP {resp.status}: {resp.body.decode('utf-8', 'replace')[:300]}")
    obj = resp.json()
    result = {"object": "item", **obj}
    if not obj["data"].get("parentItem"):
        result["children"] = [brief_item(ch) for ch in children_of(c, obj)]
    out(result)


def cmd_fetch(args: argparse.Namespace) -> None:
    c = Client()
    dest = Path(args.dest)
    if dest.exists():
        die(f"目标路径已存在：{dest}")
    att = c.item(args.key)
    if att["data"].get("itemType") != "attachment":
        die(f"{args.key} 不是附件")
    dav = None if args.storage == "zotero" else get_webdav()
    if args.storage == "webdav" and not dav:
        die("未配置 WebDAV，请先运行 zoteroctl configure-webdav")
    if dav:
        got = dav.download(args.key)
        if got is None:
            die(f"WebDAV 上没有 {args.key}.zip（可能存在 Zotero 存储中，可加 --storage zotero 再试）")
        body = got[1]
        source = "webdav"
    else:
        resp = c.call("GET", f"/items/{args.key}/file", ok=(200, 302))
        body = resp.body
        if resp.status == 302:
            loc = resp.header("Location")
            r2 = raw_request("GET", loc)  # 不向存储服务转发 API key
            if r2.status != 200:
                die(f"下载文件失败：HTTP {r2.status}")
            body = r2.body
        source = "zotero"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    md5 = hashlib.md5(body).hexdigest()
    out({"saved": str(dest.resolve()), "source": source, "bytes": len(body), "md5": md5,
         "md5_matches_record": md5 == att["data"].get("md5"), "is_pdf": body[:5] == b"%PDF-"})


def cmd_snapshot(args: argparse.Namespace) -> None:
    c = Client()
    ver_resp = c.call("GET", "/items", params={"limit": 1})
    snap = {
        "library": c.prefix.strip("/"),
        "libraryVersion": int(ver_resp.header("Last-Modified-Version") or 0),
        "exportedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "collections": c.get_all("/collections"),
        "items": c.get_all("/items", includeTrashed=1),
    }
    if args.out:
        dest = Path(args.out)
    else:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        dest = SNAPSHOT_DIR / f"zotero-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.json"
    if dest.exists():
        die(f"快照文件已存在：{dest}")
    dest.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    out({"snapshot": str(dest.resolve()), "libraryVersion": snap["libraryVersion"],
         "items": len(snap["items"]), "collections": len(snap["collections"]),
         "note": "仅含元数据和笔记，不含 PDF 文件"})


def _ensure_collections_exist(c: Client, keys: list[str]) -> None:
    existing = {r["key"] for r in c.get_all("/collections")}
    missing = [k for k in keys if k not in existing]
    if missing:
        die(f"分类不存在：{missing}")


def cmd_add(args: argparse.Namespace) -> None:
    c = Client()
    raw = read_json_file(args.file)
    items = raw if isinstance(raw, list) else [raw]
    if len(items) > MAX_BATCH:
        die(f"每批最多 {MAX_BATCH} 条，当前 {len(items)} 条")
    prepared, dupes = [], {}
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("itemType"):
            die(f"第 {i} 条缺少 itemType")
        if it["itemType"] in ("attachment", "note"):
            die("add 只用于父条目；附件用 attach，笔记用 note")
        it = {k: v for k, v in it.items() if k not in ("key", "version")}
        template = c.get("/items/new", itemType=it["itemType"])
        unknown = sorted(set(it) - set(template) - {"collections", "tags", "relations", "deleted"})
        if unknown:
            die(f"第 {i} 条含 {it['itemType']} 不支持的字段：{unknown}")
        tags = list(it.get("tags", []))
        if not any(str(t.get("tag", "")).startswith("status:") for t in tags):
            tags.append({"tag": "status:inbox"})
        it["tags"] = tags
        if it.get("collections"):
            _ensure_collections_exist(c, it["collections"])
        prepared.append(it)
        if it.get("title"):
            hits = c.get_all("/items/top", q=it["title"], qmode="titleCreatorYear")
            if hits:
                dupes[it["title"]] = [brief_item(h) for h in hits]
    if not args.apply:
        dry_run(f"新增 {len(prepared)} 个父条目", {"items": prepared, "possible_duplicates_by_title": dupes})
        if dupes:
            print("注意：按题名检索到已有条目，请再比较 DOI/arXiv/URL/作者后决定是否复用。")
        return
    res = c.write_items(prepared)
    out({"created": res.get("success"), "unchanged": res.get("unchanged")})


def cmd_attach(args: argparse.Namespace) -> None:
    c = Client()
    pdf = Path(args.pdf)
    if not pdf.is_file():
        die(f"文件不存在：{pdf}")
    with pdf.open("rb") as f:
        if f.read(5) != b"%PDF-":
            die("附件必须是 PDF（文件头不是 %PDF-）")
    parent = c.item(args.item)
    if parent["data"].get("parentItem"):
        die(f"{args.item} 是子对象，不能作为附件父条目")
    title = args.title or pdf.stem
    stat = pdf.stat()
    md5 = md5_file(pdf)
    siblings = [brief_item(ch) for ch in children_of(c, parent)
                if ch["data"].get("itemType") == "attachment"]
    same = [s for s in siblings if s.get("title") == title]
    attach_obj = {"itemType": "attachment", "parentItem": args.item, "linkMode": "imported_file",
                  "title": title, "accessDate": "", "url": "", "note": "", "tags": [], "relations": {},
                  "contentType": "application/pdf", "charset": "", "filename": pdf.name,
                  "md5": None, "mtime": None}
    if not args.apply:
        dry_run("上传 PDF 附件", {"parent": {"key": args.item, "title": parent["data"].get("title")},
                               "attachment": attach_obj, "file": str(pdf.resolve()), "bytes": stat.st_size,
                               "md5": md5, "existing_attachments": siblings})
        if same:
            print("注意：父条目下已有同名附件，请确认不是重复上传。")
        return
    dav = None if args.storage == "zotero" else get_webdav()
    if args.storage == "webdav" and not dav:
        die("未配置 WebDAV，请先运行 zoteroctl configure-webdav")
    res = c.write_items([attach_obj])
    akey = res["success"]["0"]
    print(f"已创建附件记录 {akey}，开始上传文件（{'WebDAV' if dav else 'Zotero 存储'}）……", file=sys.stderr)
    if dav:
        mtime_ms = int(stat.st_mtime * 1000)
        try:
            dav.upload(akey, pdf, md5, mtime_ms)
        except ZoteroError as e:
            die(f"{e}。附件记录 {akey} 已创建但无文件，请处理后重试或删除该记录。")
        cur = c.item(akey)
        c.call("PATCH", f"/items/{akey}", payload={"md5": md5, "mtime": mtime_ms},
               headers={"If-Unmodified-Since-Version": str(cur["version"])}, ok=(204,))
        got = dav.download(akey)
        back_md5 = hashlib.md5(got[1]).hexdigest() if got else None
        after = c.item(akey)
        out({"attachment": brief_item(after), "storage": "webdav", "md5_local": md5,
             "md5_record": after["data"].get("md5"), "md5_downloaded_back": back_md5,
             "verified": back_md5 == md5 == after["data"].get("md5")})
        return
    form = urllib.parse.urlencode({"md5": md5, "filename": pdf.name, "filesize": stat.st_size,
                                   "mtime": int(stat.st_mtime * 1000)}).encode()
    auth = c.call("POST", f"/items/{akey}/file", raw_body=form,
                  headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}).json()
    if not auth.get("exists"):
        body = auth["prefix"].encode() + pdf.read_bytes() + auth["suffix"].encode()
        up = raw_request("POST", auth["url"], body=body, headers={"Content-Type": auth["contentType"]})
        if up.status != 201:
            die(f"文件上传到存储失败：HTTP {up.status}。附件记录 {akey} 已创建但无文件，请处理后重试或删除该记录。")
        c.call("POST", f"/items/{akey}/file", raw_body=urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode(),
               headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}, ok=(204,))
    after = c.item(akey)
    out({"attachment": brief_item(after), "md5_record": after["data"].get("md5"), "md5_local": md5,
         "file_exists_on_server": bool(auth.get("exists")) or after["data"].get("md5") == md5})


def cmd_note(args: argparse.Namespace) -> None:
    c = Client()
    html_path = Path(args.html)
    if not html_path.is_file():
        die(f"文件不存在：{html_path}")
    html = html_path.read_text(encoding="utf-8")
    parent = c.item(args.item)
    if parent["data"].get("parentItem"):
        die(f"{args.item} 是子对象，不能作为笔记父条目")
    notes = [brief_item(ch) for ch in children_of(c, parent) if ch["data"].get("itemType") == "note"]
    obj = {"itemType": "note", "parentItem": args.item, "note": html, "tags": [], "relations": {}}
    if not args.apply:
        dry_run("新建子笔记（不是更新）", {"parent": {"key": args.item, "title": parent["data"].get("title")},
                                   "note_preview": html[:500], "note_chars": len(html), "existing_notes": notes})
        if notes:
            print("注意：父条目下已有笔记。若要更新已有索引笔记，请用 update NOTEKEY --fields。")
        return
    res = c.write_items([obj])
    out({"created_note": res["success"]["0"]})


def cmd_collection(args: argparse.Namespace) -> None:
    c = Client()
    cols = c.get_all("/collections")
    keys = {r["key"] for r in cols}
    if args.parent and args.parent not in keys:
        die(f"父分类不存在：{args.parent}")
    same = [r["key"] for r in cols if r["data"]["name"] == args.name
            and (r["data"].get("parentCollection") or None) == (args.parent or None)]
    obj = {"name": args.name, "parentCollection": args.parent or False}
    if same:
        die(f"同一父分类下已存在同名分类：{same}")
    if not args.apply:
        dry_run("新建分类", obj)
        return
    res = c.write_collections([obj])
    out({"created_collection": res["success"]["0"]})


def cmd_update(args: argparse.Namespace) -> None:
    c = Client()
    obj = c.item(args.key)
    data = obj["data"]
    patch: dict[str, Any] = {}
    changes: dict[str, Any] = {}
    if args.fields:
        fields = read_json_file(args.fields)
        if not isinstance(fields, dict) or not fields:
            die("--fields 文件必须是非空 JSON 对象")
        bad = sorted(set(fields) & FORBIDDEN_FIELDS)
        if bad:
            die(f"--fields 不能修改 {bad}；标签/分类用 --tag/--collection，其他用受控脚本")
        if "parentItem" in fields:
            die("改挂父条目请用 move-child")
        unknown = sorted(set(fields) - set(data))
        if unknown:
            die(f"{data['itemType']} 没有字段 {unknown}")
        for k, v in fields.items():
            if data.get(k) != v:
                patch[k] = v
                changes[k] = {"old": data.get(k), "new": v}
    if args.tag:
        cur = [t["tag"] for t in data.get("tags", [])]
        add = [t for t in dict.fromkeys(args.tag) if t not in cur]
        if add:
            patch["tags"] = data.get("tags", []) + [{"tag": t} for t in add]
            changes["tags_added"] = add
    if args.remove_tag:
        base = patch.get("tags", data.get("tags", []))
        drop = [t for t in dict.fromkeys(args.remove_tag) if any(x["tag"] == t for x in base)]
        if drop:
            patch["tags"] = [x for x in base if x["tag"] not in drop]
            changes["tags_removed"] = drop
    if args.collection:
        if data.get("parentItem"):
            die("子对象（附件/笔记）不能直接加入分类")
        _ensure_collections_exist(c, args.collection)
        add = [k for k in dict.fromkeys(args.collection) if k not in data.get("collections", [])]
        if add:
            patch["collections"] = data.get("collections", []) + add
            changes["collections_added"] = add
    if not args.fields and not args.tag and not args.collection and not args.remove_tag:
        die("请至少指定 --fields、--tag、--remove-tag 或 --collection")
    if not patch:
        print("无变化：目标值与现状一致。")
        return
    if not args.apply:
        dry_run(f"修改 {args.key}（{data.get('itemType')}，version {obj['version']}）", changes)
        return
    c.call("PATCH", f"/items/{args.key}", payload=patch,
           headers={"If-Unmodified-Since-Version": str(obj["version"])}, ok=(204,))
    out({"updated": brief_item(c.item(args.key)), "changes": changes})


def cmd_move_collection(args: argparse.Namespace) -> None:
    c = Client()
    col = c.collection(args.key)
    new_parent: Any = False if args.new_parent.upper() == "ROOT" else args.new_parent
    if new_parent:
        if new_parent == args.key:
            die("不能移到自身之下")
        cols = {r["key"]: r for r in c.get_all("/collections")}
        if new_parent not in cols:
            die(f"目标父分类不存在：{new_parent}")
        p = new_parent
        while p:
            if p == args.key:
                die("不能移到自己的子分类之下")
            p = cols[p]["data"].get("parentCollection") or None
    detail = {"collection": args.key, "name": col["data"]["name"],
              "old_parent": col["data"].get("parentCollection") or None, "new_parent": new_parent or None}
    if not args.apply:
        dry_run("移动分类", detail)
        return
    c.call("PATCH", f"/collections/{args.key}", payload={"parentCollection": new_parent},
           headers={"If-Unmodified-Since-Version": str(col["version"])}, ok=(204,))
    after = c.collection(args.key)
    out({"moved": args.key, "parentCollection": after["data"].get("parentCollection") or None})


def cmd_move_child(args: argparse.Namespace) -> None:
    c = Client()
    child = c.item(args.key)
    if child["data"].get("itemType") not in ("attachment", "note"):
        die(f"{args.key} 不是附件或笔记")
    parent = c.item(args.new_parent)
    if parent["data"].get("parentItem"):
        die(f"{args.new_parent} 是子对象，不能作为父条目")
    detail = {"child": brief_item(child), "old_parent": child["data"].get("parentItem"),
              "new_parent": {"key": args.new_parent, "title": parent["data"].get("title")}}
    if not args.apply:
        dry_run("改挂子对象", detail)
        return
    c.call("PATCH", f"/items/{args.key}", payload={"parentItem": args.new_parent},
           headers={"If-Unmodified-Since-Version": str(child["version"])}, ok=(204,))
    out({"moved": args.key, "parentItem": c.item(args.key)["data"].get("parentItem")})


def cmd_delete(args: argparse.Namespace) -> None:
    c = Client()
    require_confirm(args, args.key)
    resp = raw_request("GET", c.url(f"/items/{args.key}"), api_key=c.key)
    if resp.status == 404:
        print(f"{args.key} 已不存在，无需删除。")
        return
    if resp.status != 200:
        die(f"HTTP {resp.status}")
    obj = resp.json()
    kids = [] if obj["data"].get("parentItem") and obj["data"].get("itemType") == "note" else children_of(c, obj)
    if kids:
        out({"target": brief_item(obj), "children": [brief_item(k) for k in kids]})
        die("目标仍有子对象（附件/笔记/批注），CLI 拒绝删除。请先保全并处理子对象（可用 move-child 改挂）。")
    detail = {"target": brief_item(obj), "effect": "调用 API DELETE，按永久删除处理；附件存储中的文件不保证被清理"}
    if not args.apply:
        dry_run("删除对象", detail)
        print(f"确认执行：zoteroctl delete {args.key} --confirm-key {args.key} --apply")
        return
    c.call("DELETE", f"/items/{args.key}", headers={"If-Unmodified-Since-Version": str(obj["version"])}, ok=(204,))
    gone = raw_request("GET", c.url(f"/items/{args.key}"), api_key=c.key).status == 404
    out({"deleted": args.key, "verified_absent": gone})


def cmd_delete_collection(args: argparse.Namespace) -> None:
    c = Client()
    require_confirm(args, args.key)
    resp = raw_request("GET", c.url(f"/collections/{args.key}"), api_key=c.key)
    if resp.status == 404:
        print(f"分类 {args.key} 已不存在，无需删除。")
        return
    col = resp.json()
    allcols = c.get_all("/collections")
    tree, frontier = [], [args.key]
    while frontier:
        k = frontier.pop()
        subs = [r for r in allcols if r["data"].get("parentCollection") == k]
        tree.extend(subs)
        frontier.extend(s["key"] for s in subs)
    if tree and not args.include_subcollections:
        out({"subcollections": [{"key": s["key"], "name": s["data"]["name"]} for s in tree]})
        die("该分类有子分类。仅在已授权删除整棵子分类树时加 --include-subcollections。")
    detail = {"collection": args.key, "name": col["data"]["name"], "numItems": col["meta"].get("numItems", 0),
              "subcollections": [{"key": s["key"], "name": s["data"]["name"]} for s in tree],
              "effect": "文献本身不删除，但成员关系移除；分类无回收站，重建不能恢复原 key"}
    if not args.apply:
        dry_run("删除分类", detail)
        return
    # 自底向上逐个删除，不依赖服务器端级联；tree 按广度优先排列，倒序即先删最深层
    targets = [(s["key"], s["version"]) for s in reversed(tree)] + [(args.key, col["version"])]
    result = {}
    for key, _ in targets:
        cur = raw_request("GET", c.url(f"/collections/{key}"), api_key=c.key)
        if cur.status == 404:
            result[key] = "已不存在"
            continue
        c.call("DELETE", f"/collections/{key}",
               headers={"If-Unmodified-Since-Version": str(cur.json()["version"])}, ok=(204,))
        gone = raw_request("GET", c.url(f"/collections/{key}"), api_key=c.key).status == 404
        result[key] = "已删除" if gone else "删除后仍存在"
    out({"deleted_collections": result})


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zoteroctl", description="Zotero Web API 文献管理（写操作默认预览，--apply 执行）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("configure", help="写入 API key 配置（交互输入，不回显）")
    s.add_argument("--api-key")
    s.add_argument("--library-type", choices=["user", "group"], default="user")
    s.add_argument("--library-id", help="group 库必填；user 库自动取 userID")
    s.add_argument("--here", action="store_true", help="在当前目录新建 .zoteroctl/，配置只在此目录及其子目录内生效")
    s.set_defaults(fn=cmd_configure)

    s = sub.add_parser("configure-webdav", help="写入 WebDAV 文件存储配置（密码交互输入）")
    s.add_argument("--url")
    s.add_argument("--user")
    s.add_argument("--password-stdin", action="store_true", help="从标准输入读取密码（用于脚本）")
    s.set_defaults(fn=cmd_configure_webdav)

    sub.add_parser("status", help="凭据、库版本和数量").set_defaults(fn=cmd_status)

    s = sub.add_parser("collections", help="列出分类（key、名称、父子关系）")
    s.add_argument("--full", action="store_true", help="输出完整 API 对象")
    s.set_defaults(fn=cmd_collections)

    s = sub.add_parser("list", help="列出顶层条目；--all 含附件/笔记/批注")
    s.add_argument("--query")
    s.add_argument("--qmode", choices=["titleCreatorYear", "everything"], default="titleCreatorYear")
    s.add_argument("--all", action="store_true")
    s.add_argument("--collection", help="限定分类 key")
    s.add_argument("--tag")
    s.add_argument("--include-trashed", action="store_true")
    s.add_argument("--full", action="store_true", help="输出完整 API 对象（含 data.*）")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("get", help="对象详情（条目附带子对象概要；也可查分类 key）")
    s.add_argument("key")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("fetch", help="下载附件文件到新路径")
    s.add_argument("key")
    s.add_argument("dest")
    s.add_argument("--storage", choices=["auto", "webdav", "zotero"], default="auto",
                   help="auto：配置了 WebDAV 就用 WebDAV")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("snapshot", help="导出元数据快照（不含 PDF）")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("add", help="从 JSON 文件新增父条目")
    s.add_argument("file")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("attach", help="给父条目上传 PDF 附件")
    s.add_argument("item")
    s.add_argument("pdf")
    s.add_argument("--title")
    s.add_argument("--storage", choices=["auto", "webdav", "zotero"], default="auto",
                   help="auto：配置了 WebDAV 就用 WebDAV")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("note", help="给父条目新建一条 HTML 笔记")
    s.add_argument("item")
    s.add_argument("html")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("collection", help="新建分类")
    s.add_argument("name")
    s.add_argument("--parent")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_collection)

    s = sub.add_parser("update", help="修改字段；--tag/--collection 只增加，--remove-tag 移除标签")
    s.add_argument("key")
    s.add_argument("--fields", help="JSON 文件路径")
    s.add_argument("--tag", action="append")
    s.add_argument("--remove-tag", action="append", help="移除指定标签（不存在则忽略）")
    s.add_argument("--collection", action="append")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("move-collection", help="移动分类到新父分类（ROOT 表示顶层）")
    s.add_argument("key")
    s.add_argument("new_parent")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_move_collection)

    s = sub.add_parser("move-child", help="把附件/笔记改挂到另一父条目")
    s.add_argument("key")
    s.add_argument("new_parent")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_move_child)

    s = sub.add_parser("delete", help="永久删除条目/附件/笔记（拒绝删除仍有子对象的对象）")
    s.add_argument("key")
    s.add_argument("--confirm-key")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser("delete-collection", help="删除分类（不删文献）")
    s.add_argument("key")
    s.add_argument("--confirm-key")
    s.add_argument("--include-subcollections", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_delete_collection)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
