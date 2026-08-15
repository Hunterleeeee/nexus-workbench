# Changelog

NEXUS 的版本记录。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [0.3.213] - 2026-08-15

### Changed
- **品牌升级：Workbench → NEXUS**。更新 PWA Manifest、页面标题、顶栏品牌、桌面壳标题、`/api/meta` 与 Service Worker 缓存名。
- 重新生成图标资源（PNG/SVG/icns），更新 favicon 与 PWA 图标声明。
- 重建 Service Worker 缓存清单，补齐所有页面、项目路由与静态资源路径。

## [0.3.212] - 2026-08-15

### Added
- 新增 MIT LICENSE、README、CONTRIBUTING、SECURITY 文档。
- `projects.json` 缺失时自动回退到 `projects.open-source.json`，首次启动开箱即有项目入口。
- 服务器监控探测服务名可配置（`WORKBENCH_APP_SERVICE_NAME`）。
- 依赖清单增加 major 版本上限约束。

### Changed
- 移除个人知识库内容跟踪，`knowledge-base/` 仅保留目录说明。
- `requirements.txt` 与 `requirements.lock` 职责划分明确（开发宽松 / 部署可复现）。
- 内部部署脚本与文档移出仓库，通用工具移至 `scripts/`。

## [0.3.211] - 2026-08-15

### Fixed
- 修复拆分遗留的 15 个缺陷（导入缺失、转发遗漏、插拔过滤）。
- 补全项目插拔链路（禁用项目页面与业务 API 统一 404）。

## [0.3.210] - 2026-08-14

### Added
- 项目可插拔：`projects.json` 支持 `enabled` 字段，禁用后首页入口、子 Agent 工具与调度路由统一过滤。
- 提供 `projects.open-source.json` 开源默认模板（爬虫入口默认关闭）。

## [0.3.202] - 2026-08-13

### Changed
- 完成巨型单文件应用按领域拆分：`app.py` 拆分为 `app_pkg/` 模块（35 个领域模块）。

[0.3.213]: https://github.com/YOUR_ORG/nexus/releases/tag/v0.3.213
[0.3.212]: https://github.com/YOUR_ORG/nexus/releases/tag/v0.3.212
[0.3.211]: https://github.com/YOUR_ORG/nexus/releases/tag/v0.3.211
[0.3.210]: https://github.com/YOUR_ORG/nexus/releases/tag/v0.3.210
[0.3.202]: https://github.com/YOUR_ORG/nexus/releases/tag/v0.3.202
