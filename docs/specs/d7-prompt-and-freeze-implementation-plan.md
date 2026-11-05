# D7 提示模板与阈值冻结实现计划（组长）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 模板输出格式必须与数据契约 v1 第 4/5 节一致。

**Goal:** 定稿可复用的 LLM 判定提示模板（严格 JSON 三态输出），并在验证集混淆矩阵评审后冻结全部实验参数。

**Architecture:** `prompts/dga_judge_prompt.md` 是唯一模板载体，渲染变量仅 `{qname}`、`{registrable_label}`；模板头部记录版本与 `prompt_template_hash`，供 D8 缓存失效判定。冻结检查点复用 D6 `rule_engine.evaluate_candidates` 的输出，三人确认后把阈值、模型参数、seed 快照写入 `results/run_manifest.json`。

**Tech Stack:** Windows PowerShell、Python 3.11.17、标准库 `hashlib`、`json`、`pathlib`；不新增依赖。

---

## 依据与边界

- 验收依据：[项目路线图](project-roadmap.md) 2.4、2.5、D7 检查点；输出字段遵循 [数据契约 v1](../plans/data-contracts.md)。
- 只修改 `prompts/dga_judge_prompt.md`、`results/run_manifest.json`；不改 D4–D6 模块、不改 `config.json` 阈值以外的字段；阈值冻结后 `config.json.threshold_candidates` 标记为已定稿。
- 模板与 few-shot 示例不得包含任何真实训练/验证/测试集中的域名作为正例答案以外的信息；few-shot 用自造示例。
- 冻结检查点（Task 4）依赖组员A私密传输的 `data/` 产物；Task 1–3 无数据即可执行。
- 不在本计划中提交、合并或推送；提交需用户明确许可。

## 文件映射

| 文件 | 责任 |
|---|---|
| `prompts/dga_judge_prompt.md` | 模板正文、few-shot、输出约束、版本与 hash 记录 |
| `results/run_manifest.json` | 冻结快照：seed、阈值、模型参数、模板 hash、数据哈希、抽样规则 |
| `docs/specs/d7-prompt-and-freeze-implementation-plan.md` | 本计划 |

## 接口约定

| 名称 | 签名/字段 | 责任 |
|---|---|---|
| 模板变量 | `{qname}`, `{registrable_label}` | 仅这两个占位符；禁止出现真值/来源/算法/split |
| 输出 JSON | `{"verdict":"DGA\|normal\|uncertain","reason":"≤40字"}` | 唯一允许的响应形态；`max_tokens=32` |
| `template_hash()` | `(path: Path) -> str` | sha256 前 12 位，写入模板头部与 D8 缓存条目 |

## 验证环境

```powershell
$python = "D:\study\miniconda\envs\dns-dga\python.exe"   # 按 AGENTS.md 环境节调整
if (-not (Test-Path $python)) { throw "Python not found: $python" }
```

## Task 1：输出格式与变量契约

- [ ] 在模板文件先写"输出格式"节：三态枚举、reason 长度上限、仅输出一个 JSON 对象、禁止围栏外文本；标注该节为 D8 解析器的强约束来源。
- [ ] 明确渲染规则：`{qname}` 为契约 §1 规范化后的域名，`{registrable_label}` 来自 D5 `extract_label`；两变量之外零输入。

## Task 2：模板正文与 few-shot

- [ ] 写系统角色（DNS 安全分析师）与任务定义（仅凭词形语义判断是否为算法生成域名，行为证据不可见）。
- [ ] 判定标准三条：随机拼接/无语义组合→DGA；真实词/已知服务→normal；拿不准→uncertain。
- [ ] few-shot 4 例：2 个明显 DGA、1 个真实词组合正常、1 个难负样本（CDN 哈希子域 → uncertain），全部自造。
- [ ] 用 `& $python -c` 渲染 2 个测试域名，人工检查最终 prompt 无元数据泄漏。

## Task 3：版本与失效机制

- [ ] 实现 `template_hash()` 并在模板头部记录 `version: v1` 与 hash 值；说明改模板必须升版本、旧 hash 缓存自动失效（契约 §6.2）。
- [ ] 运行 `& $python -m pytest tests/ -q` 确认无回归（本任务不新增测试文件）。

## Task 4：冻结检查点（数据到位后）

- [ ] 运行 D2→D3→D5→D6 全链路（组员A产物就位后），得到验证集混淆矩阵与候选比较表。
- [ ] 三人评审：确认 `H_quantile=0.99/F_quantile=0.99/I_quantile=0.01` 候选的验证集 FP/FN 可接受，记录选择理由（含小样本 P99 噪声说明）。
- [ ] 冻结：把阈值、seed、模型参数、模板 hash、输入数据 SHA-256 写入 `results/run_manifest.json`；此后 D8/D9 只读该快照，任何参数变更必须重开检查点。

## D7 完成判据

- 模板可直接复制进报告附录，评审人不读代码即可复用；few-shot 覆盖三态与难负样本。
- 模板输入零元数据泄漏（人工+渲染检查双确认）。
- 冻结后 run_manifest 完整记录参数与数据哈希；离线重放（D11）可据此还原同一配置。
