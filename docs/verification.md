# 验证报告

本文件记录公开版本的可重复检查项。命令应在仓库根目录执行。

## 静态检查

```bash
python -m compileall -q server.py output_build.py scripts tests
python scripts/generate_tools_docs.py
python -m unittest discover -s tests -v
docker compose config
```

预期结果：Python 无语法错误，生成 13 个工具文档，隔离测试通过，Compose 配置可以解析。

## 运行检查

启动一个只绑定回环地址的实例后，依次检查：

```bash
curl -i http://127.0.0.1:8095/mcp
curl -i -X OPTIONS http://127.0.0.1:8095/mcp
curl -i -X POST http://127.0.0.1:8095/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
```

预期结果：GET 返回 405，OPTIONS 返回 204，合法的 POST 返回 JSON-RPC 结果，缺少或错误 Token 返回 401。

## 工具流程检查

在隔离测试站点先执行一次 `hugo_build` 生成测试输出并建立清单，再检查以下工具行为（该调用会发布当前非草稿内容，不是只读初始化）：

1. `hugo_list_posts`：确认分页、状态筛选和 revision 返回。
2. `hugo_get_post`：确认正文和 front matter 可读。
3. `hugo_create_draft`：确认生成草稿 Markdown。
4. `hugo_update_draft`：确认错误 revision 被拒绝，正确 revision 可以更新。
5. 重复发送相同 `request_id`：确认不会产生第二次变更。
6. `hugo_publish_post`：确认构建成功且文章输出存在。
7. `hugo_unpublish_post`：确认 draft 状态和输出清理。
8. `hugo_delete_post`：确认 Markdown 进入 trash 而不是直接销毁。
9. `hugo_upload_media`：确认只接受允许的 MIME、扩展名和文件签名，并返回相对 Markdown 引用。
10. `hugo_list_media`：确认返回媒体大小、MIME 和 revision。
11. `hugo_delete_media`：确认媒体进入返回的 `trash_path`（`trash/<request_id>/media/<slug>/<filename>`），并清理受管理的旧媒体输出。
12. `hugo_migrate_post_bundle`：确认 flat 文章先备份，再移动为 `index.md`。
13. `hugo_batch_posts`：确认默认预览不写文件；错误 revision 或重复文章使整批预先校验失败；显式执行后统一构建一次。

不要在生产文章上执行删除测试；使用隔离的测试站点和独立运行数据目录。

回归检查还应覆盖：消失的分页、Feed 和媒体被清理，未受管理文件不被误删，外部修改的旧输出返回警告；注入构建失败后使用原请求恢复，并确认待恢复期间拒绝其他写入。HTTP 测试检查 409 `OPERATION_PENDING` 的请求 ID、阶段和可重试标记，以及 429 的 `Retry-After`。

另用未建立清单的测试站点检查自动基线：它只记录匹配路径上已有公开文件的实际哈希，不发布变更前源码；基线构建失败时源码和公开目录均保持不变。

## 测试隔离

自动化测试使用临时目录与虚构内容，不读取或操作用户站点。真实 Hugo 集成测试通过可选的 `HUGO_TEST_BIN` 指定测试二进制；未设置时跳过该测试，不使用任何预设的本机路径。

## 尚未承诺的范围

本项目没有声明多副本并发写入、跨主机事务和 GitHub 自动提交。媒体功能只覆盖小型图片，不覆盖 SVG、远程抓取和大文件处理。
