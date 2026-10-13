# Hugo MCP 使用教程

本教程假定服务已经通过 Docker 启动，并且 MCP 客户端可以访问服务 URL。HTTP JSON-RPC 不要求客户端会话；每个请求都需要独立携带 Bearer Token。源码、恢复记录和输出清单则保存在服务端，必须持久化。

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

先调用 `initialize`，确认服务返回 `HugoMCP` 和协议版本；再调用 `tools/list`，确认包含 `hugo_batch_posts` 的 13 个工具可见：

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

首次执行需要构建的源码变更时，服务自动私下构建当前源码，为匹配路径上的已有公开文件建立基线，不更改公开输出；构建失败时源码尚未改变。首次接管不能识别这些文件此前的手工修改，也不能发现当前源码不再生成的历史页面或正文副本。`hugo_build` 会正常发布当前非草稿内容，不要把它当作只读初始化。

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
content/posts/example-post/
├── index.md
└── step-01.webp
```

旧的 `content/posts/example-post.md` 仍然可以读取和编辑；需要上传媒体前，先调用 `hugo_migrate_post_bundle`，它会备份原文件并保持 slug 不变。

## 4. 写作、检查、发布

推荐把“写作”和“上线”分成两个动作：

1. `hugo_create_draft` 创建草稿，或者 `hugo_update_draft` 修改已有文章。
2. 读取返回的文章，检查 front matter 和正文。
3. 需要截图时调用 `hugo_upload_media`，把返回的 `markdown` 字段插入正文。
4. 确认发布时调用 `hugo_publish_post`，更新服务端的本地输出。
5. 根据部署方式检查对外页面；MCP 构建成功本身不是远程上线证明。

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

发布和撤回会执行 Hugo 构建。构建失败时，服务返回错误，源码可能已经变化；不要继续把失败结果当作已上线处理，也不要用新 ID 重做操作。按下文说明恢复原请求。

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
      "post_slug": "example-post",
      "filename": "step-01.webp",
      "mime_type": "image/webp",
      "data_base64": "<base64>",
      "alt": "示例图片",
      "role": "inline",
      "request_id": "media-20260920-001"
    }
  }
}
```

返回的 `media.markdown` 是可直接插入 `index.md` 的相对引用，例如 `![示例图片](step-01.webp)`。`hugo_list_media` 返回媒体 revision；删除时必须带这个 revision，删除会移动到私有回收站并重新构建。

## 6. 重试与冲突

每个写操作使用一个新的、稳定的 `request_id`。网络超时后不要立即换新的 ID：

- 重试使用原工具与完全相同的参数，包括原 `expected_revision`、`request_id` 和批量项目顺序。已完成时返回原结果；修改参数复用 ID 会被拒绝。升级前的旧结果记录仍兼容回放，但没有原参数绑定。
- 如果返回 HTTP 409 `OPERATION_PENDING` 或进程中断，用原请求恢复。错误中的 `details.request_id` 指明应恢复的请求，`details.phase` 标明阶段；`retryable: true` 不代表故障已自动修复。计划会识别已完成的文件步骤并继续构建，即使文章已变为草稿或已经移入回收站，也不要另建请求。
- 待恢复期间可以读取内容，但其他源码写入和独立 `hugo_build` 会被阻止。先修复磁盘、Hugo 模板等失败原因，再重试原请求。
- 如果在新操作的预先校验阶段返回 revision mismatch，重新读取并合并变更，再用新 ID 提交。恢复中出现文件状态冲突则需先核对备份和操作记录，不能靠新 ID 绕过。
- 收到 HTTP 429 时降低调用频率，按 `Retry-After: 60` 等待后用相同请求重试，不要创建重复任务。

跨文件系统移动中断或外部并发修改可能无法自动恢复，需要人工核对原文件、回收站和备份，不要直接删除恢复记录。当前版本没有单独的 `get_operation` 工具，不能把幂等记录当作远程状态查询接口。幂等文件、审计日志和备份只供服务端运维检查。

## 7. 撤回和删除

- `hugo_unpublish_post` 将 `draft` 设为 true，重新构建并清理已受管理的旧输出。
- `hugo_delete_post` 将 flat Markdown 或整个 Page Bundle 移动到运行数据目录的 `trash/`，再重新构建并清理已受管理的旧输出。
- 删除是软删除，不等于永久销毁；回收站需要在宿主机上按备份策略管理。

构建会清理输出清单中不再生成的文章页、分页、聚合页、Feed 和媒体。未受管理的遗留文件不会仅因为本次没生成就被删除；外部修改过的旧生成文件也会保留，在构建结果的 `warnings` 和 `preserved_modified` 中报告。检查警告、历史公开副本及对外响应，不能只检查文章 URL。不要直接清空整个 `public/`，否则可能丢失迁移附件。

## 8. 批量预览与执行

`hugo_batch_posts` 支持 `publish`、`unpublish` 和 `delete`，每批 1 至 50 篇不同文章，必须显式指定 slug 和 revision。例如先预览撤回与删除计划：

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "hugo_batch_posts",
    "arguments": {
      "dry_run": true,
      "items": [
        {"action": "unpublish", "slug": "article-title", "expected_revision": "<sha256-from-hugo_get_post>"},
        {"action": "delete", "slug": "outdated-note", "expected_revision": "<sha256-from-hugo_get_post>"}
      ]
    }
  }
}
```

省略 `dry_run` 也是预览，不写文件、不构建。审核计划后，对同一组 `items` 显式传 `dry_run: false` 和一个新的 `request_id`，例如 `batch-example-001`。预览结束后不保留锁，执行会重新校验全部 revision，任何项目不合法时都不会开始源码变更。

通过校验后统一执行并在最后构建一次；若尚未建立基线，会先额外构建一次。批量操作不是跨文件事务，I/O 失败可能留下部分源码变更。用完全相同的整批参数和 ID 恢复，不要把尚未完成的项目拆成新的操作。单次批量调用可减少逐篇构建和请求频率，但不免除限流或客户端超时处理。

## 9. 与 GitHub MCP 配合

Hugo MCP 不提交 Git，也不保存 GitHub Token。需要同步源码时，由客户端单独调用 GitHub MCP：

```text
Hugo MCP：创建/修改/发布文章
    ↓
GitHub MCP：提交 Hugo 源码到目标仓库
    ↓
GitHub Actions 或部署脚本：构建并发布站点
```

两个 MCP 是并列服务，不应让 Hugo MCP 在服务端保存或转发 GitHub 凭据。
