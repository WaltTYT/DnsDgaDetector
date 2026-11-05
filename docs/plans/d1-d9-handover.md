# D1–D9 交接文档

> 用途：三人小组内部交接。记录当前完成度、产物位置、数据缺口、待办顺序与冻结红线。
> 更新时间：2026-11-05
> 权威依据：`docs/specs/project-roadmap.md`（阶段划分）、`docs/plans/data-contracts.md` v2（共享产物字段）

## 1. 进度总览

| 阶段 | 负责人 | 代码/文档 | 真实数据 | 说明 |
|---|---|---|---|---|
| D1 基础 | 组长 | 完成 | — | 环境、依赖锁定、`src/dns_demo.py` |
| D2 数据准备 | 组员A | 完成 | **部分** | 难负样本 100 条已就位；正常榜单、公开 DGA、PSL、手工 DGA、`splits.csv` 未生成 |
| D3 日志生成 | 组员A | 完成 | **未生成** | `data/dns_log.csv` 不存在 |
| D5 特征 | 组员B | 完成 | **未生成** | `results/features.csv` 仅表头 |
| D6 基线/规则 | 组长 | 完成 | **未拟合** | `results/baseline.json` 为 0 字节 |
| D7 提示与冻结 | 组长 | Task 1–3 完成 | **Task 4 未做** | 模板 v1 已定稿，冻结检查等待 D6 结果 |
| D8 LLM 复核 | 组长 | Task 1–4 完成 | **Task 5 未做** | 全部 mock 测试通过；未发起真实批量调用 |
| D9 混合评估 | 组长 | Task 1–4 完成 | **Task 5 未做** | 手算与端到端测试通过；未产出正式 `metrics.json` |
| D10–D12 | 组长 | 未开始 | 未开始 | 依赖 D9 的真实结果 |

> 一句话结论：**D1–D9 的代码与单元测试均已落地，整条链路尚未用真实数据跑通。** 卡点是数据，不是实现。

## 2. 代码与文件地图

| 文件 | 职责 | 状态 |
|---|---|---|
| `src/prepare_data.py` | D2 规范化、去重、冲突处理、eTLD+1 划分 | 已实现，已适配契约 v2 |
| `src/gen_dga.py` | 三种手工 DGA 生成算法 | 已实现 |
| `src/gen_log.py` | D3 合成 DNS 查询日志 | 已实现，未执行 |
| `src/features.py` | D5 H/F/I 特征与 60 秒窗口聚合 | 已实现，未执行 |
| `src/baseline.py` | D6 训练集正常样本分位数阈值基线 | 已实现，未拟合 |
| `src/rule_engine.py` | D6 规则 `R = E OR B` 与证据输出 | 已实现 |
| `src/llm_judge.py` | D8 筛选、渲染、解析、缓存、预算、调用编排 | 已实现，mock 覆盖 |
| `src/evaluate.py` | D9 写回、双口径指标、改判明细、子集断言 | 已实现，mock 覆盖 |
| `src/dns_demo.py` | D1 教材原理演示 | 已实现 |
| `run_all.py` | D11 一键流水线 | **仍是骨架**，待实现 |
| `prompts/dga_judge_prompt.md` | D7 提示模板 v1，含 `template_hash` | 已定稿 |
| `tests/` | 8 个测试文件 | 全套 315 passed |

`config.json` 当前值：`seed=20261101`、窗口 60 秒、`H/F=0.99`、`I=0.01`、GLM-4-Flash、`temperature=0`、唯一请求预算 800。

## 3. 数据契约 v2 交接要点

- 五类共享产物：`data/dns_log.csv`、`data/splits.csv`、`data/dga_domains.csv`、`results/features.csv`、`results/llm_cache.jsonl`。
- `source` 四值：`normal_list` / `hard_negative` / `public_dga` / `manual_dga`；`hard_negative` 必须 `label=0`、`algorithm` 为空。
- 主评估单元 `(client_ip, window_start, qname)`；副口径为唯一完整域名（任一窗口告警即检出）。
- `hybrid = B OR (E AND verdict != normal)`；`B=1` 不放行；失败/缺失按 `uncertain` 保留告警。
- 缓存键含输入域名、模型、模板哈希、生成参数等六要素，改模板必须升版本使旧缓存失效。
- 基线阈值只拟合 `split=train` 的正常样本，阈值只在 `val` 上选择，`test` 只做最终评估。
- `label/source/algorithm/split` 不得进入特征或 LLM 输入，也不得被基线/提示读取测试集。

## 4. 当前数据现状与缺口

**已就位**

- `data/raw/hard_negative__opendns_top10k_digits.txt`（60 条）
- `data/raw/hard_negative__opendns_top10k_dga_like.txt`（20 条）
- `data/raw/hard_negative__opendns_top10k_cdn_cloud.txt`（20 条）
- 来源、许可、哈希与逐条行号依据已写入 `data/sources.md`
- `data/dga_domains.csv`：仅表头

**缺失（按依赖顺序）**

1. `data/raw/normal_list__*.txt`（Majestic Million 或替代榜单，约 3,000 条）
2. `data/raw/public_dga__*.csv`（建议 360netlab/DGA，需先核验家族语义与许可）
3. `data/raw/public_suffix_list.dat`（固定快照，含 PRIVATE 区）
4. `data/dga_domains.csv` 的 300 条手工样本（`python -m src.prepare_data` 生成）
5. `data/splits.csv`（D2 输出，60/20/20，按 eTLD+1 分组）
6. `data/dns_log.csv`（D3，约 50,000 行）
7. `results/features.csv`（D5 实算结果）
8. `results/baseline.json`（D6 拟合结果）
9. `results/llm_cache.jsonl`、`results/metrics.json`、`results/run_manifest.json`（D8/D9）

`data/sources.md` 中“正常域名榜单”“公开 DGA 数据集”“PSL 快照”三节仍是待填写模板。

## 5. 待办清单（按阻塞关系）

1. **组员A**：补齐 D2 剩余来源 → 填完 `data/sources.md` 三节 → 运行 `python -m src.prepare_data` 产出 `splits.csv` 与 `dga_domains.csv`。
2. **组员B**：运行 D3 产出 `dns_log.csv` → 运行 D5 产出 `results/features.csv`。
3. **组长**：D6 拟合 `baseline.json`，在 val 上确认 `H/F=0.99`、`I=0.01` → D7 Task 4 冻结（写 `run_manifest.json`）。
4. **组长**：D8 Task 5 先 `--smoke` 测时，再 `--online` 批量改判（唯一请求 ≤800）。
5. **组长**：D9 Task 5 产出正式 `metrics.json`，核对覆盖率与失败案例。
6. **组长**：D10 图表与双口径表 → D11 `run_all.py` → D12 报告素材。

## 6. 冻结红线与注意事项

- D7 冻结后，阈值、提示模板、模型配置不得再改；确需改动必须重开检查点并在报告说明理由。
- 提示模板的 `template_hash` 只覆盖正文（首个 `---` 之后），改正文必须升 `version` 并更新哈希；头部第 10–11 行仍写“数据契约 v1”，属待更新的文字表述，不改变正文哈希，但交接时需知会。
- 难负样本由 loader 统一赋 `label=0`、空 `algorithm`；同一域名同时出现在正常榜单与难负清单时归入 `hard_negative`，摘要输出 `hard_negative_promoted`。
- 断网演示前必须确认 `tldextract` 走固定本地 PSL；`features.py` 目前构造 `TLDExtract(cache_dir=None, fallback_to_snapshot=False)` 且未读取 `config.json` 的 `psl_snapshot`，**首跑可能联网**，需组员B在 D5 落地前确认或改造。
- API Key 只从环境变量 `ZHIPU_API_KEY` 或根目录 `api.txt` 回退读取，不得写入代码、缓存、截图与报告。
- 当前改动全部未提交（用户要求先不提交）。提交时按 `AGENTS.md` 8.1 格式，按分工拆分。

## 7. 验证状态（2026-11-05）

| 检查 | 结果 |
|---|---|
| `python -m pytest tests/ -q` | **315 passed in 31.89s** |
| D7–D9 相关（`test_llm_judge.py`、`test_evaluate.py`、`test_decisions.py`） | 38 passed in 4.23s |
| 难负样本规范化校验 | 100 行 / 100 有效 / 0 排除 / 100 唯一 |
| `load_source_rows` 全目录 | 因缺 `normal_list` 类别报错（预期，正常榜单未采集） |

## 8. 已知限制与风险

- 难负样本未包含具体 CDN 哈希子域实例（如 `<hash>.cloudfront.net`）：D2 禁止 DNS 查询且无可逐条核验的公开主机名清单，该类别由 CDN/云平台可注册域名覆盖，已在 `data/sources.md` 记录。
- OpenDNS 榜单每周更新，换快照会改变域名集合与行号，需同步更新三个派生文件与哈希。
- 公开 DGA 家族标签未逐域名进入契约，只以 `source=public_dga` 区分；若需按家族独立评估，必须先评审升级契约。
- 合成标签只表示已知生成类别，报告与结论不得表述为现实恶意证明。
