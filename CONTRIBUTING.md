# Contributing

感谢参与。这个仓库维护的是通用的 Hugo 文件站点 MCP 适配器，不包含维护者的私人博客内容或生产环境配置。

## 开始之前

- 小问题、文档修正和功能建议可以先创建 Issue。
- 较大的改动请先创建 Issue 说明目标、兼容性和安全影响，再提交 Pull Request。
- 不要向仓库提交真实站点内容、Token、`.env`、备份、审计日志或任何生产数据。
- 安全漏洞不要公开创建 Issue，按 [SECURITY.md](SECURITY.md) 联系维护者。

## 修改原则

- 保持 Hugo Markdown 为内容源，不引入数据库依赖。
- 新增或修改工具时，先更新 `server.py` 的 `TOOLS`，再运行 `python scripts/generate_tools_docs.py`。
- 工具行为、输入 Schema 和 README/`docs/` 说明必须保持一致。
- 不提交 Token、`.env`、运行数据、备份、审计日志或真实站点内容。
- 涉及发布、撤回和删除的改动必须补充测试步骤，并说明失败恢复方式。

## 本地检查

```bash
python -m compileall -q server.py output_build.py scripts tests
python scripts/generate_tools_docs.py
git diff --exit-code -- docs/tools.md docs/tools.json
python -m unittest discover -s tests -v
docker compose config
```

更新工具合同时先审核并提交生成文档，再运行文档差异检查。GitHub Actions 也会执行这些 Python 检查和 Docker 镜像构建。Pull Request 必须通过检查后才能合并。

## Pull Request 要求

- PR 标题说明行为变化，例如 `Add media URL validation`。
- 描述中写明动机、兼容性影响、测试命令和失败恢复方式。
- 修改 `TOOLS` 时同步运行文档生成器，并提交更新后的 `docs/tools.md` 和 `docs/tools.json`。
- 涉及写入、删除、媒体或路径校验时，必须补充或更新测试。
- 保持用户环境隔离：不要在 PR 中引用私人博客仓库、真实域名、Token、主机标识或用户操作记录。

提交消息应说明行为变化；涉及工具合同的改动请同时更新 `CHANGELOG.md`。
