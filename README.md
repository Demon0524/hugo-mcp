# Hugo MCP

一个面向 Hugo 文件站点的轻量 MCP HTTP 服务。它直接读写 `content/posts/*.md`，不引入数据库，也不提供 CMS 后台。当前实现保持博客线上使用的功能：文章查询、草稿创建与更新、发布/撤回、软删除和 Hugo 构建。

## 功能

服务提供 JSON-RPC MCP 端点（兼容 `/`、`/mcp` 和 `/index.php/action/agent-mcp` 三个路径）：

- `hugo_list_posts`：按状态、关键词分页列出文章，并返回 revision
- `hugo_get_post`：读取文章 front matter 与正文
- `hugo_create_draft`：创建草稿
- `hugo_update_draft`：使用 revision 乐观锁更新文章
- `hugo_publish_post`：发布并构建 Hugo
- `hugo_unpublish_post`：撤回为草稿并构建 Hugo
- `hugo_delete_post`：移动到私有 trash 后构建 Hugo（软删除）
- `hugo_build`：手动构建 Hugo

所有写操作都要求 `request_id`。服务会记录幂等结果、写审计 JSONL、在变更前备份，并限制 slug、文件路径、请求体大小和请求频率。token 从数据目录的 `token` 文件读取；首次启动会生成随机 token，仓库不保存任何 token。

## Docker 部署

1. 复制配置并修改为实际的绝对路径：

   ```bash
   cp .env.example .env
   $EDITOR .env
   mkdir -p /srv/hugo-mcp
   chmod 700 /srv/hugo-mcp
   ```

2. 确认宿主机已安装 Hugo，并启动：

   ```bash
   docker compose up -d --build
   ```

`HUGO_SITE_ROOT_HOST` 必须是 Hugo 站点根目录，目录中应包含 `hugo.toml` 和 `content/`。`HUGO_MCP_DATA_ROOT_HOST` 只用于 token、备份、审计、幂等记录和回收站，建议使用独立且不被 Git 跟踪的目录。Compose 默认只绑定 `127.0.0.1`；如果需要通过反向代理访问，请在代理层提供 TLS、访问控制和来源限制。

## MCP 客户端

将服务地址配置为反向代理后的 URL，并使用 `Authorization: Bearer <token>`。例如 Codex 的配置可以写成：

```toml
[mcp_servers.hugo]
url = "https://example.com/index.php/action/agent-mcp"
bearer_token_env = "HUGO_MCP_TOKEN"
```

本地调试时可以先调用 `initialize`、`tools/list`，再调用 `tools/call`。发布、撤回、更新和删除必须先读取文章拿到最新 revision，并提供新的 `request_id`。

## 安全边界

- 不把 `.env`、token、备份、审计日志或回收站提交到 Git。
- 不直接把 MCP 容器端口暴露到公网；生产环境放在受控反向代理后面。
- 站点卷和运行数据卷使用规范化的绝对路径挂载，服务只在允许的文章目录和数据目录内操作。
- 删除是软删除，原 Markdown 会先移动到私有回收站；需要人工清理时再处理运行数据目录。

## 开发检查

```bash
python -m py_compile server.py
docker compose config
```

本项目只包含 MCP 服务源码与部署模板；具体博客内容在私有的 `Felix-personal_blog` 仓库中维护。
