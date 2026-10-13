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
  └─ data：token、备份、审计、恢复与幂等记录、输出清单、trash
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
- `HUGO_MCP_DATA_ROOT` 和 `HUGO_MCP_TOKEN_FILE` 必须位于站点根目录之外；文章目录、公开目录和运行数据目录不得相同或相互包含。
- 文章路径由受限 slug 生成，不能由调用者提供任意文件路径。

服务启动时会规范化并检查站点目录；文章写入采用同目录临时文件、fsync 和原子替换。

## revision 与并发

每篇文章的 revision 是 Markdown 文件的 SHA-256。修改已有文章的工具要求 `expected_revision`，服务在锁内重新计算并比较；不匹配就拒绝操作。新建草稿检查路径是否存在，媒体上传检查目标文件冲突，媒体删除使用媒体自身的 revision。

`MUTATION_LOCK` 只保护当前进程内的变更。同一请求的幂等检查也在锁内执行，避免并发重试重复执行。单实例 Docker 部署适用；多副本部署必须增加外部锁和共享数据策略，本项目不宣称支持多实例写入。直接编辑源码或输出的外部程序不受此锁保护，运维必须协调其写入。

## 幂等与恢复

源码写操作要求 `request_id`。新记录绑定工具名称和请求参数，同一个 ID 不能用于不同输入；完成的请求返回原结果。升级前的结果型幂等记录保留兼容，但缺少原输入，不能补做绑定验证。

服务在修改文件前持久化操作计划，计划中的写入和移动步骤记录前后内容哈希。重试时根据实际文件状态识别未执行或已完成的步骤，继续执行剩余步骤和构建，避免重复修改正文或重复移动到回收站。更新前备份、删除回收站和审计仍保留。

文件操作、构建或输出同步失败后，操作可能已部分完成。服务返回 HTTP 409 `OPERATION_PENDING`，包含 `retryable: true` 及 `details.request_id`、`details.phase`（`source`、`build` 或 `finish`）；调用者必须保留原工具、全部参数、原 revision 和 `request_id` 重试，即使源码 revision 已变化。服务重启后仍可从持久化记录恢复。待恢复期间允许读取，拒绝其他源码写入和独立构建，避免在未完成的源码状态上继续修改或发布。

这不是事务回滚，也不是跨进程锁。源码写入采用原子替换，移动采用 `shutil.move`；跨文件系统移动中断时可能留下不完整的目标副本，不能保证自动恢复。此类状态或外部编辑导致的哈希冲突需要运维核对源文件、回收站、备份和记录；不要通过删除恢复记录或换 ID 来跳过冲突。运行数据必须持久化，不能只保留源码而丢弃恢复记录。

完成记录会移除计划中的正文和媒体载荷。崩溃后恢复可能重复写入相同请求的审计事件，因此审计不是严格恰好一次，应按 `request_id` 关联检查。

## 发布与撤回

`output_build.py` 将 Hugo 输出与已有公开目录分离。发布、撤回、删除、迁移和独立构建使用干净的临时目录：

```text
hugo --source <site-root> --destination <temporary-destination>
```

Hugo 成功后才按文件同步到 `public/`，输出清单记录生成文件的路径和哈希。下一次构建中消失的受管理文件会清除，因此文章页之外的旧分页、分类与标签页面、Feed 和媒体也能清理。若旧文件被外部修改，则保留并返回警告；未受管理且未被本次构建生成的文件不删除，避免清空迁移附件。Hugo 本次生成的路径仍属于构建输出，不应与外部维护的文件共用。

首次需要构建的源码变更会先私下构建当前源码，找到它生成的路径，并将这些路径下已有公开文件的实际哈希登记为基线；这一步不写入或删除公开文件。它要求当前源码与模板可构建，失败时尚未改变源码。首次接管无法分辨这些已有文件是否曾被手工修改，也无法识别当前源码不再生成的历史残留，须人工审查。显式 `hugo_build` 则执行正常构建并发布当前非草稿内容，只在确实要更新输出时调用。

Hugo 执行失败不会把临时构建内容同步到公开目录。文件同步本身仍可能部分失败，恢复请求会继续处理；这是逐文件同步，不是整个目录的原子切换。构建只更新服务文件系统，不检查域名、CDN、反向代理或远程部署状态。

## 批量文章操作

`hugo_batch_posts` 接受 1 至 50 个显式项目，每项包含 `action`（`publish`、`unpublish` 或 `delete`）、`slug` 和 `expected_revision`；同一文章不能重复出现。不支持通配符或按搜索结果隐式删除。

默认 `dry_run: true` 只验证并返回计划，不写源码、不建立恢复记录，也不构建。执行时显式传 `dry_run: false` 和唯一 `request_id`。服务在锁内重新校验全部项目、路径和 revision，校验通过后才保存计划并写入，最后统一构建一次（首次基线构建另计）。

预先校验防止已知输入错误造成部分变更，但不保证磁盘故障等运行时错误的全有或全无。失败后的整批请求由同一个 `request_id` 恢复，不应把剩余项目拆成新请求重试。

## 认证与限制

- Token 位于运行数据目录的 `token` 文件，首次启动随机生成。
- 只接受 Bearer Token，不提供后台登录、OAuth 或 GitHub 凭据管理。
- 默认请求体上限 8 MiB，单个媒体文件默认最大 5 MiB。
- 每个客户端地址每分钟最多 30 个请求；HTTP 429 返回 `Retry-After: 60` 和 `retryable: true`。
- 服务默认通过 Compose 绑定回环地址，公网访问应放在受控 HTTPS 反向代理之后。

## 媒体模型

媒体上传通过 JSON 中的 Base64 字段传输，服务端不读取调用者本机路径，也不抓取远程 URL。当前允许 JPEG、PNG、WebP、GIF；文件签名必须与声明的 MIME 类型一致，默认单文件上限 5 MiB。

媒体文件与文章的 `index.md` 放在同一目录，上传结果返回相对 Markdown 引用。删除媒体使用媒体文件自身的 SHA-256 revision，并移动到 `trash/<request_id>/media/<slug>/<filename>` 后重建站点；以结果中的 `trash_path` 为准。

## 非目标

当前版本不实现：

- CMS 后台 UI
- SVG、远程 URL 抓取和大文件媒体处理
- 评论、分类和标签管理
- Git 提交或 GitHub API
- 多实例共享锁
- 远程 URL 抓取
- 单独的操作状态查询工具
