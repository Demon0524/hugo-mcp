# 验证报告

本文件记录公开版本的可重复检查项。命令应在仓库根目录执行。

## 静态检查

```bash
python -m py_compile server.py
python scripts/generate_tools_docs.py
docker compose config
```

预期结果：Python 无语法错误，生成 8 个工具文档，Compose 配置可以解析。

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

在测试站点执行以下顺序：

1. `hugo_list_posts`：确认分页、状态筛选和 revision 返回。
2. `hugo_get_post`：确认正文和 front matter 可读。
3. `hugo_create_draft`：确认生成草稿 Markdown。
4. `hugo_update_draft`：确认错误 revision 被拒绝，正确 revision 可以更新。
5. 重复发送相同 `request_id`：确认不会产生第二次变更。
6. `hugo_publish_post`：确认构建成功且文章输出存在。
7. `hugo_unpublish_post`：确认 draft 状态和输出清理。
8. `hugo_delete_post`：确认 Markdown 进入 trash 而不是直接销毁。

不要在生产文章上执行删除测试；使用隔离的测试站点和独立运行数据目录。

## 已验证环境

- 公开源码：`v0.1.0`
- Python：3.12（docker03）
- 服务形态：Docker Compose + Hugo 二进制挂载
- 站点数据：文件系统 Markdown，不依赖数据库

## 尚未承诺的范围

本项目没有声明多副本并发写入、跨主机事务、媒体上传和 GitHub 自动提交。需要这些能力时应先扩展设计和测试，再更新工具合同与版本说明。
