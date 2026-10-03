"""端到端测试：在模拟的 Zotero API 与 WebDAV 上运行 zoteroctl 命令。

运行：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mock_servers  # noqa: E402

CLI = Path(__file__).resolve().parent.parent / "zoteroctl.py"


class CLITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.z_base, cls.d_base, cls.state, cls.dav, cls.stop = mock_servers.start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name) / "cfg"
        cls.home.mkdir()
        (cls.home / "config.json").write_text(json.dumps({"api_key": "k", "library_type": "user", "library_id": "1"}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stop()
        cls.tmp.cleanup()

    def run_cli(self, *args: str, stdin: str = "", ok: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "ZOTEROCTL_API_BASE": self.z_base, "ZOTEROCTL_HOME": str(self.home)}
        for k in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE"):
            env.pop(k, None)
        p = subprocess.run([sys.executable, str(CLI), *args], input=stdin, capture_output=True, text=True,
                           env=env, cwd=self.tmp.name)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def json_of(self, *args: str) -> object:
        return json.loads(self.run_cli(*args).stdout)

    def file(self, name: str, content: str | bytes) -> str:
        path = Path(self.tmp.name) / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return str(path)

    def new_item(self, title: str, collections: list[str] | None = None) -> str:
        f = self.file(f"{title}.json", json.dumps({"itemType": "document", "title": title,
                                                    "collections": collections or []}))
        return self.json_of("add", f, "--apply")["created"]["0"]

    # ------------------------------------------------------------------ 新增与预览

    def test_preview_does_not_write(self) -> None:
        before = len(self.state.items)
        f = self.file("p.json", json.dumps({"itemType": "document", "title": "Preview only"}))
        out = self.run_cli("add", f).stdout
        self.assertIn("[预览]", out)
        self.assertEqual(len(self.state.items), before)

    def test_add_sets_inbox_and_rejects_unknown_field(self) -> None:
        key = self.new_item("Inbox item")
        self.assertIn({"tag": "status:inbox"}, self.state.items[key]["tags"])
        bad = self.file("bad.json", json.dumps({"itemType": "document", "title": "x", "noSuchField": 1}))
        self.assertIn("不支持的字段", self.run_cli("add", bad, ok=False).stderr)

    # ------------------------------------------------------------------ 修改

    def test_update_tags_and_collections(self) -> None:
        col = self.json_of("collection", "主题A", "--apply")["created_collection"]
        key = self.new_item("Tag item")
        self.run_cli("update", key, "--tag", "status:to-read", "--collection", col, "--apply")
        data = self.state.items[key]
        self.assertIn(col, data["collections"])
        self.assertIn({"tag": "status:to-read"}, data["tags"])
        self.run_cli("update", key, "--remove-tag", "status:inbox", "--apply")
        self.assertNotIn({"tag": "status:inbox"}, self.state.items[key]["tags"])
        forbidden = self.file("f.json", json.dumps({"tags": []}))
        self.run_cli("update", key, "--fields", forbidden, ok=False)

    def test_version_conflict_reported(self) -> None:
        key = self.new_item("Conflict item")
        fields = self.file("t.json", json.dumps({"title": "new"}))
        original = mock_servers.ZoteroHandler.do_PATCH

        def bump_then_patch(handler):  # 模拟读取后被其他客户端修改
            handler.state.items[key]["version"] = handler.state.bump()
            original(handler)

        mock_servers.ZoteroHandler.do_PATCH = bump_then_patch
        try:
            self.assertIn("412", self.run_cli("update", key, "--fields", fields, "--apply", ok=False).stderr)
        finally:
            mock_servers.ZoteroHandler.do_PATCH = original

    # ------------------------------------------------------------------ 删除

    def test_delete_refuses_parent_with_children_and_needs_confirm(self) -> None:
        key = self.new_item("Parent")
        note = self.file("n.html", "<p>idx</p>")
        child = self.json_of("note", key, note, "--apply")["created_note"]
        self.assertIn("子对象", self.run_cli("delete", key, "--confirm-key", key, "--apply", ok=False).stderr)
        self.run_cli("delete", child, "--confirm-key", "WRONG", "--apply", ok=False)
        self.run_cli("delete", child, "--confirm-key", child, "--apply")
        self.assertNotIn(child, self.state.items)

    def standalone_attachment(self, filename: str, content_type: str) -> str:
        """直接在模拟库里放一个没有父条目的附件（CLI 不提供新建独立附件的命令）。"""
        key = mock_servers.new_key()
        self.state.items[key] = {"key": key, "version": self.state.bump(), "itemType": "attachment",
                                 "linkMode": "imported_file", "title": filename, "contentType": content_type,
                                 "filename": filename, "tags": [], "relations": {}, "deleted": 1}
        return key

    # 真实 API 对非 PDF/EPUB/快照附件的 /children 请求返回 400，这不应导致 get/delete 失败
    def test_get_non_pdf_standalone_attachment(self) -> None:
        key = self.standalone_attachment("plugin.xpi", "application/x-xpinstall")
        got = self.json_of("get", key)
        self.assertEqual(got["key"], key)
        self.assertEqual(got["children"], [])

    def test_delete_non_pdf_standalone_attachment(self) -> None:
        key = self.standalone_attachment("plugin.xpi", "application/x-xpinstall")
        self.assertIn("[预览]", self.run_cli("delete", key, "--confirm-key", key).stdout)
        self.assertIn(key, self.state.items)
        res = self.json_of("delete", key, "--confirm-key", key, "--apply")
        self.assertTrue(res["verified_absent"])
        self.assertNotIn(key, self.state.items)

    def test_children_other_400_still_fails(self) -> None:
        # 只放过上面那一种 400；其他 400 照常报错，delete 不执行
        key = self.standalone_attachment("paper.pdf", "application/pdf")
        original = mock_servers.ZoteroHandler.do_GET

        def bad_children(handler):
            if handler.path.split("?")[0].endswith(f"/items/{key}/children"):
                return handler.send(400, raw=b"Invalid 'includeTrashed' value")
            original(handler)

        mock_servers.ZoteroHandler.do_GET = bad_children
        try:
            p = self.run_cli("delete", key, "--confirm-key", key, "--apply", ok=False)
            self.assertIn("400", p.stderr)
            self.run_cli("get", key, ok=False)
        finally:
            mock_servers.ZoteroHandler.do_GET = original
        self.assertIn(key, self.state.items)

    def test_delete_collection_tree_bottom_up(self) -> None:
        root = self.json_of("collection", "Root", "--apply")["created_collection"]
        mid = self.json_of("collection", "Mid", "--parent", root, "--apply")["created_collection"]
        leaf = self.json_of("collection", "Leaf", "--parent", mid, "--apply")["created_collection"]
        self.run_cli("delete-collection", root, "--confirm-key", root, "--apply", ok=False)
        self.run_cli("delete-collection", root, "--confirm-key", root, "--include-subcollections", "--apply")
        for k in (root, mid, leaf):
            self.assertNotIn(k, self.state.collections)

    # ------------------------------------------------------------------ 文件

    def test_attach_and_fetch_via_zotero_storage(self) -> None:
        key = self.new_item("ZFS paper")
        pdf = self.file("zfs.pdf", b"%PDF-1.4 zfs")
        out = self.json_of("attach", key, pdf, "--title", "PDF", "--storage", "zotero", "--apply")
        att = out["attachment"]["key"]
        dest = str(Path(self.tmp.name) / "zfs-back.pdf")
        got = self.json_of("fetch", att, dest, "--storage", "zotero")
        self.assertTrue(got["md5_matches_record"])
        self.run_cli("fetch", att, dest, "--storage", "zotero", ok=False)  # 目标已存在

    def test_attach_rejects_non_pdf(self) -> None:
        key = self.new_item("Not pdf")
        self.run_cli("attach", key, self.file("x.pdf", b"hello"), "--apply", ok=False)

    def test_webdav_configure_attach_fetch(self) -> None:
        # 粘贴时混入的“括号粘贴”标记应被去掉；地址带不带 zotero/ 都能找到同一目录
        for url in (self.d_base, self.d_base + "zotero/"):
            self.run_cli("configure-webdav", "--url", url, "--user", "u", "--password-stdin", stdin="\x1b[200~pw\x1b[201~\n")
            cfg = json.loads((self.home / "config.json").read_text())
            self.assertEqual(cfg["webdav_dir"], self.d_base + "zotero/")
        p = self.run_cli("configure-webdav", "--url", self.d_base, "--user", "u", "--password-stdin", stdin="u\n", ok=False)
        self.assertIn("与用户名完全相同", p.stderr)
        p = self.run_cli("configure-webdav", "--url", self.d_base, "--user", "u", "--password-stdin", stdin="wrong\n", ok=False)
        self.assertIn("401", p.stderr)

        key = self.new_item("WebDAV paper")
        pdf = self.file("dav.pdf", b"%PDF-1.4 webdav")
        out = self.json_of("attach", key, pdf, "--title", "PDF", "--apply")
        self.assertTrue(out["verified"])
        att = out["attachment"]["key"]
        self.assertIn(f"/dav/zotero/{att}.zip", self.dav)
        self.assertIn(hashlib.md5(b"%PDF-1.4 webdav").hexdigest().encode(), self.dav[f"/dav/zotero/{att}.prop"])
        got = self.json_of("fetch", att, str(Path(self.tmp.name) / "dav-back.pdf"))
        self.assertEqual(got["source"], "webdav")
        self.assertTrue(got["md5_matches_record"])


if __name__ == "__main__":
    unittest.main()
