# 本仓库的 agent 约定

- 用 zoteroctl 操作用户的 Zotero 文献库时，遵守 [docs/agent-guide.md](docs/agent-guide.md)。
- 修改代码后运行 `python3 -m unittest discover -s tests`，全部通过后再提交。测试只连本地模拟服务器，不要连真实账号。
- `zoteroctl.py` 只用 Python 标准库，不引入第三方依赖。
- 新增写操作必须默认只预览、加 `--apply` 才执行。写入时带 `If-Unmodified-Since-Version`；删除类操作要有 `--confirm-key`。
- 新功能要在 `tests/mock_servers.py` 中补上对应的模拟接口，并在 `tests/test_cli.py` 中补测试。
- 面向用户的提示和文档使用中文。
