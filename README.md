# zoteroctl

用命令行（以及 AI 编程助手）安全地管理 Zotero 文献库：查询、新增条目、上传 PDF、写笔记、整理分类和标签、删除。

*A safe, dependency-free command-line tool for managing a Zotero library through the Zotero Web API, with WebDAV file support. Every write is a dry run unless you pass `--apply`.*

- **只用 Python 标准库**，单文件，Python 3.9 及以上即可运行。
- **写操作默认只预览**：不加 `--apply` 不会改动文献库；删除还要用 `--confirm-key` 再写一遍目标 key。
- **带版本号写入**：文献库在读取之后被别处改过，写入会失败（HTTP 412），不会覆盖别人的修改。
- **支持两种文件存储**：Zotero 官方存储，以及 WebDAV（坚果云、Seafile 等网盘）。上传后会重新下载并比对 md5。
- **不需要安装 Zotero 桌面端**：直接操作云端文献库，桌面端同步后可见。

## 安装

```bash
git clone <仓库地址> zoteroctl
cd zoteroctl
./zoteroctl --help              # 直接运行
# 或安装成命令：pip install .   然后使用 zoteroctl
```

## 配置

### 1. Zotero API key

在 <https://www.zotero.org/settings/keys> 新建 key，勾选 “Allow library access”、“Allow notes access” 和 “Allow write access”。然后运行：

```bash
zoteroctl configure          # 按提示粘贴 key，输入时不显示
zoteroctl status             # 显示用户名、权限、条目数，确认配置成功
```

群组库：`zoteroctl configure --library-type group --library-id <群组ID>`。

### 2. 配置保存在哪里

zoteroctl 按以下顺序查找配置目录：

1. 环境变量 `ZOTEROCTL_HOME` 指定的目录；
2. 从当前目录向上找到的第一个 `.zoteroctl/` 目录（与 git 查找 `.git` 的方式相同）；
3. `~/.config/zoteroctl/`。

只想让配置在某个工作目录内生效，就在该目录运行 `zoteroctl configure --here`。在这个目录及其子目录里运行时使用这份配置，在别处运行时找不到它。配置文件权限为 600，里面有 API key 和 WebDAV 密码，不要提交到 git。

临时覆盖凭据可以用环境变量 `ZOTERO_API_KEY`、`ZOTERO_LIBRARY_ID`、`ZOTERO_LIBRARY_TYPE`。

### 3. WebDAV（可选）

如果 Zotero 桌面端的“文件同步”选的是 WebDAV，那么 PDF 存放在你的 WebDAV 网盘里，而不在 Zotero 官方存储中。这时需要配置 WebDAV：

```bash
zoteroctl configure-webdav --url https://dav.jianguoyun.com/dav/
```

- **地址**：填写 Zotero 设置里“网址”输入框的内容。Zotero 会在后面自动加 `/zotero/`，zoteroctl 也一样。如果你照抄了带 `/zotero/` 的完整地址，也能识别：zoteroctl 会检查两个可能的目录，选出含有 `lastsync.txt`（即 Zotero 同步过）的那个。
- **用户名和密码**：与 Zotero 设置里的完全一致。坚果云要用“第三方应用密码”；用统一身份认证登录的 Seafile 网盘，通常要用在网盘设置里另外设置的 WebDAV 密码。
- 配置成功后，`status` 显示 `file_storage: webdav: ...`。之后 `attach` 和 `fetch` 默认使用 WebDAV；要访问官方存储，加 `--storage zotero`。

WebDAV 上传的过程：先在文献库里新建附件记录，再上传 `<附件KEY>.zip` 和 `<附件KEY>.prop`，接着把 md5 和修改时间写回附件记录，最后重新下载并比对 md5。桌面端下次同步时，会从 WebDAV 下载这个文件。

## 命令一览

| 命令 | 作用 |
|---|---|
| `status` | 账号、权限、库版本、条目和分类数量、文件存储方式 |
| `collections` | 列出分类：key、名称、父分类、条目数 |
| `list [--query Q] [--collection K] [--tag T] [--all] [--full]` | 列出顶层条目；`--all` 包含附件、笔记和批注，`--full` 输出完整 JSON |
| `get KEY` | 查看条目详情及其子对象；也可以查分类 |
| `fetch ATTKEY DEST` | 下载附件；目标文件已存在时拒绝执行，下载后报告 md5 是否与记录一致 |
| `snapshot [--out F]` | 导出全库元数据快照（不含 PDF 文件），用于批量修改前备份 |
| `add ITEM.json` | 从 JSON 新增父条目（每批最多 10 条）。会检查字段名和分类 key，按题名提示可能重复的条目，并自动加上 `status:inbox` 标签 |
| `attach ITEMKEY FILE.pdf --title T` | 上传 PDF 附件并校验 |
| `note ITEMKEY FILE.html` | 新建一条子笔记（每次都新建，不会更新已有笔记） |
| `collection NAME [--parent K]` | 新建分类 |
| `update KEY [--fields F.json] [--tag T] [--remove-tag T] [--collection K]` | 修改字段；添加或移除标签；加入分类 |
| `move-collection KEY NEWPARENT\|ROOT` | 移动分类；`ROOT` 表示移到顶层 |
| `move-child KEY NEWPARENT` | 把附件或笔记挂到另一个父条目下 |
| `delete KEY --confirm-key KEY` | 永久删除条目、附件或笔记。目标还有子对象时拒绝执行 |
| `delete-collection KEY --confirm-key KEY [--include-subcollections]` | 删除分类（不删除其中的文献）。删除整棵分类树时从最底层往上逐个删除，并逐个确认 |

所有写命令都要加 `--apply` 才真正执行。示例：

```bash
zoteroctl list --query 'reinforcement'
zoteroctl add paper.json                 # 预览
zoteroctl add paper.json --apply         # 执行
zoteroctl attach ABCD1234 paper.pdf --title 'Preprint PDF' --apply
zoteroctl update ABCD1234 --tag status:reading --remove-tag status:inbox --apply
```

`add` 的输入是 Zotero Web API 格式的 JSON，例如：

```json
{
  "itemType": "preprint",
  "title": "Paper title",
  "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
  "date": "2026-07-01",
  "url": "https://arxiv.org/abs/2607.00001",
  "archiveID": "arXiv:2607.00001",
  "collections": ["COLLKEY1"],
  "tags": [{"tag": "topic:example"}]
}
```

`update --fields` 不能修改 `key`、`version`、`itemType`、`dateAdded`、`dateModified`、`tags`、`collections`、`parentItem`。标签用 `--tag`/`--remove-tag` 修改，分类用 `--collection` 修改，父条目用 `move-child` 修改。

## 让 AI 助手使用 zoteroctl

[docs/agent-guide.md](docs/agent-guide.md) 是给 Claude Code、Codex 等 AI 编程助手看的操作规范，内容包括：先读取现状再改动、先预览、分批执行、执行后回读核对，以及删除前要保全哪些内容。把它放进你的 `CLAUDE.md` 或 `AGENTS.md`，或者在里面引用它。

## 常见问题

**Zotero 里“验证服务器”成功，`configure-webdav` 却报 401。**
基本可以确定是密码输入有误。Zotero 的密码框即使点开眼睛图标显示了明文，复制也常常不生效，剪贴板里还是之前复制的内容，比如用户名。报错信息会显示本次收到的密码长度，和你的真实密码位数比一比就能判断。看清密码后用键盘手动输入。输入的密码与用户名完全相同时，zoteroctl 会直接提示。

**粘贴密码时多出几个字符。**
有些终端在粘贴内容的前后自动加 `ESC[200~`、`ESC[201~` 这样的“括号粘贴”标记。zoteroctl 会自动去掉这些标记和其他控制字符，并提示前后的长度变化。

**`configure-webdav` 提示“目录存在但没有 lastsync.txt”。**
地址和账号都对，但 Zotero 可能还没往这里同步过文件，或者这个网盘不保存这个文件。如果目录里已经有很多 `.zip`/`.prop` 文件，可以忽略这条提示。

**`fetch` 从 Zotero 官方存储下载时返回 404。**
改用 WebDAV 之后加入的附件，文件都在 WebDAV 上，官方存储里没有。配置 WebDAV 后再下载。所有用 zoteroctl 往同一个文献库上传文件的电脑，都要配置成同一种存储方式，否则上传的文件在桌面端打不开。

**群组文库。**
Zotero 的群组文库只能使用官方存储，不支持 WebDAV。

**名为“Addon Item”的条目和大量奇怪的笔记。**
这类条目可能是 Zotero 插件存的数据，例如阅读时长统计。删除前先确认，删掉后插件的数据无法恢复。

**限流。**
zoteroctl 会遵守 Zotero API 返回的 `Backoff` 和 `Retry-After` 头。网盘的 WebDAV 也有各自的限制（例如坚果云免费版每月上传流量有限），批量上传前先估算总大小。

## 局限

- 没有原位替换 PDF 的命令：先上传新附件并核验，再删除旧附件。旧附件上的批注不会自动转移。
- 没有重命名分类、把条目移出分类的命令。
- `delete` 调用 API 的 DELETE，属于永久删除，不经过回收站；删除附件记录后，WebDAV 或官方存储里的文件不会被清理。
- 不解析 DOI 或 BibTeX：`add` 要求输入完整的条目 JSON。

## 开发

```bash
python3 -m unittest discover -s tests
```

测试会启动本地的模拟 Zotero API 和模拟 WebDAV 服务器，不需要真实账号。可以用环境变量 `ZOTEROCTL_API_BASE` 让 zoteroctl 连接其他 API 地址。

## 许可证

待定。
