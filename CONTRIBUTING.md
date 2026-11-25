# 贡献说明

欢迎通过 fork 和 Pull Request 提交修改。公开仓库可阅读／fork，不自动授予远程写入权限。

1. Fork 仓库，在自己的功能分支进行本地 commit，并 push 到自己的 fork。
2. 向本仓库 `main` 提交 PR，说明修改目的、行为变化和验证结果。
3. PR 需要一份批准、代码所有者审核，以及解决代码讨论；新增提交会使之前批准失效。
4. 仓库所有者负责合并。默认分支限制直接更新、强推与删除；仓库所有者保留规则例外，用于维护。

已经获得本仓库写入权限的协作者也应使用功能分支和 PR，不直接修改 `main`。本地 commit 不受 GitHub 分支规则控制；规则控制的是远程分支更新和合并。

本地验证：

```sh
python -m unittest discover -s tests
node tests/test_stage_c_drafts.cjs
node tests/check_images_ui.cjs
python scripts/prepare_release.py --check
```

请勿提交真实 `.env`、API Key、数据库、客户资料或生成结果。配置示例仅放空凭证。新功能保留来源范围、版本绑定、用户确认和已有预算；调整 Agent 或联网分工时同步需求及 SDD。

规则配置保存在 [.github/rulesets/main.json](.github/rulesets/main.json)，它是可审阅的规则定义；实际生效状态以 GitHub 仓库 Settings → Rules → Rulesets 为准。
