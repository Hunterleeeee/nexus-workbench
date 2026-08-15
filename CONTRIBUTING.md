# Contributing

感谢你考虑为 Workbench 贡献代码。

## 开发环境

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 测试

所有改动必须保持测试全绿：

```bash
.venv/bin/python -m pytest tests/ -q
```

新增功能请同步补充测试。测试集是全量运行的发布闸门，不允许 `--deselect` 或跳过环境敏感用例。

## 代码风格

- Python 3.11+，`from __future__ import annotations` 已普遍使用。
- 保持 `app.py` 精简：领域逻辑按模块放到 `app_pkg/`。
- 模块间互调或被测试 patch 的函数使用 `_app_call(fn_name, ...)` 运行时转发，避免绑定导入。
- 服务器部署环境是 Python 3.11：本地 3.13/3.14 能过的 PEP 701 f-string 嵌套引号在服务器会 SyntaxError，提交前用 `ast.parse(src, feature_version=(3, 11))` 预检。

## 提交信息

沿用现有约定：`版本号：改动摘要`（如 `0.3.212：修复 xxx`）。

## License

贡献即代表同意以 [MIT](LICENSE) 授权你的代码。
