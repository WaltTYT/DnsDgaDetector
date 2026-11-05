# D9 混合方案评估实现计划（组长）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 全部列名、取值枚举与数据契约 v1 第 4 节逐字一致。

**Goal:** 实现 `src/evaluate.py`：把 D8 改判结论写回 `features.csv` 三列，按 `hybrid = B OR (E AND verdict != normal)` 得出混合结论，输出纯规则与混合两方案的混淆矩阵/误报率/漏报率，并自动校验子集关系（FPR 不升、FNR 可能升）。

**Architecture:** `src.evaluate` 为纯计算模块，输入是 D5/D6 产出的 `features.csv` 与 D8 的 `llm_cache.jsonl`（经 `llm_judge.load_cache` 读取），输出 `results/metrics.json` 与 `results/run_manifest.json` 评估段。评估单元固定为聚合样本 `(client_ip, window_start, qname)`；唯一域名级第二口径作为附录另表输出。手算 fixture 测试不依赖真实数据。

**Tech Stack:** Windows PowerShell、Python 3.11.17、标准库 `json`、`hashlib`，现有 `pandas` 与 `pytest==9.1.1`；不新增依赖、不发起网络请求。

---

## 依据与边界

- 设计依据：[项目路线图](project-roadmap.md) 2.4 改判规则与子集关系论证、D9 检查点；[D8 计划](d8-llm-judge-implementation-plan.md) 的 `VerdictResult`/缓存接口。
- 只修改 `src/evaluate.py`、新建 `tests/test_evaluate.py`、运行时写 `results/metrics.json` 与 run_manifest 评估段；不改 D4–D6 与 D8 模块；`features.csv` 追加列不改动既有列的值与顺序。
- 评估只在 `split=test`（最终指标）与 `split=val`（冻结复核）上计算；`train` 仅用于分布图，不产指标。
- uncertain / fail_reason 非空的样本一律保留规则原判（hybrid=R），并单列统计，不计入"改判"。
- 受限数据产物只在本机使用；`metrics.json` 只含计数与比率，不含域名明文清单（失败案例摘录除外，且仅限项目自造样本）。
- 不在本计划中提交、合并或推送；提交需用户明确许可。

## 文件映射

| 文件 | 责任 |
|---|---|
| `src/evaluate.py` | 写回三列、两方案指标、改判明细、子集断言、唯一域名口径、manifest 评估段 |
| `tests/test_evaluate.py` | 手算基准 fixture、公式逐字测试、断言路径测试 |
| `results/metrics.json` | 运行时输出：两方案指标 + 改判明细 + 覆盖率 + 口径说明 |
| `results/run_manifest.json` | 追加评估段：数据哈希、配置快照引用、抽样规则、缓存覆盖率 |

## 接口约定

`src/evaluate.py` 至少提供以下接口，测试从模块导入：

| 名称 | 签名 | 责任 |
|---|---|---|
| `apply_hybrid` | `(features_df, verdicts: dict[str, dict]) -> pd.DataFrame` | 按 qname 关联 D8 结果，追加 `llm_verdict / llm_fail_reason / hybrid` 三列；未送审 `llm_verdict='na'`；`hybrid = B or (E and verdict != 'normal')`；返回列序符合契约 §4 |
| `compute_metrics` | `(df, pred_col: str, split: str) -> dict` | 指定 split 上算 TP/FP/FN/TN、`fpr=FP/(FP+TN)`、`fnr=FN/(TP+FN)`、precision、recall；分母为 0 记 None 并保留原始计数 |
| `reversal_detail` | `(df, split: str) -> dict` | 改判明细四类计数：正确撤回 FP（E=1,B=0,label=0,verdict=normal）、错误放行 TP（label=1 同条件）、保留告警 uncertain/fail 数、无改判数；各附 ≤5 个样本摘录 |
| `assert_subset_property` | `(rule: dict, hybrid: dict, split: str) -> None` | 断言 `hybrid.FP ⊆ rule.FP` 且 `hybrid.TN ⊇ rule.TN`（等价 FPR 不升）、`hybrid.TP ⊆ rule.TP`；违反即 AssertionError，附反例聚合键 |
| `unique_domain_metrics` | `(df, split: str) -> dict` | 第二口径：按唯一 qname 折叠聚合样本后重算两方案指标（附录用） |
| `evaluate_all` | `(features_path, cache_path, config_path=None) -> dict` | 端到端：读入→写回三列→test/val 两 split 全部计算→子集断言→写 metrics.json 与 manifest 评估段→返回摘要 |
| `main` | `() -> int` | CLI：`--features PATH --cache PATH [--out PATH]`；stdout 仅 JSON 摘要 |

## 计算规则

- 评估前全表过契约 §4 校验（列名、顺序、枚举），失败即报错，不静默兼容。
- `verdict != 'normal'` 判定中，`na` 不会出现于送审集合（E=1,B=0 必有 verdict 或 fail_reason）；fail_reason 非空一律视为 uncertain。
- 混淆矩阵按聚合样本计数；`metrics.json` 同时记录每格原始计数与比率（4 位小数），保证可手算复核。
- run_manifest 评估段字段：`features_sha256, cache_sha256, config_snapshot_sha256, splits_evaluated, cache_coverage, budget_sampling_applied`。

## 验证环境

```powershell
$python = "D:\study\miniconda\envs\dns-dga\python.exe"   # 按 AGENTS.md 环境节调整
if (-not (Test-Path $python)) { throw "Python not found: $python" }
```

## Task 1：手算基准与写回公式（先测后写）

- [ ] 在 `tests/test_evaluate.py` 用 8 行手算 fixture（覆盖 E/B/verdict 全组合：含 B=1 不放行、uncertain 保留、fail 保留、normal 撤回、na 未送审）断言 `apply_hybrid` 三列逐值正确、列序符合契约。
- [ ] 断言 `hybrid` 公式与契约逐字一致：任何 `B=1` 行 hybrid=1，无论 verdict。
- [ ] 运行 `& $python -m pytest tests/test_evaluate.py -q` 确认失败来自接口未定义；实现后重跑通过。

## Task 2：指标与改判明细

- [ ] 测试 `compute_metrics`：fixture 混淆矩阵手算 TP/FP/FN/TN/fpr/fnr/precision/recall 全对；空分母记 None。
- [ ] 测试 `reversal_detail` 四类计数与样本摘录上限。
- [ ] 实现两函数；重跑通过。

## Task 3：子集关系断言

- [ ] 测试 `assert_subset_property`：正常 fixture 通过；人为构造 hybrid FP 超出 rule FP 的反例必须抛 AssertionError 并给出聚合键。
- [ ] 实现并测试 test/val 双 split 调用路径；重跑通过。

## Task 4：端到端与输出文件

- [ ] 测试 `evaluate_all`（tmp_path 造 features.csv + llm_cache.jsonl）：metrics.json 字段完整、比率与计数可互相推出；manifest 评估段哈希与输入一致；stdout 仅 JSON。
- [ ] 测试唯一域名口径 `unique_domain_metrics` 与聚合口径在 fixture 上的折叠正确性。
- [ ] 实现 `unique_domain_metrics`、`evaluate_all`、`main`；`& $python -m pytest tests/ -q` 全套无回归。

## Task 5：真实数据评估（数据与 D8 结果到位后）

- [ ] `& $python -m src.evaluate --features results/features.csv --cache results/llm_cache.jsonl` 产出正式 `metrics.json`。
- [ ] 核对：test split 两方案指标、改判明细、缓存覆盖率、预算抽样记录；子集断言通过。
- [ ] 把失败案例（错误放行的真 DGA、顽固 uncertain）摘录进报告"AI 局限性"素材清单；唯一域名口径表放附录。

## D9 完成判据

- 手算基准测试与全套 pytest 通过；混淆矩阵可人工复核。
- `FPR_hybrid ≤ FPR_rule` 自动断言通过，且同时报告"错误放行 TP 数"（FNR 上升侧），两口径并列呈现。
- metrics.json / run_manifest 评估段完整，断网可重放（D11 依赖此文件而非网络）。
- 报告"AI 增强/AI 局限"两节的全部数字可追溯到 metrics.json 的单一字段。
