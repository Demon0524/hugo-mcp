# Contributing

## 修改原则

- 保持 Hugo Markdown 为内容源，不引入数据库依赖。
- 新增或修改工具时，先更新 `server.py` 的 `TOOLS`，再运行 `python scripts/generate_tools_docs.py`。
- 工具行为、输入 Schema 和 README/`docs/` 说明必须保持一致。
- 不提交 Token、`.env`、运行数据、备份、审计日志或真实站点内容。
- 涉及发布、撤回和删除的改动必须补充测试步骤，并说明失败恢复方式。

## 本地检查

```bash
python -m py_compile server.py
python scripts/generate_tools_docs.py
docker compose config
```

提交消息应说明行为变化；涉及工具合同的改动请同时更新 `CHANGELOG.md`。
