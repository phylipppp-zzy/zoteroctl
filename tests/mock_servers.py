"""测试用的最小 Zotero Web API 与 WebDAV 模拟服务器（只实现 zoteroctl 用到的部分）。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEMPLATES = {
    t: {"itemType": t, "title": "", "creators": [], "abstractNote": "", "date": "", "url": "", "extra": "",
        "tags": [], "collections": [], "relations": {}}
    for t in ("document", "book", "journalArticle", "conferencePaper", "preprint")
}
TEMPLATES["conferencePaper"].update(conferenceName="", proceedingsTitle="", place="", publisher="")

# 与真实 API 一致：只有 PDF、EPUB、网页快照附件可以请求 /children，其他附件返回 400
CHILDREN_TYPES = ("application/pdf", "application/epub+zip", "text/html")
CHILDREN_UNSUPPORTED = "/children can only be called on PDF, EPUB, and snapshot attachments"


def new_key() -> str:
    return "".join(secrets.choice("ABCDEFGHIJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


class ZoteroState:
    def __init__(self) -> None:
        self.version = 1
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}  # Zotero 存储

    def bump(self) -> int:
        self.version += 1
        return self.version


class ZoteroHandler(BaseHTTPRequestHandler):
    state: ZoteroState
    base: str
    prefix = "/users/1"

    def log_message(self, *args) -> None:
        pass

    def send(self, code: int, obj=None, headers=None, raw: bytes | None = None) -> None:
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Last-Modified-Version", str(self.state.version))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def page(self, rows: list, q: dict) -> None:
        start = int(q.get("start", ["0"])[0])
        limit = int(q.get("limit", ["25"])[0])
        self.send(200, rows[start:start + limit], {"Total-Results": len(rows)})

    def wrap_item(self, key: str) -> dict:
        d = self.state.items[key]
        n = sum(1 for x in self.state.items.values() if x.get("parentItem") == key)
        return {"key": key, "version": d["version"], "meta": {"numChildren": n}, "data": d}

    def wrap_col(self, key: str) -> dict:
        d = self.state.collections[key]
        n_items = sum(1 for x in self.state.items.values() if key in x.get("collections", []))
        n_cols = sum(1 for x in self.state.collections.values() if x.get("parentCollection") == key)
        return {"key": key, "version": d["version"], "meta": {"numItems": n_items, "numCollections": n_cols}, "data": d}

    def split(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path.startswith(self.prefix):
            path = path[len(self.prefix):]
        return path, urllib.parse.parse_qs(u.query)

    def do_GET(self) -> None:
        if self.headers.get("Zotero-API-Key") is None and not self.path.startswith("/s3"):
            return self.send(403)
        raw_path = urllib.parse.urlparse(self.path).path
        path, q = self.split()
        if raw_path == "/keys/current":
            return self.send(200, {"userID": 1, "username": "tester", "access": {"user": {"library": True, "write": True}}})
        if raw_path == "/items/new":  # 模板接口在 API 根路径下
            return self.send(200, TEMPLATES[q["itemType"][0]])
        if raw_path.startswith("/s3"):
            return self.send(200, raw=self.state.files[q["k"][0]])
        if not raw_path.startswith(self.prefix):
            return self.send(404)
        if path == "/collections":
            return self.page([self.wrap_col(k) for k in self.state.collections], q)
        m = re.fullmatch(r"/collections/(\w+)(/items(?:/top)?)?", path)
        if m:
            if m[1] not in self.state.collections:
                return self.send(404)
            if not m[2]:
                return self.send(200, self.wrap_col(m[1]))
            rows = [self.wrap_item(k) for k, d in self.state.items.items() if m[1] in d.get("collections", [])]
            return self.page(rows, q)
        if path in ("/items", "/items/top"):
            rows = [self.wrap_item(k) for k, d in self.state.items.items()
                    if path == "/items" or not d.get("parentItem")]
            if "q" in q:
                rows = [r for r in rows if q["q"][0].lower() in r["data"].get("title", "").lower()]
            return self.page(rows, q)
        m = re.fullmatch(r"/items/(\w+)(/children|/file)?", path)
        if m:
            key = m[1]
            if key not in self.state.items:
                return self.send(404)
            if m[2] == "/children":
                d = self.state.items[key]
                if d.get("itemType") == "attachment" and d.get("contentType") not in CHILDREN_TYPES:
                    return self.send(400, raw=CHILDREN_UNSUPPORTED.encode())
                return self.page([self.wrap_item(k) for k, d in self.state.items.items() if d.get("parentItem") == key], q)
            if m[2] == "/file":
                if key not in self.state.files:
                    return self.send(404)
                return self.send(302, headers={"Location": f"{self.base}/s3?k={key}"})
            return self.send(200, self.wrap_item(key))
        self.send(404)

    def do_POST(self) -> None:
        raw_path = urllib.parse.urlparse(self.path).path
        path, _ = self.split()
        body = self.body()
        if raw_path == "/s3":
            key, rest = body.split(b"|", 1)
            self.state.files[key.decode()] = rest.rsplit(b"|END", 1)[0]
            return self.send(201)
        if path in ("/items", "/collections"):
            store = self.state.items if path == "/items" else self.state.collections
            result = {"success": {}, "successful": {}, "unchanged": {}, "failed": {}}
            for i, obj in enumerate(json.loads(body)):
                key = new_key()
                obj.update(key=key, version=self.state.bump())
                store[key] = obj
                result["success"][str(i)] = key
            return self.send(200, result)
        m = re.fullmatch(r"/items/(\w+)/file", path)
        if m:
            key = m[1]
            form = urllib.parse.parse_qs(body.decode())
            if "upload" in form:
                self.state.items[key]["md5"] = hashlib.md5(self.state.files[key]).hexdigest()
                self.state.items[key]["version"] = self.state.bump()
                return self.send(204)
            return self.send(200, {"url": f"{self.base}/s3", "contentType": "application/octet-stream",
                                   "prefix": key + "|", "suffix": "|END", "uploadKey": "u1"})
        self.send(404)

    def _versioned(self):
        path, _ = self.split()
        m = re.fullmatch(r"/(items|collections)/(\w+)", path)
        store = self.state.items if m[1] == "items" else self.state.collections
        obj = store.get(m[2])
        if obj is None:
            self.send(404)
            return None, None, None
        if int(self.headers["If-Unmodified-Since-Version"]) != obj["version"]:
            self.send(412, raw=b"version conflict")
            return None, None, None
        return store, m[2], obj

    def do_PATCH(self) -> None:
        store, key, obj = self._versioned()
        if obj is None:
            return
        obj.update(json.loads(self.body()))
        obj["version"] = self.state.bump()
        self.send(204)

    def do_DELETE(self) -> None:
        store, key, obj = self._versioned()
        if obj is None:
            return
        del store[key]
        if store is self.state.collections:  # 与服务器行为一致：移除成员关系
            for item in self.state.items.values():
                if key in item.get("collections", []):
                    item["collections"].remove(key)
        self.state.bump()
        self.send(204)


class WebDAVHandler(BaseHTTPRequestHandler):
    store: dict[str, bytes]
    dirs: set[str]
    auth: str

    def log_message(self, *args) -> None:
        pass

    def reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self) -> bool:
        if self.headers.get("Authorization") != self.auth:
            self.reply(401)
            return False
        return True

    def do_PROPFIND(self) -> None:
        if self.authed():
            self.reply(207 if self.path in self.dirs else 404)

    def do_PUT(self) -> None:
        if self.authed():
            self.store[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(201)

    def do_GET(self) -> None:
        if self.authed():
            body = self.store.get(self.path)
            self.reply(200 if body is not None else 404, body or b"")


def start(user: str = "u", password: str = "pw"):
    """启动两个服务器，返回 (zotero_base, webdav_base, zotero_state, webdav_store, stop)。"""
    import base64

    state = ZoteroState()
    z_srv = ThreadingHTTPServer(("127.0.0.1", 0), ZoteroHandler)
    z_base = f"http://127.0.0.1:{z_srv.server_port}"
    ZoteroHandler.state, ZoteroHandler.base = state, z_base

    dav_store: dict[str, bytes] = {"/dav/zotero/lastsync.txt": b"1"}
    d_srv = ThreadingHTTPServer(("127.0.0.1", 0), WebDAVHandler)
    WebDAVHandler.store, WebDAVHandler.dirs = dav_store, {"/dav/zotero/"}
    WebDAVHandler.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    d_base = f"http://127.0.0.1:{d_srv.server_port}/dav/"

    for srv in (z_srv, d_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop() -> None:
        z_srv.shutdown()
        d_srv.shutdown()

    return z_base, d_base, state, dav_store, stop
