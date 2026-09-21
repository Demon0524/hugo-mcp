# Hugo MCP

[最新 Release](https://github.com/Demon0524/hugo-mcp/releases/latest) · [工具合同](docs/tools.md) · [使用教程](docs/tutorial.md) · [设计说明](docs/design.md) · [验证报告](docs/verification.md) · [更新记录](CHANGELOG.md)

> Hugo 是其各自权利人的商标。本项目是独立的第三方 MCP 适配器，与 Hugo 官方没有隶属、授权或背书关系。

一个面向 Hugo 文件站点的轻量 MCP HTTP 服务。它直接读写 content/posts/ 下的 Markdown 和 Page Bundle，不引入数据库，也不提供 CMS 后台。AI 客户端通过 MCP 调用文章和媒体工具，服务负责校验、写文件、生成备份、记录审计并按需执行 Hugo 构建。

Git 操作不属于本服务。需要提交到 GitHub 时，由 MCP 客户端另外调用 GitHub MCP 或 GitHub Actions；Hugo MCP 不保存 GitHub 凭据。

## 能做什么

服务提供 JSON-RPC MCP 端点，以下三个路径等价：/、/mcp、/index.php/action/agent-mcp。只接受 POST，请求必须带 Authorization: Bearer <token>。

| 工具 | 用途 | 关键参数 |
| --- | --- | --- |
| hugo_list_posts | 分页列出文章 | status、query、limit、offset |
| hugo_get_post | 读取 front matter、正文和 revision | slug |
| hugo_create_draft | 创建草稿（默认 Page Bundle） | title、request_id，可选 slug、content、categories、tags、bundle |
| hugo_update_draft | 更新文章 | slug、expected_revision、request_id，以及要修改的字段 |
| hugo_publish_post | 发布文章并构建 Hugo | slug、expected_revision、request_id |
| hugo_unpublish_post | 撤回为草稿并构建 Hugo | slug、expected_revision、request_id |
| hugo_delete_post | 软删除文章并构建 Hugo | slug、expected_revision、request_id |
| hugo_migrate_post_bundle | 将旧的 flat Markdown 迁移为 Page Bundle | slug、expected_revision、request_id |
| hugo_upload_media | 上传图片并返回相对 Markdown 引用 | post_slug、mime_type、data_base64、request_id |
| hugo_list_media | 列出文章 Page Bundle 中的媒体 | post_slug |
| hugo_delete_media | 软删除媒体并构建 Hugo | post_slug、filename、expected_revision、request_id |
| hugo_build | 按当前源码执行一次 Hugo 构建 | 无 |

本服务不包含后台 UI、评论管理、全文搜索、Git 提交或 GitHub 账号管理。媒体上传目前只支持 JPEG、PNG、WebP、GIF，不抓取远程 URL。

## 推荐调用流程

文章修改使用乐观锁，避免 AI 覆盖别人刚刚修改的内容：

1. 调用 hugo_list_posts 或 hugo_get_post，取得目标文章的 revision。
2. 调用 hugo_create_draft 或 hugo_update_draft。每次写操作都使用唯一的 request_id；重试同一个 request_id 会返回原结果，不会重复写入。
3. 检查返回内容，确认标题、分类、标签和正文。
4. 如果文章需要截图，调用 hugo_upload_media，把返回的相对 Markdown 引用插入正文。
5. 调用 hugo_publish_post，并传入刚才返回的最新 revision。
6. 如需同步代码仓库，再由客户端调用 GitHub MCP 提交 Hugo 源码和 Page Bundle 媒体。

如果 revision 已变化，服务会拒绝写入；重新读取文章后再提交，不要绕过检查。

## JSON-RPC 示例

查询工具：

~~~http
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"tools/list"}
~~~

列出已发布文章：

~~~json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "hugo_list_posts",
    "arguments": {"status": "publish", "limit": 20, "offset": 0}
  }
}
~~~

创建草稿：

~~~json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "hugo_create_draft",
    "arguments": {
      "title": "文章标题",
      "slug": "article-title",
      "content": "正文内容",
      "categories": ["技术笔记"],
      "tags": ["Hugo"],
      "request_id": "draft-20260920-001"
    }
  }
}
~~~

## Docker 部署

宿主机需要 Docker Compose、Hugo 可执行文件和一个 Hugo 站点目录。路径必须使用宿主机绝对路径，并且运行数据不要放在 Git 仓库内：

~~~bash
cp .env.example .env
$EDITOR .env
mkdir -p /srv/hugo-mcp
chmod 700 /srv/hugo-mcp
docker compose up -d --build
~~~

.env 至少设置：

~~~dotenv
HUGO_SITE_ROOT_HOST=/srv/hugo/site
HUGO_MCP_DATA_ROOT_HOST=/srv/hugo-mcp
HUGO_BIN_HOST=/usr/local/bin/hugo
HUGO_MCP_BIND_ADDRESS=127.0.0.1
HUGO_MCP_PORT=8095
HUGO_MCP_MEDIA_MAX_BYTES=5242880
~~~

HUGO_SITE_ROOT_HOST 中必须存在 hugo.toml 和 content/。新建文章默认使用 `content/posts/<slug>/index.md` Page Bundle；现有的 `content/posts/<slug>.md` 仍然兼容，可通过 `hugo_migrate_post_bundle` 逐篇迁移。HUGO_MCP_DATA_ROOT_HOST 用于保存 token、备份、审计日志、幂等记录和软删除回收站。首次启动会自动生成 token，可在宿主机读取：

~~~bash
sudo cat /srv/hugo-mcp/token
~~~

Compose 默认只监听回环地址。需要让其他机器访问时，应通过 HTTPS 反向代理，并在代理层做访问控制；不要直接把 8095 暴露到公网。

## MCP 客户端配置

以支持远程 HTTP MCP 的客户端为例：

~~~toml
[mcp_servers.hugo]
url = "https://example.com/mcp"
bearer_token_env = "HUGO_MCP_TOKEN"
~~~

先调用 initialize，再调用 tools/list。如果使用公开的兼容入口，也可以把 URL 改成 /index.php/action/agent-mcp。

## 数据与安全

- 文章只允许落在规范化后的 content/posts/ 目录，拒绝路径越界和非法 slug；新文章默认使用 Page Bundle。
- 媒体只允许落在所属 Page Bundle 目录，拒绝跨文章写入、非法文件名和错误文件签名。
- 写入使用临时文件加原子替换；更新、发布、撤回、删除前会生成备份。
- 删除是软删除，Markdown 会先移动到私有回收站，不会立即销毁。
- 所有写操作都写入审计 JSONL，并要求 request_id 幂等。
- 默认请求体上限为 8 MiB，单个媒体文件默认最大 5 MiB，每个来源地址每分钟最多 30 次请求。
- .env、token、备份、审计日志、幂等记录和回收站都不应提交到 Git。

## 开发检查

~~~bash
python -m py_compile server.py
docker compose config
~~~

功能基线从 v0.1.0 开始；当前版本为 v0.2.0，后续变更会在 GitHub Releases 中记录。

## 贡献与发布边界

本项目采用 MIT License，欢迎通过 Issue、Fork 和 Pull Request 改进通用的 Hugo MCP 能力。贡献范围包括工具合同、文件安全、媒体管理、测试、文档和容器部署；请不要提交私人博客文章、真实站点内容、Token、备份、审计日志或生产环境配置。

所有改动先经过 GitHub Actions 检查，再通过 Pull Request 合并到 `main`。生产 docker03 不会自动运行公开仓库的提交，只从审核后的 Release 手工部署。安全问题请遵循 [安全报告流程](SECURITY.md)，不要在公开 Issue 中披露可利用细节。

完整流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
