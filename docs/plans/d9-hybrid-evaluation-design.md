# D9 设计：混合方案评估

> 状态：设计定稿（待评审确认）
> 日期：2026-11-05
> 实现计划：[d9-hybrid-evaluation-implementation-plan.md](../specs/d9-hybrid-evaluation-implementation-plan.md)
> 依据：[项目路线图](../specs/project-roadmap.md) 2.4/D9、[数据契约 v1](data-contracts.md) 第 4 节
> 上游：[D6 设计](d4-d6-design.md)（规则列）、[D8 设计](d8-llm-judge-design.md)（改判结果与缓存）

---

## 1. 目标

把"纯规则 vs 混合"的对比做成可自动校验、可手算复核的封闭证据链，产出报告两节（AI 增强 / AI 局限）引用的全部数字。

## 2. 输入与输出

- 输入：`results/features.csv`（D5/D6 产出，含 E/B/R）、`results/llm_cache.jsonl`（D8 产出，经 `llm_judge.load_cache` 读取）、冻结配置快照（D7）。
- 输出：`features.csv` 追加三列（`llm_verdict / llm_fail_reason / hybrid`，既有列值与顺序不动）、`results/metrics.json`、`run_manifest.json` 评估段。

## 3. 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 改判公式 | `hybrid = B OR (E AND verdict != normal)` | 与契约 §4 逐字一致；fail/uncertain 保留告警，LLM 只有"撤回证据充分的可疑"的权限 |
| 评估单元 | 聚合样本 `(client_ip, window_start, qname)` | 与检测/送审粒度一致，指标才对应系统真实行为 |
| 第二口径 | 唯一域名级折叠，仅入附录 | 同一恶意域被多客户端查询时聚合口径会放大计数；两口径并列防"指标口径挑好看的"质疑 |
| 改判明细四类 | 正确撤回FP / 错误放行TP / uncertain与fail保留 / 无改判 | 把子集关系拆成可点名的人话数字；"错误放行TP数"是 AI 局限的核心指标 |
| 子集断言 | 代码自动校验（FPR 不升、TP 不增），违反即报错 | 2.4 的论证不靠报告自觉，靠测试兜底 |
| 手算基准 | 8 行 fixture 覆盖 E/B/verdict 全组合 | 混淆矩阵计算正确（检查要点）的最硬证据 |
| split 使用 | 指标只在 test；val 仅冻结复核；train 只画分布 | 数据隔离红线（路线图 2.1） |

## 4. 输出物结构

`metrics.json`：两方案 × 两 split 的 TP/FP/FN/TN 原始计数 + 4 位小数比率 + 改判明细（各附 ≤5 个样本摘录，限项目自造样本）+ 缓存覆盖率；只含计数与比率，不含域名明文。

`run_manifest.json` 评估段：`features_sha256 / cache_sha256 / config_snapshot_sha256 / splits_evaluated / cache_coverage / budget_sampling_applied`，支撑 D11 断网重放。

## 5. 测试策略

- 手算 fixture 全部指标人工计算写入断言（含空分母记 None 的边界）。
- 反例测试：人为构造 hybrid FP 超出 rule FP 的数据，断言 `assert_subset_property` 必须报错并给出聚合键。
- 端到端 tmp_path 测试：metrics.json 字段可由输入逐格复算；受限列不出现在任何输出文件。
- 每步跑全套 pytest，不改 D1–D8 已锁行为。

## 6. 明确不做

不做传统分类器对照（选做项，砍单里排第一）；不在评估阶段重算特征或调用 LLM（纯离线计算，输入即冻结产物）。

## 7. 完成判据

- 手算基准测试与全套 pytest 通过；混淆矩阵可人工复核。
- `FPR_hybrid ≤ FPR_rule` 自动断言通过，且同时报告"错误放行 TP 数"（FNR 上升侧），两口径并列呈现。
- 报告"AI 增强/AI 局限"两节的全部数字可追溯到 metrics.json 的单一字段。
