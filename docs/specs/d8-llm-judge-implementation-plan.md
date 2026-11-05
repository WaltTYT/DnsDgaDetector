# D8 LLM 改判引擎实现计划（组长）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 全部行为遵循数据契约 v1 第 4/5 节与 D7 模板的输出强约束。

**Goal:** 在 `src/llm_judge.py` 现有骨架（`get_api_key`、`--smoke`，组员A已实现）上扩展语义改判引擎：只送 `E=1 且 B=0` 的样本，严格 JSON 三态判定，重试与失败兜底、本地缓存、离线模式与预算守卫；真实批量调用在数据到位后执行。

**Architecture:** `src.llm_judge` 提供纯函数筛选/渲染/解析 + 有副作用的调用/缓存层，二者分离以便 mock 测试。缓存 `results/llm_cache.jsonl` 以 `cache_key` 为主键，字段与契约第 5 节逐字一致；`offline` 模式绝不发起网络请求，只读缓存并报告覆盖率。预算守卫按唯一 qname 计数，超限走分层抽样并记录规则。

**Tech Stack:** Windows PowerShell、Python 3.11.17、标准库 `json`、`hashlib`、`re`、`time`、`random`，现有 `requests==2.32.3` 与 `pytest==9.1.1`；不新增依赖。

---

## 依据与边界

- 设计依据：[项目路线图](project-roadmap.md) 2.4 改判规则、2.5 调用预算与缓存、D8 检查点；[D7 计划](d7-prompt-and-freeze-implementation-plan.md) 的输出格式契约。
- 只修改 `src/llm_judge.py`、新建 `tests/test_llm_judge.py`、运行时写 `results/llm_cache.jsonl`；不改 D4–D6 模块与 `config.json`（模型参数已冻结）。
- 单元测试一律 mock HTTP（monkeypatch `requests.post`），不发真实请求、不需要 API Key；真实调用仅发生在 Task 6 的显式命令中。
- 输入给 LLM 的字段仅 `qname` 与 `registrable_label`；`label/source/algorithm/split` 及任何统计值不得进入请求体、缓存或日志。
- 解析失败、超时、限流一律 `verdict=uncertain` + 对应 `fail_reason`，保留规则原判；禁止编造判定。
- Key 仅经 `get_api_key()`；任何输出文件零 Key。受限数据产物只在本机使用，不提交。
- 不在本计划中提交、合并或推送；提交需用户明确许可。

## 文件映射

| 文件 | 责任 |
|---|---|
| `src/llm_judge.py` | 保留 `get_api_key`/`--smoke`，新增筛选、渲染、调用、解析、缓存、离线、预算接口 |
| `tests/test_llm_judge.py` | mock 测试：纯函数、错误路径、缓存一致性、离线、预算 |
| `results/llm_cache.jsonl` | 运行时缓存；一行一 JSON 对象，字段见契约 §5 |
| `prompts/dga_judge_prompt.md` | 只读输入（模板与 hash） |

## 接口约定

`src/llm_judge.py` 至少提供以下接口，测试从模块导入：

| 名称 | 签名 | 责任 |
|---|---|---|
| `VerdictResult` | `verdict, reason, fail_reason, latency_ms, cache_hit, raw_response` | 单次判定结果值对象；`verdict ∈ {DGA, normal, uncertain}`，`fail_reason ∈ {None, timeout, rate_limit, server_error, format_error, no_cache}` |
| `select_judgeable` | `(features_df: pd.DataFrame) -> pd.DataFrame` | 取 `E==1 AND B==0`，按规范化 `qname` 去重（保留首条聚合键）；输入表头先过契约 §4 校验 |
| `build_prompt` | `(qname: str, registrable_label: str, template: str) -> str` | 渲染 D7 模板；断言渲染结果不含元数据字段名 |
| `cache_key` | `(qname, registrable_label, model, template_hash, params, endpoint) -> str` | sha256；params 取 `config.json.model` 全部生成参数 |
| `parse_verdict` | `(raw_text: str) -> tuple[str, str]` | 容忍代码围栏与首尾空白；枚举校验失败抛 `ValueError`；reason 截断至 120 字 |
| `call_llm` | `(prompt: str, cfg: dict) -> tuple[str, int]` | 单次 HTTP（timeout、无自动重试）；返回原始文本与延迟 ms；429/5xx/超时抛分类异常 |
| `judge_domain` | `(qname, registrable_label, mode: str, cfg: dict) -> VerdictResult` | 编排：查缓存→命中返回；`offline` 未命中返回 `no_cache`；`online` 调用 `call_llm` 带 `max_retries` 指数退避→解析→写缓存；任何失败返回 uncertain + fail_reason |
| `load_cache` / `append_cache` | `(path: Path) -> dict[str, dict]` / `(entry: dict, path: Path) -> None` | 缓存读写；`prompt_template_hash` 或 params 与当前不符视为未命中；追加写、不重复 key |
| `budget_guard` | `(qnames: list[str], max_unique: int, seed: int) -> tuple[list[str], dict]` | 唯一 qname ≤ max_unique 全放行；超限按 `source×algorithm` 分层随机抽样，返回选中清单与抽样规则 dict（供 run_manifest） |
| `run_judging` | `(features_path, mode, config_path=None) -> dict` | 端到端：筛选→预算→逐一改判→缓存落盘→stdout JSON 摘要（覆盖率、各 fail_reason 计数、请求数） |
| `main` | `() -> int` | CLI：`--smoke`（保留）、`--online`、`--offline`、`--features PATH`；默认 offline |

## 确定性规则

- 退避序列固定 `[1s, 2s, 4s]`（最多 `max_retries=3` 次重试，共 4 次尝试）；测试用注入 clock 断言。
- 缓存条目 `created_at` 为 UTC ISO 8601 毫秒；`raw_response` 不截断。
- 分层抽样使用 `random.Random(seed)`，seed 取 `config.json.seed`；同一输入两次运行选中集合一致。
- `run_judging` 摘要字段：`requested_unique, cache_hits, verdict_counts, fail_reason_counts, offline_coverage, budget_applied, sampling_rule|None`。

## 验证环境

```powershell
$python = "D:\study\miniconda\envs\dns-dga\python.exe"   # 按 AGENTS.md 环境节调整
if (-not (Test-Path $python)) { throw "Python not found: $python" }
```

## Task 1：筛选与渲染（纯函数，先测后写）

- [ ] 写测试：`E=1,B=0` 才入选；`B=1` 即使 `E=1` 不送审（行为告警不放行）；同 qname 多聚合键去重保留首条；表头不符契约报错。
- [ ] 写测试：`build_prompt` 输出含 qname 与 registrable_label、不含 `label/source/algorithm/split` 字样；未知占位符报错。
- [ ] 运行 `& $python -m pytest tests/test_llm_judge.py -q` 确认失败来自接口未定义。
- [ ] 实现 `select_judgeable`、`build_prompt`；重跑通过。

## Task 2：解析与缓存键

- [ ] 测试 `parse_verdict`：合法三态、带围栏、多余空白、缺字段、非法枚举、超长 reason；非法一律抛错。
- [ ] 测试 `cache_key`：六要素任一变化即不同；相同输入稳定。
- [ ] 实现两函数；重跑通过。

## Task 3：调用编排与失败兜底（mock HTTP）

- [ ] 测试 `judge_domain` online：monkeypatch `call_llm` 成功/超时/429/5xx/坏 JSON 五路径，断言 `VerdictResult` 的 verdict 与 fail_reason 映射、退避序列（注入 clock）、最终失败保留 uncertain。
- [ ] 测试缓存命中不发请求；`offline` 未命中返回 `no_cache` 且零网络调用（socket 封锁自检）。
- [ ] 实现 `call_llm`、`load_cache`、`append_cache`、`judge_domain`；重跑通过。

## Task 4：预算守卫与端到端摘要

- [ ] 测试 `budget_guard`：≤800 全放行；超限分层抽样确定性（同 seed 两次一致）、规则 dict 完整。
- [ ] 测试 `run_judging` 端到端（tmp_path 造 features.csv + mock 调用）：摘要计数与逐条结果可复算；受限列不出现在任何输出。
- [ ] 实现 `run_judging`、`main`（保留 `--smoke`）；`& $python -m pytest tests/ -q` 全套无回归。

## Task 5：真实批量改判（数据到位后）

- [ ] `& $python -m src.llm_judge --smoke` 确认连通（已有 D1 记录，重验一次即可）。
- [ ] `& $python -m src.llm_judge --online --features results/features.csv` 执行批量改判；核对 stdout 摘要与预算（≤800）。
- [ ] 一致性抽检（预算允许）：随机 30 个送审样本各重复调用 3 次，记录改判翻转率，结果存 `report/evidence/d8_consistency.json`。

## D8 完成判据

- mock 测试全绿且零真实网络请求；离线模式 socket 封锁下可完整运行。
- 失败一律 uncertain + fail_reason 单列统计，缓存缺失不伪造 verdict。
- LLM 输入零元数据泄漏；输出文件零 Key。
- 真实批量调用摘要可复算，预算超限有分层抽样记录。
