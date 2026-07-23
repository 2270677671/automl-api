# API-first 阶段评审处置记录

## 结论

API-first 设计阶段可以结束并进入 API skeleton 与 Python SDK 同步实现。最终 Gemini 独立评估为 97/100，结论是“立即推进”；三类专门 Agent 报告的 P1 均已关闭。

## 多 Agent 问题处置

| 评审面 | 主要问题 | 最终处置 |
|---|---|---|
| 输出与结果 | 事件 payload 过宽、output type/kind 可冲突、终态结果可出现非法组合 | `RunEvent`、`OutputResource`、`RunResult` 全部改为判别联合；取消与过期事件拆分 |
| 连续性 | SSE 自动重连 query/header 冲突、乱序投影跨 gap 发布、取消可能 revision 饥饿 | `Last-Event-ID` 覆盖旧 query；公共水位只推进连续 seq；取消无需 `If-Match` |
| 并发 | answer/approval 使用 Run 级 ETag，可能受不相关更新影响 | 分别绑定 `wait_set_revision`、`evidence_version`；暂停/恢复才使用 Run 控制 revision |
| 游标 | 事件与集合分页错误复用同一恢复动作 | 拆分两类 410；事件缺口返回不可恢复 `lost_event_range`，集合从第一页去重恢复 |
| Webhook | Bearer 误继承、HMAC key/raw body 不明确、无轮换/投递/重投闭环 | `security: []`；固定 key 编码、字节签名和跨语言向量；加入轮换、查询、熔断、启用、重投 API |
| 死信 | 自动重试上限和耗尽后的可见性不明确 | 20 次或 72 小时后 `EXHAUSTED`；它就是租户可见死信，保留并允许重投 30 天 |
| 生命周期 | 活动 Run 与 Dataset 删除关系不明确 | 固定级联取消；`DeletionJob.affected_run_ids` 返回影响范围，legal hold 等才返回 409 |
| 下载 | Ticket “短期”但 TTL/续传未冻结 | 固定 900 秒；每个请求开始鉴权，过期后用新幂等键换票并按稳定 ETag Range 续传 |

## 最终验证

- OpenAPI 3.1：33 paths、36 operations、113 schemas。
- Redocly recommended lint：0 error、0 warning。
- openapi-typescript 7.13.0：成功生成 3,107 行类型。
- Python 与 Node.js 对同一含中文 Webhook 测试向量生成相同 HMAC。
- `git diff --check`：通过。

## 下一阶段边界

下一阶段只实现纵向闭环：dataset upload、RunSnapshot/events、DecisionPacket answer、OutputResource、RunResult、artifact ticket，以及封装重连和恢复语义的 Python SDK。真实 LLM 规划和真实用户数据仍按里程碑延后。
