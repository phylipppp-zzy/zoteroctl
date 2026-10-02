# zoteroctl

English | [中文](README.zh-CN.md)

A safe, dependency-free command-line tool for managing a Zotero library through the Zotero Web API — built to be driven by you or by an AI coding assistant (Claude Code, Codex, …). Query items, add papers, upload PDFs, write notes, organize collections and tags, and delete things, with every write previewed first.

- **Standard library only.** One Python file, Python 3.9+.
- **Dry run by default.** Nothing changes until you add `--apply`. Deletions also require `--confirm-key <same key>`.
- **Version-checked writes.** If the library changed since it was read, the write fails with HTTP 412 instead of overwriting someone else's edit.
- **Two file backends.** Zotero Storage and WebDAV (Nextcloud, Seafile, Jianguoyun, …). Uploads are downloaded back and checked by MD5.
- **No Zotero desktop needed.** It talks to your cloud library; the desktop app picks up the changes on its next sync.

> The command-line messages and the agent guide are currently in Chinese. The commands and options are the same in any language.

## Install

```bash
git clone https://github.com/phylipppp-zzy/zoteroctl.git
cd zoteroctl
./zoteroctl --help        # run in place
# or: pip install .        # installs a `zoteroctl` command
```

## Configure

### 1. Zotero API key

Create a key at <https://www.zotero.org/settings/keys> with library, notes and write access, then:

```bash
zoteroctl configure      # paste the key when prompted (input is hidden)
zoteroctl status         # shows user, permissions and item counts
```

For a group library: `zoteroctl configure --library-type group --library-id <groupID>`.

### 2. Where the configuration lives

zoteroctl looks for its configuration directory in this order:

1. `$ZOTEROCTL_HOME`
2. the first `.zoteroctl/` directory found walking up from the current directory (like git looks for `.git`)
3. `~/.config/zoteroctl/`

To keep a configuration scoped to one working directory, run `zoteroctl configure --here` there. It is then used only when you run zoteroctl inside that directory tree. The config file is created with mode 600 and contains your API key and WebDAV password — never commit it. `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID` and `ZOTERO_LIBRARY_TYPE` override it temporarily.

### 3. WebDAV (optional)

If Zotero desktop syncs files via WebDAV, your PDFs live on your WebDAV server, not in Zotero Storage. Configure it:

```bash
zoteroctl configure-webdav --url https://dav.example.com/dav/
```

- **URL:** what you typed in the URL field of Zotero's sync settings. Zotero appends `/zotero/`, and so does zoteroctl. If you paste the full path including `/zotero/`, that works too: zoteroctl checks both candidate directories and picks the one containing `lastsync.txt`, i.e. the one Zotero actually syncs to.
- **Username / password:** exactly the ones in Zotero's settings. Some providers require an app-specific password (Jianguoyun) or a separate WebDAV password (Seafile behind single sign-on).
- After this, `status` shows `file_storage: webdav: ...`, and `attach` / `fetch` use WebDAV by default. Pass `--storage zotero` to use Zotero Storage instead.

A WebDAV upload creates the attachment item, uploads `<KEY>.zip` and `<KEY>.prop`, writes the MD5 and modification time back to the item, then downloads the file again to verify it. Zotero desktop fetches the file from WebDAV on its next sync.

## Commands

| Command | What it does |
|---|---|
| `status` | Account, permissions, library version, counts, file backend |
| `collections` | Collections with key, name, parent, item count |
| `list [--query Q] [--collection K] [--tag T] [--all] [--full]` | Top-level items; `--all` includes attachments, notes and annotations; `--full` prints the raw API JSON |
| `get KEY` | One item and its children (also accepts a collection key) |
| `fetch ATTKEY DEST` | Download an attachment. Refuses to overwrite an existing file; reports whether the MD5 matches the record |
| `snapshot [--out F]` | Export all metadata (no files) as a backup before bulk edits |
| `add ITEM.json` | Add parent items from Zotero API JSON (max 10 per batch). Checks field names and collection keys, flags possible duplicates by title, adds `status:inbox` |
| `attach ITEMKEY FILE.pdf --title T` | Upload a PDF attachment and verify it |
| `note ITEMKEY FILE.html` | Create a child note (always creates; never updates) |
| `collection NAME [--parent K]` | Create a collection |
| `update KEY [--fields F.json] [--tag T] [--remove-tag T] [--collection K]` | Edit fields; add or remove tags; add to a collection |
| `move-collection KEY NEWPARENT\|ROOT` | Move a collection (`ROOT` = top level) |
| `move-child KEY NEWPARENT` | Re-parent an attachment or note |
| `delete KEY --confirm-key KEY` | Permanently delete an item, attachment or note. Refuses while it still has children |
| `delete-collection KEY --confirm-key KEY [--include-subcollections]` | Delete a collection (its items are kept). Subtrees are deleted bottom-up, one collection at a time, and each deletion is verified |

Every write command needs `--apply` to take effect:

```bash
zoteroctl list --query 'reinforcement'
zoteroctl add paper.json                 # preview
zoteroctl add paper.json --apply         # write
zoteroctl attach ABCD1234 paper.pdf --title 'Preprint PDF' --apply
zoteroctl update ABCD1234 --tag status:reading --remove-tag status:inbox --apply
```

`add` takes Zotero Web API JSON, for example:

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

`update --fields` cannot change `key`, `version`, `itemType`, `dateAdded`, `dateModified`, `tags`, `collections` or `parentItem`. Use `--tag`/`--remove-tag`, `--collection` and `move-child` for those.

## Using it from an AI assistant

[docs/agent-guide.md](docs/agent-guide.md) (in Chinese) is a set of operating rules for AI assistants: read the current state before changing anything, preview first, work in small batches, read back after writing, and keep attachments, notes and annotations safe before deleting. Copy it into your `CLAUDE.md` or `AGENTS.md`, or link to it from there.

## Troubleshooting

**Zotero's "Verify Server" succeeds but `configure-webdav` returns 401.**
Almost always a password-entry problem. Copying from Zotero's password field often silently fails, even with the password revealed, so the clipboard still holds whatever you copied before — often the username. The error message shows the length of the password it received; compare that with your real password. Type the password by hand. zoteroctl also stops early if the password equals the username.

**Pasted passwords gain extra characters.**
Some terminals wrap pasted text in bracketed-paste markers (`ESC[200~` … `ESC[201~`). zoteroctl strips these and other control characters, and tells you when it did.

**"Directory exists but has no lastsync.txt".**
The URL and credentials are fine, but Zotero may not have synced files there yet, or your server does not keep that file. If the directory already holds many `.zip`/`.prop` files, you can ignore the warning.

**`fetch` from Zotero Storage returns 404.**
After you switch to WebDAV, files for new attachments exist only on WebDAV. Configure WebDAV first. Every machine that uploads files to the same library with zoteroctl must use the same backend; otherwise the desktop app cannot open those files.

**Group libraries.**
Zotero group libraries can only store files in Zotero Storage; WebDAV is not supported for them.

**An item called "Addon Item" with dozens of JSON-like notes.**
Such items usually hold plugin data, for example reading-time statistics. Ask before deleting; the plugin's data cannot be recovered.

**Rate limits.**
zoteroctl honours the Zotero API's `Backoff` and `Retry-After` headers. WebDAV providers have their own limits (for example, Jianguoyun's free plan caps monthly upload traffic), so estimate the total size before bulk uploads.

## Limitations

- No in-place PDF replacement: upload and verify the new attachment, then delete the old one. Annotations on the old file are not carried over.
- No commands to rename a collection or to remove an item from a collection.
- `delete` uses the API's DELETE: it is permanent and skips the trash. Deleting an attachment item does not remove its file from WebDAV or Zotero Storage.
- No DOI or BibTeX resolution: `add` expects complete item JSON.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests start local mock servers for the Zotero API and WebDAV, so no real account is needed. `ZOTEROCTL_API_BASE` points zoteroctl at a different API base URL.

## License

[MIT](LICENSE)
