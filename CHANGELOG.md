# Changelog

## [0.2.0] - 2026-09-20

Page Bundle 与媒体管理。

- 新建草稿默认使用 `content/posts/<slug>/index.md` Page Bundle。
- 新增 `hugo_upload_media`、`hugo_list_media` 和 `hugo_delete_media`。
- 新增 `hugo_migrate_post_bundle`，可逐篇迁移旧 flat Markdown 文章。
- 媒体上传支持 JPEG、PNG、WebP、GIF，校验文件签名、扩展名和大小。
- 媒体使用独立 revision，删除进入 `trash/media/`。
- 文章查询、读取、更新、发布和删除同时兼容 flat post 与 Page Bundle。

## [0.1.1] - 2026-09-20

文档与开发体验更新，不改变 MCP 工具行为。

- README 增加安装、调用、重试、排错和 GitHub MCP 协作说明。
- 增加从 `server.py` 工具注册表生成 `docs/tools.md` 和 `docs/tools.json` 的脚本。
- 增加使用教程、设计说明、验证报告和贡献指南。

## [0.1.0] - 2026-09-20

首个公开版本。

- 提供 8 个 Hugo MCP 工具：查询、读取、创建草稿、更新、发布、撤回、软删除和构建。
- 增加 SHA-256 revision 乐观锁，防止基于旧内容覆盖文章。
- 增加 request_id 幂等记录、变更前备份、审计日志和私有回收站。
- 增加站点目录路径规范化和越界检查。
- 提供 Bearer Token、请求体限制、来源限流和 Docker Compose 部署模板。
- GitHub 操作保持在服务外部，由 MCP 客户端或 GitHub Actions 负责。
