# 分类与回归完整示范案例

本页提供两套可直接上传到 Managed AutoML API 的合成数据和 Python SDK 案例。每套数据均为
360 条记录，不包含真实个人信息，并通过固定随机种子生成。

| 案例 | 数据文件 | 记录数 | 目标列 | 任务类型 | 主指标 |
| --- | --- | ---: | --- | --- | --- |
| 客户流失预测 | `data/classification_360.csv` | 360 | `churned` | `BINARY_CLASSIFICATION` | `roc_auc` |
| 月租金估计 | `data/regression_360.csv` | 360 | `monthly_rent` | `REGRESSION` | `rmse` |

两套数据都包含少量特征缺失值，用于演示 API 在训练折内拟合填补器的行为。目标列没有缺失，
不存在直接复制目标的泄漏列。

## 1. 准备服务

在仓库根目录启动 API：

```bash
uv sync --extra dev
uv run automl-api
```

另开终端设置开发凭据：

```bash
export AUTOML_API_URL=http://127.0.0.1:8000
export AUTOML_TOKEN=local-development-token
export PYTHONPATH=packages/python_sdk/src
```

生产环境应使用 OAuth2 `client_credentials` 获取短期 JWT，不使用示例开发 token。

## 2. 二分类案例

案例目标是根据客户年龄、消费、订阅时长、支持工单、套餐、区域、自动付款和最近登录间隔，预测
`churned` 是否为 `1`。

数据概况：

- 360 条记录，8 个特征和 1 个目标列。
- 目标分布：`0` 为 288 条，`1` 为 72 条，正类比例 20%。
- 21 个特征单元格为空；目标列完整。
- `positive_class=1`，主指标为 `roc_auc`，数据按 i.i.d. holdout 评估。

运行：

```bash
uv run python examples/python/sdk_classification_regression.py classification
```

当前 sklearn 参考运行的 sealed holdout ROC AUC 约为 `0.79`。具体值会随后端、依赖版本和预算变化，
它不是生产质量承诺。

成功后会下载以下聚合图片到 `example-output/classification/`：

- 指标对比图
- ROC 曲线
- Precision-Recall 曲线
- 校准曲线
- 混淆矩阵

对应的纯 HTTP Run 请求体为
[`requests/sklearn-classification-360.json`](requests/sklearn-classification-360.json)。其中
`dsv_REPLACE_ME` 需要替换为上传数据后获得的 `dataset_version_id`。

## 3. 回归案例

案例目标是根据面积、卧室数、楼龄、到中心距离、公共交通得分、片区、电梯和装修质量估计
`monthly_rent`。

数据概况：

- 360 条记录，8 个特征和 1 个目标列。
- 目标范围约为 `1193.51` 至 `10075.30`，均值约为 `5885.81`。
- 11 个特征单元格为空；目标列完整。
- 主指标为 `rmse`，方向为越小越好，数据按 i.i.d. holdout 评估。

运行：

```bash
uv run python examples/python/sdk_classification_regression.py regression
```

当前 sklearn 参考运行的 sealed holdout RMSE 约为 `282`。成功后会下载以下聚合图片到
`example-output/regression/`：

- 指标对比图
- 观测值与预测值图
- 残差与预测值图
- 残差分布图

对应的纯 HTTP Run 请求体为
[`requests/sklearn-regression-360.json`](requests/sklearn-regression-360.json)。

## 4. 指定后端和预算

默认使用 sklearn，执行两个候选试验。可以切换到 manifest 中 `available=true` 的后端：

```bash
uv run python examples/python/sdk_classification_regression.py classification \
  --backend autogluon \
  --max-trials 1
```

```bash
uv run python examples/python/sdk_classification_regression.py regression \
  --backend tabpfn \
  --max-trials 1 \
  --timeout 1800
```

TabPFN 只有在部署方已接受适用许可、配置批准的模型权重且 manifest 报告可用时才能运行。

## 5. 接入阶段 Callback

先注册 Webhook endpoint，并确保其 `event_types` 为 `*`，或者同时包含阶段完成、Run 失败和 Run
取消事件。然后运行：

```bash
uv run python examples/python/sdk_classification_regression.py classification \
  --callback-uri https://agent.example.com/hooks/automl \
  --webhook-endpoint-id wh_000000000001
```

也可以通过 `AUTOML_CALLBACK_URI` 设置 URI。接收端按 `callback.stage`、`callback.states`、
`callback.next_stage` 和 `callback.reason` 驱动 Agent 平台，完整契约见
[阶段 Callback 文档](../docs/stage-callback-contract.md)。

## 6. 逐接口输入输出文件

两个案例均已通过真实 HTTP 服务录制一遍：

- 二分类：[`api-io/classification/`](api-io/classification/)
- 回归：[`api-io/regression/`](api-io/regression/)

每个目录的 `index.json` 记录调用顺序、HTTP 状态码、数据哈希和实际主指标。每个序号都有
独立的 `*.request.json` 和 `*.response.json`：

| 序号 | 调用 | 文件名前缀 |
| ---: | --- | --- |
| 01 | 获取 Agent manifest | `01-get-agent-manifest` |
| 02 | 创建数据集上传会话 | `02-create-dataset-upload` |
| 03 | 上传 CSV 分片 | `03-upload-dataset-part` |
| 04 | 完成 DatasetVersion | `04-finalize-dataset-version` |
| 05 | 读取 DatasetVersion | `05-get-dataset-version` |
| 06 | 创建 Run | `06-create-run` |
| 07 | 读取 Run 初始状态 | `07-get-run-initial` |
| 08 | 读取 Run 终态 | `08-get-run-terminal` |
| 09 | 读取阶段状态 | `09-list-run-stages` |
| 10 | 读取事件流 | `10-read-run-events` |
| 11 | 读取外部 Agent context | `11-get-agent-context` |
| 12 | 读取外部 Agent actions | `12-list-agent-actions` |
| 13 | 列出 Run outputs | `13-list-run-outputs` |
| 14 | 读取评估 output | `14-get-evaluation-output` |
| 15 | 读取汇总结果 | `15-get-run-result` |
| 16 | 读取 artifact 元数据 | `16-get-artifact-metadata` |
| 17 | 创建 artifact 下载票据 | `17-create-artifact-download-ticket` |
| 18 | 使用票据下载图片 | `18-download-artifact-by-ticket` |

第 03 步的 JSON 以文件路径、MIME、字节数和 SHA-256 表达二进制请求体，避免在 JSON 中复制整份
CSV。第 18 步的响应 JSON 记录字节数和 SHA-256，真实响应体保存为
`18-download-artifact-by-ticket.response.png`。

记录文件不包含真实凭据：`Authorization` 固定写为 `Bearer <AUTOML_TOKEN>`，一次性 artifact
票据 URL 和必需 header 也已脱敏。重新录制时，先启动 API，再执行：

```bash
AUTOML_API_URL=http://127.0.0.1:8000 \
AUTOML_TOKEN=local-development-token \
uv run python examples/python/record_classification_regression_api_io.py
```

录制脚本会在创建 Run 时开启 `policy.allow_external_llm=true`，因为第 11–12 步需要读取外部 Agent
接口。原始 Run 请求模板仍保持更保守的 `false`。

## 7. 重新生成和验证数据

重新生成数据：

```bash
uv run python examples/python/generate_classification_regression_data.py
```

固定数据哈希为：

```text
classification_360.csv  ff76b47ff7a1b93dde983c495c55ffb7425af7691a237d74d449f2c00e29caec
regression_360.csv      1f475d0153648dbb0ce0029c22abbef2c5128081120b330919d2070d13b720c8
```

案例脚本在上传前会再次检查：数据文件存在、记录数不少于 300、目标列存在且没有缺失值。API 仍然
负责正式的数据解析、质量检查、划分、训练、评估、绘图和结果打包。
