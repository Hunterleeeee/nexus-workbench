# Security

## 报告漏洞

请勿在公开 issue 中披露安全漏洞。请通过私有渠道报告（例如维护者的邮箱或私有仓库 issue），并附上复现步骤、影响范围和建议修复。

## 安全设计

- **凭据不入库**：`.env`、`data/*.db`、`data/llm_settings.json` 均被 `.gitignore` 排除；`.env.example` 只含占位符。
- **LLM 密钥**：全局 LLM 配置保存在 `data/llm_settings.json`（尝试设置为仅当前用户可读），支持多 Provider 主/fallback 与 failover。
- **云开发**：只接受白名单工作区 + 固定配方命令，`shell=False`，不支持任意 shell、远程 SSH 或自动部署；构建必须先进入审批中心。
- **浏览器项目**：只允许访问公开 URL；工作台自身（`WORKBENCH_PUBLIC_URL`）永远不能成为目标。
- **飞书回调**：公开回调必须配置 `VERIFY_TOKEN` 或 `ENCRYPT_KEY` 至少一项，否则拒绝处理；按事件 ID 做幂等。
- **服务器监控**：只读 SSH 探测；日志读取和重启需要服务器侧人工确认。
- **API 纵深防御**：可配置 `WORKBENCH_API_TOKEN`，除公开路径外所有 API 需要 `X-Workbench-Token` 头或 Cookie。

## 部署注意事项

- 生产环境务必启用 HTTPS（Nginx 反向代理 + 认证）。
- Web Push 使用 VAPID 密钥（`python3 deploy/generate-vapid-keys.py` 生成），私钥默认写 `data/vapid_private.pem`（权限 600），换密钥会使已有订阅失效。
- 定期备份：`python3 backup.py backup`。
