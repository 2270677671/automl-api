# Managed AutoML Milestone 2a 本地 Durable 评估处置

独立评估见 `milestone-2a-local-durable-gemini.md`，得分 **85/100**。

## 采纳结论

- 默认 API 的真实本地 durable 纵向切片已接通，可用于受控的小数据本地开发和演示。
- 不能称为生产可用，也不能向第三方开放不受控数据上传。
- lease heartbeat、可强制终止的 wall-time、上传和内存上限是下一阶段首先处理的 P1 可靠性门禁。
- OIDC/JWT、授权/RLS、审计/DLP、PostgreSQL、可观测性和灾备仍是生产前 P0 门禁。

## 本轮已处置

1. 默认 `create_app()` 接入 `DurableWorkflowService`、job repair 和后台 worker 生命周期。
2. worker shutdown 时 fenced release lease，避免重启后等待租约到期。
3. 修正 EvaluationReport metrics 结构和字符串二分类正类选项。
4. 将 `max_trials` 传给引擎并限制候选数。
5. 新增真实 E2E：上传、等待/回答、异步训练、结果、下载校验和 SQLite 重开恢复。

## 保留门禁

Gemini 建议的 heartbeat、硬 wall-time 和资源大小限制尚未实现，按事实保留为下一阶段阻塞项；
没有将这些缺口解释为当前已解决。

最终处置：**批准 M2a 本地开发切片收口；批准继续升级；不批准生产或开放试用。**
