# 开源发布检查清单

> 本文档面向维护者：每次公开发布前按此清单核对。

## 发布前必查

- [ ] `git status` 干净，无未提交的个人文件
- [ ] `git ls-files | grep -E 'knowledge-base/|data/|outputs/|\.env$'` 只命中预期白名单
- [ ] 全仓库敏感信息扫描为空：
  ```bash
  git grep -n -E 'zhuliren|124\.223\.56|hotel-server|hotel_deploy|/Users/lifenghe' $(git rev-list --all)
  ```
  （`workbench.example.dev` 是占位符，允许出现）
- [ ] `.gitignore` 覆盖：`.env`、`data/`、`outputs/`、`knowledge-base/*`（除 README）、`projects.json`、`desktop/dist`
- [ ] 打包发布时排除运行数据（参考下方命令）
- [ ] Python 测试全绿：`.venv/bin/python -m pytest tests/ -q`
- [ ] 前端验证：`cd desktop && npm ci && npm run verify`
- [ ] 版本号四同步：`VERSION` / `desktop/package.json` / 各页面显示版本 / `sw.js` 缓存名
- [ ] `requirements.txt` 上限约束存在（避免自动安装拉到大版本）

## 打包发布（排除运行数据）

```bash
tar -czf workbench-open-source.tar.gz \
  --exclude=.git --exclude=.venv --exclude=.env \
  --exclude=data --exclude=outputs --exclude=knowledge-base \
  --exclude=desktop/node_modules --exclude=desktop/dist \
  --exclude=projects.json --exclude='._*' .
```

## 历史清洗（如首次公开旧仓库）

```bash
pip install git-filter-repo
# 1. 替换敏感文本
git filter-repo --force --replace-text /tmp/replacements.txt
# 2. 删除敏感路径（含历史）
git filter-repo --force --invert-paths --path deploy/ --path companion/ --path knowledge-base/
# 3. 验证
git log --all --name-only --format='' | grep -aE 'deploy/|knowledge-base' | sort -u
```

> ⚠️ filter-repo 会重写所有 commit hash 并移除 remote；先 `git bundle` 备份，再操作。
