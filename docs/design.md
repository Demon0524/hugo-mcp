# 设计说明

## 目标

Hugo MCP 解决的是“让 AI 安全地操作一个文件型 Hugo 站点”，而不是提供新的 CMS。Hugo Markdown 是内容源，服务只在规定的文章目录和运行数据目录内工作。

## 组件关系

```text
MCP client / Codex
        │ JSON-RPC 2.0 + Bearer Token
        ▼
Hugo MCP HTTP server
  ├─ ToolRegistry：工具名称与输入 Schema
  ├─ content/posts：flat 文章与 Page Bundle
  ├─ public：Hugo 生成目录
  └─ data：token、备份、审计、幂等记录、trash
        │ subprocess
        ▼
      Hugo
```

GitHub MCP、GitHub Actions 和反向代理属于外部组件，不是 Hugo MCP 的运行时依赖。

## 内容模型

服务兼容两种文章形态：

```text
legacy flat：content/posts/slug.md
Page Bundle：content/posts/slug/index.md
             content/posts/slug/image.webp
```

新建草稿默认使用 Page Bundle。旧文章可以继续读取、更新和发布；`hugo_migrate_post_bundle` 会在 revision 校验后将单个 flat 文件移动为 `index.md`，迁移前创建备份。媒体只能属于 Page Bundle，不能写入 flat 文章，也不能跨文章引用服务端文件路径。

## 文件与路径边界

- `HUGO_SITE_ROOT` 是容器内 Hugo 站点根目录。
- `HUGO_POSTS_DIR` 默认是 `content/posts`，必须保持在站点根目录内。
- `HUGO_PUBLIC_DIR` 默认是 `public`，必须保持在站点根目录内。
- `HUGO_MCP_DATA_ROOT` 保存服务运行数据，与源码仓库分离。
- 文章路径由受限 slug 生成，不能由调用者提供任意文件路径。

服务启动时会规范化并检查站点目录；文章写入采用同目录临时文件、fsync 和原子替换。

## revision 与并发

每篇文章的 revision 是 Markdown 文件的 SHA-256。所有会改变文章的工具都要求 `expected_revision`，服务在锁内重新计算并比较；不匹配就拒绝操作。这样可以避免两个客户端基于旧内容互相覆盖。

`MUTATION_LOCK` 只保护当前进程内的变更。单实例 Docker 部署适用；多副本部署必须增加外部锁和共享数据策略，本项目不宣称支持多实例写入。

## 幂等与恢复

写操作要求 `request_id`，结果保存到 `idempotency/`。同一个 ID 再次到达时返回此前结果。变更前复制到 `backups/`，删除移动到 `trash/`，每次变更写入 `audit.jsonl`。

这是一种“尽量一次”的文件型幂等机制，不是跨进程事务。若进程在文件变更和幂等记录之间崩溃，调用者应先读取文章和审计日志再判断结果。

## 发布与撤回

发布和撤回会调用：

```text
hugo --source <site-root> --destination <public-root>
```

撤回或删除前只清理对应 slug 的已知输出路径，然后重新构建，不对整个 `public/` 做无条件删除，避免影响站点中的其他静态资源。

## 认证与限制

- Token 位于运行数据目录的 `token` 文件，首次启动随机生成。
- 只接受 Bearer Token，不提供后台登录、OAuth 或 GitHub 凭据管理。
- 默认请求体上限 8 MiB，单个媒体文件默认最大 5 MiB。
- 每个客户端地址每分钟最多 30 个请求。
- 服务默认通过 Compose 绑定回环地址，公网访问应放在受控 HTTPS 反向代理之后。

## 媒体模型

媒体上传通过 JSON 中的 Base64 字段传输，服务端不读取调用者本机路径，也不抓取远程 URL。当前允许 JPEG、PNG、WebP、GIF；文件签名必须与声明的 MIME 类型一致，默认单文件上限 5 MiB。

媒体文件与文章的 `index.md` 放在同一目录，上传结果返回相对 Markdown 引用。删除媒体使用媒体文件自身的 SHA-256 revision，并移动到 `trash/media/` 后重建站点。

## 非目标

当前版本不实现：

- CMS 后台 UI
- SVG、远程 URL 抓取和大文件媒体处理
- 评论、分类和标签管理
- Git 提交或 GitHub API
- 多实例共享锁
- 远程 URL 抓取
- 单独的操作状态查询工具
