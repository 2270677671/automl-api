# Milestone 1 Gemini 评审处置

外部评审结果见 `milestone-1-implementation-gemini.md`，综合健康度为 **92/100**，结论为
“继续推进”，并认可 API 骨架与同步 SDK 已达到本阶段目标。

## 已立即采纳

- README 顶部增加醒目的内存易失/禁止真实数据与生产使用警告。
- Uvicorn lifespan 启动时输出 development-only warning。
- OpenAPI 增加当前实现里程碑，并为 M1 外 operation 添加 `x-maturity` 标记。
- 重新执行 pytest、Ruff、Redocly、TypeScript 生成与 Python 编译，全部通过。

## 下一阶段阻断门禁

- 用 PostgreSQL/Temporal/object storage 替换进程内状态；将 answer、output/event、terminal result
  等多资源写入改造成可恢复的事务/工作流提交边界，并通过进程崩溃注入测试。
- 在接触真实数据或外部 LLM 前完成 OIDC/JWT、资源授权、RLS、出站 DLP、审计和删除 saga。
- 为 Python SDK 生成或维护强类型公共模型，并以 `mypy --strict` 和契约兼容测试验收。

## 保留为非阻断改进

- 让高层 wait timeout 同时约束单次 HTTP 请求和重试预算。
- 增加真实网络故障代理测试，覆盖 SSE 半包、断流、重复投递和长时间重连。
- 增加 collection cursor TTL、429/慢消费者治理、Metrics 与 OpenTelemetry。
