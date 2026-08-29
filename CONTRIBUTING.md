# 参与贡献

## 开发环境

推荐将两个仓库放在同一目录：`AuroraAgentBackend/` 与 `AuroraAgentFrontend/`。后端严格使用 `uv sync --frozen` 和 `uv run`，前端使用 `pnpm install --frozen-lockfile`。

## 分支与提交

分支使用 `feat/`、`fix/`、`docs/`、`refactor/` 或 `test/` 前缀。提交信息遵循 Conventional Commits，例如 `feat(runtime): add session recovery`。

## 提交前检查

```bash
uv run pre-commit run --all-files
uv run pytest
```

涉及前端时同时运行 `pnpm lint && pnpm typecheck && pnpm test`。PR 应说明变更、关联 Issue、验证命令和用户界面截图。至少一名维护者 review 通过后合并。

Bug Issue 需提供环境、复现步骤、实际结果与期望结果；功能建议需描述使用场景和兼容性影响。
