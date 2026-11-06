# DNS DGA Detector

DNS 异常查询检测的规则 + LLM 混合方案，实施范围见 [项目路线图](docs/specs/project-roadmap.md)。

**当前状态（2026-11-06）**：D1–D12 已完成，D13 报告未在本仓库生成。最终流水线使用冻结配置跑通：50,000 行合成 DNS 日志、20,483 个聚合样本、96 个唯一域名完成在线复核、321 项测试通过，离线重放指标与在线结果一致。

## 项目文档

- [项目路线图与实施计划](docs/specs/project-roadmap.md)
- [数据契约 v2](docs/plans/data-contracts.md)
- [D1 设计文档](docs/plans/d1-foundation-design.md)
- [D1 实现计划](docs/specs/d1-implementation-plan.md)
- [D2 数据准备设计](docs/plans/d2-data-preparation-design.md)
- [D2 数据准备实现计划](docs/specs/d2-data-preparation-implementation-plan.md)
- [D3 DNS 查询日志设计](docs/plans/d3-dns-query-log-design.md)
- [D3 DNS 查询日志实现计划](docs/specs/d3-dns-query-log-implementation-plan.md)
- [D4–D6 设计（教材复现、特征、规则）](docs/plans/d4-d6-design.md)
- [D4–D6 实施计划](docs/specs/d4-d6-implementation-plan.md)
- [D7 提示模板与阈值冻结设计](docs/plans/d7-prompt-and-freeze-design.md)
- [D7 实现计划](docs/specs/d7-prompt-and-freeze-implementation-plan.md)
- [D8 LLM 语义改判设计](docs/plans/d8-llm-judge-design.md)
- [D8 实现计划](docs/specs/d8-llm-judge-implementation-plan.md)
- [D9 混合方案评估设计](docs/plans/d9-hybrid-evaluation-design.md)
- [D9 实现计划](docs/specs/d9-hybrid-evaluation-implementation-plan.md)
- [审查问题修复实现计划](docs/specs/review-findings-fix-implementation-plan.md)
- [D1–D9 交接文档](docs/plans/d1-d9-handover.md)

## 参考资料

- [课本原文：第 6 章 应用层（DNS 与文件传送协议）](docs/课本原文-第6章-应用层-DNS与FTP.md)

## 开发环境

- 当前执行机器的 Miniconda：`%LOCALAPPDATA%\Miniconda3`
- Conda 环境：`%LOCALAPPDATA%\Miniconda3\envs\dns-dga`（命名环境 `dns-dga`）
- Python：3.11.17
- 核心依赖：dnspython、requests、tldextract、NumPy、pandas、Matplotlib、pytest

本机环境使用仓库中的 `environment.yml` 和 `requirements.txt` 创建；七项直接依赖已按锁定版本安装，`python -m pip check` 通过。其他组员可以将 Miniconda 安装在自己的用户目录，按各自安装路径调整解释器命令。

若本机尚未创建 `dns-dga`，在仓库根目录的 PowerShell 中执行以下命令。首次使用 defaults channel 时，Conda 可能要求用户查看并接受相应服务条款；不要在未阅读和同意时绕过提示。

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3'
$conda = Join-Path $installRoot 'Scripts\conda.exe'
$env:CONDA_PKGS_DIRS = Join-Path $installRoot '.cache\pkgs'
$env:PIP_CACHE_DIR = Join-Path $installRoot '.cache\pip'
New-Item -ItemType Directory -Force $env:CONDA_PKGS_DIRS, $env:PIP_CACHE_DIR | Out-Null
& $conda env create --file environment.yml
```

创建后用项目解释器验证：

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'
& $python --version
& $python -m pip check
```

直接依赖版本以 `requirements.txt` 为准。日常也可直接调用该环境的 `python.exe`，无需修改 PATH 或激活环境。

## D2 数据准备

来源整理输入位于 `data/raw/` 顶层，按来源分别保存；`data/raw/snapshots/` 中的原始快照不由生成器读取。生成流程只使用本地文件和固定 PSL，不下载数据、不发 DNS 查询。运行前应查看 [`data/sources.md`](data/sources.md) 的许可、标签口径和筛选说明。

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'
& $python -m src.prepare_data
```

命令生成 `data/normal_domains.txt`、`data/dga_domains.csv` 和 `data/splits.csv`，并在终端输出 JSON 摘要，包括各来源计数、固定 PSL 下的排除明细、公开 DGA 家族计数、划分比例及输入哈希。手工 DGA 按算法提供数量、唯一数量、碰撞重试数和前三个样本示例。无 eTLD+1 的记录不会进入划分；摘要保留其来源、行号和排除原因。`data/dns_log.csv` 由后续 D3 阶段负责，不会被此命令生成或改写。

Majestic 与 Mendeley 的数据许可和上游来源限制见 [`data/sources.md`](data/sources.md)。尤其是 Mendeley 数据集包含第三方来源，CC BY 4.0 标注不自动授予其上游材料的独立再分发权。许可确认前，Mendeley 派生输入及包含其样本的生成产物仅供本机使用，不得提交或公开分发。公开数据集的 DGA 标签和项目手工生成标签均不构成对域名当前恶意性的独立验证。

## LLM 接口

官方 HTTP API 端点配置在 `config.json`。真实调用前，在本地为当前用户配置 `ZHIPU_API_KEY`，或在仓库根目录创建仅本机使用的 `api.txt`。不要将密钥写入配置、源码、截图或提交记录。环境创建和单元测试不需要 API Key；依赖检查不代表真实 API 连通性已经验证。

`src/llm_judge.py` 的 `get_api_key()` 优先读取环境变量，未设置时回退读取根目录 `api.txt`；该文件已被 `.gitignore` 忽略。配置凭证后，从仓库根目录执行一次真实 JSON 冒烟调用：

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'
& $python -m src.llm_judge --smoke
```

冒烟命令最多发送一次请求，不自动重试；只保存脱敏元数据到 `report/evidence/d1_glm_smoke.json`，不保存 Key 或模型原始回复。需成功完成此命令，才能认定当前账号、模型和端点已连通。

本机 2026-11-06 冒烟测试：HTTP 200、JSON 校验通过、耗时 884 ms、84 tokens；随后 96 个唯一域名在线复核全部 HTTP 200，失败 0 条。当前演示使用缓存离线重放，不重复发起在线请求。该结果仅证明本次账号与接口连通，不代表后续调用始终可用。

## 一键流水线

`run_all.py` 按顺序执行日志生成、特征计算、基线读取或拟合、规则判定、LLM 复核与双口径评估，并把各阶段摘要写入 `results/run_manifest.json`。`--from` 指定起始阶段（`data`、`log`、`features`），`--seed` 必须为 `20261101`，`--online/--offline` 选择调用模式（缺省为离线）。

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'

# 从特征阶段开始，离线复算并复核缓存
& $python run_all.py --from features --offline

# 从日志或数据阶段重跑（冻结后需与 run_manifest.json 的输入哈希一致）
& $python run_all.py --from log --offline
& $python run_all.py --from data --offline

# 配置 ZHIPU_API_KEY 后在线复核
& $python run_all.py --from features --online
```

冻结点（`results/run_manifest.json` 中 `freeze_check=frozen`）之后，流水线读取已冻结的 `results/baseline.json` 并校验阈值，不再重新拟合；从 `data`/`log` 重跑时会校验 `splits.csv` 与 `dns_log.csv` 的 SHA-256，不一致则退出并提示重开冻结检查点。`--offline` 不发起任何网络请求，缓存缺失样本按 `uncertain` 降级处理。

## 最终结果

冻结阈值：`H=3.5369`、`F=12.0`、`I=1.7587`，仅用训练集 `label=0` 样本拟合。评估口径与原始产物见 [`results/metrics.json`](results/metrics.json) 与 [`results/run_manifest.json`](results/run_manifest.json)。

| 口径 | 方案 | FPR（val / test） | Precision（val / test） | Recall（val / test） |
|---|---|---:|---:|---:|
| 聚合样本 | 规则 | 0.98% / 0.53% | 49.35% / 54.35% | 27.74% / 18.25% |
| 聚合样本 | 混合 | 0.91% / 0.40% | 51.35% / 60.98% | 27.74% / 18.25% |
| 唯一域名 | 规则 | 1.68% / 1.18% | 68.75% / 72.00% | 28.21% / 23.38% |
| 唯一域名 | 混合 | 1.52% / 1.01% | 70.97% / 75.00% | 28.21% / 23.38% |

混合方案在测试集正确撤回 5 个误报、错误放行 0 个告警；验证集正确撤回 3 个、错误放行 0 个。离线重放 96/96 命中缓存，测试与验证缓存覆盖均为 100%。

主要局限：

- 整体召回偏低；`md5_date` 在测试集召回 0%、验证集 5.9%，短 MD5 截断串熵值低于 `H` 阈值，H 分支基本无法命中。
- 难负样本在测试集和验证集的误报均为 0，说明它们主要覆盖词法难例；其合成查询行为仍是正常客户端，未真正压到频率/间隔分支。
- LLM 唯一域名不确定率 47.9%，测试集为 33.3%；所有偏差表现为 `uncertain`，未出现明确的 DGA/normal 反向判断。
- 日志行为为合成数据，公开 DGA 标签只表示已知生成类别，不构成对域名当前恶意性的独立验证。

## 复现与演示

最小离线复现命令为 `run_all.py --from features --offline`，约 14 秒完成特征复算、冻结基线校验、规则判定、缓存复核与双口径评估。完整从数据或日志重跑会重新生成 `data/` 与 `results/` 产物；演示前建议先备份 `results/`，避免覆盖冻结结果。
