# D8 设计：LLM 语义改判引擎

> 状态：设计定稿（待评审确认）
> 日期：2026-11-05
> 实现计划：[d8-llm-judge-implementation-plan.md](../specs/d8-llm-judge-implementation-plan.md)
> 依据：[项目路线图](../specs/project-roadmap.md) 2.4/2.5/D8、[数据契约 v1](data-contracts.md) 第 4/5 节
> 上游：[D7 设计](d7-prompt-and-freeze-design.md)（模板与冻结快照）；下游：[D9 设计](d9-hybrid-evaluation-design.md)（消费改判结果）

---

## 1. 目标与交接基线

对规则筛出的可疑样本做语义二次判断：调用、校验、重试、缓存、离线、预算控制全链路工程化，失败路径绝不伪造结论。

交接基线：`llm_judge.py` 已有 `get_api_key()`（环境变量优先、`api.txt` 回退）与 `--smoke`（组员A，D1）；`config.json` 模型参数已定稿（GLM-4-Flash、timeout 30、retries 3、temperature 0、max_tokens 32、预算 800）。真实运行数据因许可限制待组员A私密传输，任务按"无数据 mock 开发"与"数据到位真实调用"两批切分。

## 2. 送审范围：E=1 且 B=0（核心设计）

```
送审集 = {聚合样本 | E=1 AND B=0}，按规范化 qname 去重
```

- **为什么不送 B=1**：行为告警（高频/低间隔）是规则引擎的硬证据，LLM 只看词形，没有否决行为依据的信息——放行 B=1 样本会造成"语义正常即可洗白高频查询"的漏洞。
- **为什么 E=0 不送**：省预算，且保持"LLM 是二次过滤而非主力检测"的定位。
- 由此得子集关系（D9 自动校验）：`FP_hybrid ⊆ FP_rule`、`TP_hybrid ⊆ TP_rule`，故 **FPR 必不升、FNR 可能升**——这正是"AI 增强 + AI 局限"的论证骨架。

## 3. 失败语义

| 场景 | verdict | fail_reason | 对 hybrid 的影响 |
|---|---|---|---|
| 正常返回且校验通过 | DGA/normal/uncertain | 无 | normal 撤回告警，其余保留 |
| 超时/限流/5xx（重试耗尽） | uncertain | timeout/rate_limit/server_error | 保留告警 |
| 返回不可解析 | uncertain | format_error | 保留告警 |
| 离线模式缓存缺失 | uncertain | no_cache | 保留告警 |

失败与"模型不确定"**分开统计**：前者是工程可靠性问题，后者是能力边界问题，报告"AI 局限性"引用的是后者。

## 4. 缓存与预算

- 主键 `cache_key = sha256(输入 + model + 模板hash + 生成参数 + 接口标识)`：任一要素变化即天然失效，答辩断网演示直接读缓存重放。
- `raw_response` 全量落盘不截断：失败案例分析需要原始文本证据。
- 预算守卫：唯一 qname ≤800 全放行；超限按 `source×algorithm` 分层随机抽样（固定 seed，可复算），抽样规则进 run_manifest——答辩被问"为什么只复核这些"有确定答案。

## 5. 安全边界

Key 仅经 `get_api_key()`；请求体、缓存、日志、摘要零 Key 零元数据；测试全部 mock HTTP，socket 封锁自检证明离线模式零网络（沿用 D3 手法）。

## 6. 一致性抽检（预算允许）

随机 30 个送审样本各重复调用 3 次，记录改判翻转率进 `report/evidence/d8_consistency.json`。temperature=0 下仍有翻转即证明"同输入不同判"是模型固有现象，是局限性一节的定量素材。

## 7. 风险与降级

| 风险 | 应对 |
|---|---|
| 免费额度限流严重 | 退避重试 + 缓存断点续跑（已成功 qname 不重复消耗）；必要时降低送审集（只送 E=1∧B=0∧H 最高的前 N）并记录 |
| 模板返工 | hash 失效机制保证旧缓存不污染新指标，代价仅是重跑预算 |
| LLM 大面积 uncertain | 不视为失败：hybrid=rule，指标退化为纯规则，如实报告覆盖率——本身就是合格的局限性结论 |

## 8. 完成判据

- mock 测试全绿且零真实网络请求；离线模式 socket 封锁下完整可运行。
- 失败一律 uncertain + fail_reason 单列统计，缓存缺失不伪造 verdict。
- 真实批量调用摘要可复算，预算超限有分层抽样记录。
