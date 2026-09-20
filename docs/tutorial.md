# Hugo MCP 使用教程

本教程假定服务已经通过 Docker 启动，并且 MCP 客户端可以访问服务 URL。服务是无状态 HTTP JSON-RPC；每个请求都需要独立携带 Bearer Token。

## 1. 取得连接信息

在宿主机配置 `.env` 后启动：

```bash
docker compose up -d --build
```

首次启动会在 `HUGO_MCP_DATA_ROOT_HOST/token` 生成 Token：

```bash
sudo cat /srv/hugo-mcp/token
```

生产环境不要把 Token 放在 URL、Git、聊天记录或 Compose 文件中。把它放到 MCP 客户端启动时能读取的环境变量中，例如 `HUGO_MCP_TOKEN`。

## 2. 检查连接

先调用 `initialize`，确认服务返回 `HugoMCP` 和协议版本；再调用 `tools/list`，确认当前版本的 12 个工具可见：

```bash
curl -fsS https://example.com/mcp \
  -H "Authorization: Bearer ${HUGO_MCP_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'

curl -fsS https://example.com/mcp \
  -H "Authorization: Bearer ${HUGO_MCP_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

兼容入口 `/index.php/action/agent-mcp` 与 `/mcp` 的处理逻辑相同。GET 请求不会返回页面，服务只接受 POST。

## 3. 查询文章

先用 `hugo_list_posts` 找到 slug，再用 `hugo_get_post` 读取正文和 front matter：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "hugo_list_posts",
    "arguments": {"status": "publish", "query": "Hugo", "limit": 20, "offset": 0}
  }
}
```

返回的 `post.revision` 是当前 Markdown 文件的 SHA-256。修改、发布、撤回和删除都必须使用这一个确切值。

新建草稿默认是 Page Bundle：

```text
content/posts/truenas-guide/
├── index.md
└── step-01.webp
```

旧的 `content/posts/truenas-guide.md` 仍然可以读取和编辑；需要上传媒体前，先调用 `hugo_migrate_post_bundle`，它会备份原文件并保持 slug 不变。

## 4. 写作、检查、发布

推荐把“写作”和“上线”分成两个动作：

1. `hugo_create_draft` 创建草稿，或者 `hugo_update_draft` 修改已有文章。
2. 读取返回的文章，检查 front matter 和正文。
3. 需要截图时调用 `hugo_upload_media`，把返回的 `markdown` 字段插入正文。
4. 需要上线时调用 `hugo_publish_post`。

更新示例：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "hugo_update_draft",
    "arguments": {
      "slug": "article-title",
      "expected_revision": "<sha256-from-hugo_get_post>",
      "title": "更新后的标题",
      "content": "更新后的正文",
      "request_id": "update-20260920-001"
    }
  }
}
```

发布示例：

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "hugo_publish_post",
    "arguments": {
      "slug": "article-title",
      "expected_revision": "<sha256-from-update-result>",
      "request_id": "publish-20260920-001"
    }
  }
}
```

发布和撤回会执行 Hugo 构建。构建失败时，服务返回错误，不应继续把失败结果当作已上线处理。

## 5. 上传和管理媒体

远程 MCP 不能读取调用者电脑上的本地路径，因此图片需要由客户端编码为 Base64 后放入 `data_base64`。服务会校验文件签名、MIME 类型、扩展名和大小；当前只接受 JPEG、PNG、WebP、GIF，单文件默认上限为 5 MiB。

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "hugo_upload_media",
    "arguments": {
      "post_slug": "truenas-guide",
      "filename": "step-01.webp",
      "mime_type": "image/webp",
      "data_base64": "<base64>",
      "alt": "TrueNAS 存储池配置",
      "role": "inline",
      "request_id": "media-20260920-001"
    }
  }
}
```

返回的 `media.markdown` 是可直接插入 `index.md` 的相对引用，例如 `![TrueNAS 存储池配置](step-01.webp)`。`hugo_list_media` 返回媒体 revision；删除时必须带这个 revision，删除会移动到私有 `trash/media/` 并重新构建。

## 6. 重试与冲突

每个写操作使用一个新的、稳定的 `request_id`。网络超时后不要立即换新的 ID：

- 如果服务已经写入幂等记录，使用同一个 `request_id` 会返回原结果。
- 如果服务进程在写入和记录结果之间崩溃，结果可能未知。先用 `hugo_get_post` 检查文件和 revision，再决定是否重试。
- 如果返回 revision mismatch，说明文章已经被修改；重新读取、合并变更，再使用新的 `request_id`。

当前版本没有单独的 `get_operation` 工具，不能把幂等记录当作远程状态查询接口。幂等文件、审计日志和备份只供服务端运维检查。

## 7. 撤回和删除

- `hugo_unpublish_post` 将 `draft` 设为 true，删除已生成的该文章输出并重新构建。
- `hugo_delete_post` 将 Markdown 移动到运行数据目录的 `trash/`，删除已生成输出后重新构建。
- 删除是软删除，不等于永久销毁；回收站需要在宿主机上按备份策略管理。

## 8. 与 GitHub MCP 配合

Hugo MCP 不提交 Git，也不保存 GitHub Token。需要同步源码时，由客户端单独调用 GitHub MCP：

```text
Hugo MCP：创建/修改/发布文章
    ↓
GitHub MCP：提交 Hugo 源码到目标仓库
    ↓
GitHub Actions 或部署脚本：构建并发布站点
```

两个 MCP 是并列服务，不应让 Hugo MCP 在服务端保存或转发 GitHub 凭据。
