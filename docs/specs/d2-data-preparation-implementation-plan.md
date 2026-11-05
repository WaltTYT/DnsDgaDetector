# D2 数据准备实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`; track progress with the checkboxes below. All shared data must follow `docs/plans/data-contracts.md`. The D2 design is approved. Do not commit, merge, or push unless separately requested.

**Goal:** 建立可追溯的正常域名、公开 DGA 与手工 DGA 数据，按固定 PSL 和种子完成无注册域泄漏的 60/20/20 划分。

**Architecture:** 原始来源快照只读保存，整理成稳定的本地输入格式；`src.gen_dga` 提供确定性生成函数，`src.prepare_data` 离线读取本地输入、校验来源和域名、调用生成器并产出正常域名列表、手工 DGA CSV 与 `splits.csv`。共享 CSV 严格复用契约 v1 表头。

**Tech Stack:** Windows PowerShell、Python 3.11.17、tldextract 5.4.0、pytest 9.1.1、标准库 `csv`、`hashlib`、`ipaddress`、`json`、`random`。

---

## 实施状态

- 设计依据：[D2 数据准备设计](../plans/d2-data-preparation-design.md)，状态为评审通过。
- 分发门禁：Mendeley 上游材料及派生样本的再分发权尚未独立确认；相关输入与包含这些域名的生成结果仅保留在本机，许可复核完成或替换数据源前不得提交或公开分发。
- D2 设计提出的契约语义澄清必须先同步到 `docs/plans/data-contracts.md`：空 `algorithm` 表示未应用项目手工生成算法；记录类别由 `label` 与 `source` 共同确定。不增列、不改表头。
- `config.json.seed` 固定为整数 `20261101`。不得把任何 API Key 放入配置或数据。
- 本计划不生成 `data/dns_log.csv`；日志行为分布和统计属于 D3。

## 文件映射

| 文件 | 责任 |
|---|---|
| `docs/plans/data-contracts.md` | 澄清 `algorithm` 空值语义，保持五类产物表头与其他字段不变 |
| `config.json` | 将 `seed` 从 `null` 固定为 `20261101`；现有 PSL 路径保持不变 |
| `data/sources.md` | 记录实际来源、许可、版本、标签语义、哈希及纳入统计 |
| `data/raw/snapshots/` | 保存许可允许再分发的原始快照；不可再分发的原始文件不入库 |
| `data/raw/normal_list__*.txt` | 每个正常榜单的本地整理输入，每行一个域名 |
| `data/raw/hard_negative__*.txt` | 每种可核查来源的难负样本输入，每行一个域名 |
| `data/raw/public_dga__*.csv` | 公开 DGA 的本地整理输入，固定表头 `domain,family` |
| `data/raw/public_suffix_list.dat` | 固定 PSL 快照，含 PRIVATE 区规则 |
| `data/raw/manual_word_roots.txt` | 项目自有固定词根表，每行一个小写 ASCII 词根 |
| `src/gen_dga.py` | 三种可重复的手工 DGA 生成与契约 CSV 输出 |
| `src/prepare_data.py` | 来源读取、规范化、冲突检查、PSL 提取、分组划分和最终输出 |
| `tests/test_gen_dga.py` | DGA 数量、唯一性、参数重建、seed 确定性和碰撞处理 |
| `tests/test_prepare_data.py` | 域名规范化、来源读取、标签冲突、离线 PSL、划分及端到端输出 |
| `data/normal_domains.txt` | 规范化且唯一的正常域名与难负样本列表 |
| `data/dga_domains.csv` | 300 条手工 DGA 记录，遵守契约 v1 |
| `data/splits.csv` | 所有正常、公开 DGA 与手工 DGA 域名的唯一划分 |
| `README.md` | 添加 D2 本地输入格式、运行命令与来源许可说明 |
| `.gitignore` | 仅忽略许可不允许提交的具体来源文件及派生输入，不忽略全部 `data/raw/` |

## API 与确定性契约

下列接口是实现任务与测试共用的约定；若需改变签名或字段，先同步本计划和对应测试。Python 类型使用 3.11 语法。

`src/gen_dga.py` 的接口：

| 名称 | 签名/字段 | 责任 |
|---|---|---|
| `DgaRecord` | `domain: str, algorithm: str, seed: str, params: dict[str, int \| str]` | 单条可重建的手工 DGA 记录 |
| `generate_dga_domains` | `(master_seed: int, word_roots_path: Path, reserved_domains: Collection[str] = (), count_per_algorithm: int = 100, collision_retry_limit: int = 100) -> list[DgaRecord]` | 按固定顺序生成记录，不写文件 |
| `rebuild_dga_domain` | `(algorithm: str, seed: str, params: Mapping[str, int \| str], word_roots_path: Path \| None = None) -> str` | 仅凭记录参数及词根文件重建 FQDN；算法不需要词根时 path 可省略 |
| `write_dga_csv` | `(records: Sequence[DgaRecord], target: Path, word_roots_path: Path \| None = None) -> None` | 写入前依据 algorithm/seed/params 重建并校验 domain；含 `word_digit` 记录时须传入词根文件路径，再按契约写入临时文件并替换目标 |

`src/prepare_data.py` 的数据类型与接口：

| 名称 | 字段/签名 | 责任 |
|---|---|---|
| `SourceRow` | `raw_domain: str, label: int, source: str, algorithm: str, source_id: str, source_path: Path, line_number: int, family: str \| None = None` | 保留原始输入和可追溯位置 |
| `NormalizedSourceRow` | `domain: str, label: int, source: str, algorithm: str, source_id: str, source_path: Path, line_number: int, family: str \| None = None` | 保存规范化域名及原始 provenance |
| `ExcludedRow` | `source_id: str, source_path: Path, line_number: int, raw_domain: str, reason: str` | 记录不合法域名及排除原因 |
| `DomainRecord` | `domain: str, label: int, source: str, algorithm: str, etld_plus_one: str, split: str, sources: tuple[NormalizedSourceRow, ...]` | 共享划分前后的单域名记录；手工 DGA 的 sources 为空 |
| `AlgorithmDiagnostics` | `count: int, unique_count: int, collision_retries: int, example_domains: list[str]` | 单个手工算法的数量、唯一数量、重试数及前三个成功样本 |
| `RunSummary` | `source_counts: dict[str, dict[str, int]], excluded_rows: list[dict[str, str \| int]], public_dga_family_counts: dict[str, dict[str, int]], split_counts: dict[str, int], algorithm_counts: dict[str, int], algorithm_diagnostics: dict[str, AlgorithmDiagnostics], collision_retries: int, psl_sha256: str, input_sha256: dict[str, str]` | 汇总来源计数与逐条排除明细、划分、生成和哈希信息 |
| `normalize_domain` | `(value: str) -> str` | 规范化或抛出 `ValueError` |
| `load_source_rows` | `(raw_dir: Path) -> list[SourceRow]` | 读取规范命名的离线整理输入 |
| `normalize_source_rows` | `(rows: Sequence[SourceRow]) -> tuple[list[NormalizedSourceRow], list[ExcludedRow]]` | 规范化并显式返回排除记录 |
| `select_public_dga` | `(rows: Sequence[NormalizedSourceRow], limit: int, master_seed: int) -> list[NormalizedSourceRow]` | 家族分层去重抽样 |
| `merge_and_validate` | `(rows: Sequence[NormalizedSourceRow]) -> list[DomainRecord]` | 合并同语义域名并拒绝冲突 |
| `manual_domain_records` | `(records: Sequence[DgaRecord]) -> list[DomainRecord]` | 将已去碰撞的手工样本映射到共享字段 |
| `create_extractor` | `(psl_path: Path) -> tldextract.TLDExtract` | 只用固定本地 PSL 初始化解析器 |
| `assign_splits` | `(records: Sequence[DomainRecord], extractor: tldextract.TLDExtract, master_seed: int) -> list[DomainRecord]` | 计算 eTLD+1 并确定性分组划分 |
| `run_data_preparation` | `(project_root: Path, config_path: Path) -> RunSummary` | 运行完整 D2 流水线 |

`RunSummary` 至少包含来源原始/有效/排除/去重计数、逐条排除明细（source ID、相对来源路径、行号、原始域名、原因）、公开 DGA 家族配额与入选数、三类 split 计数与比例、手工算法数量与碰撞重试数，以及 PSL 和输入 SHA-256。`project_root` 是包含 `data/` 的仓库根目录；配置中的相对 PSL 路径相对该目录解析。`load_source_rows` 只读 `data/raw/` 顶层规范命名的整理输入，不递归读取 `snapshots/`。域名格式错误由 `normalize_source_rows` 转为带来源、行号和原因的 `ExcludedRow`；语法有效但固定 PSL 无法提取 eTLD+1 的记录也在流水线中转为显式 `ExcludedRow`，不进入 DGA 抽样或划分。输入结构错误（如公开 DGA CSV 表头错误、缺失 family）属于来源错误并立即失败。

`RunSummary` 字段最少为 `source_counts: dict[str, dict[str, int]]`（含 raw/valid/excluded/deduplicated）、`excluded_rows: list[dict[str, str | int]]`、`public_dga_family_counts: dict[str, dict[str, int]]`（含 available/quota/selected）、`split_counts: dict[str, int]`、`algorithm_counts: dict[str, int]`、`algorithm_diagnostics: dict[str, AlgorithmDiagnostics]`、`collision_retries: int`、`psl_sha256: str`、`input_sha256: dict[str, str]`。每个算法的 `example_domains` 固定为成功样本 index 最小的前三个域名；全局重试数应等于逐算法重试数之和。

公开 DGA 超过 `limit=300` 时，先按规范化域名及 family 去重并拒绝同域名多 family 冲突，再以 family 为层分配配额：`base_f = floor(300 * n_f / N)`，剩余名额按 `(300 * n_f) mod N` 降序、family 字符串升序发放；每层内按 `(SHA-256(f'{master_seed}|public_dga|{family}|{domain}').hexdigest(), domain)` 升序取前 `quota_f` 条。候选总数不超过 300 时全部纳入。该算法不依赖 Python 运行时哈希顺序或输入文件行序。

手工生成的每类样本从 4 位 index `0001` 开始；成功样本后 index 加一，碰撞时跳过该候选并继续。`collision_retry_limit` 默认 100，取值只能为 1–100，表示最多容忍的碰撞候选数；达到上限仍未生成当前样本即失败，不生成部分交付文件。词根表须包含至少两个不同的合法 ASCII 词根，每项匹配 `[a-z][a-z0-9-]{0,19}` 且不能以连字符结尾，文件中不得有空行；哈希基于词根表原始 UTF-8 文件字节。随机参数抽取顺序固定：`rand_alpha` 用单独 `Random(seed)` 先 `randint(12, 18)` 再逐字符 `choice(a-z)`；`word_digit` 依次抽 `root_count=randint(1, 2)`、`sample(roots, root_count)`、`digit_count=randint(2, 4)`、逐位 `choice(0-9)`；`md5_date` 用单独 `Random(seed).randint(12, 16)` 决定摘要截断长度。seed 字符串严格为 `<master_seed>:<algorithm>:<index:04d>`。`params` 键固定为：`rand_alpha` 的 `length,tld`；`word_digit` 的 `wordlist_sha256,root_count,digit_count,tld`；`md5_date` 的 `date,truncation_length,tld`。`tld` 固定为 `.com`，日期固定为 `2026-11-01`。

## Task 1：核验数据来源并固定本地输入

**文件：** `data/sources.md`、`data/raw/snapshots/`、`data/raw/normal_list__*.txt`、`data/raw/hard_negative__*.txt`、`data/raw/public_dga__*.csv`、`data/raw/public_suffix_list.dat`、`data/raw/manual_word_roots.txt`。

- [x] 核验正常榜单、公开 DGA 数据集和官方 PSL 的版本、数据集许可及标签语义。正常域名目标约 3,000 条；优先检查 Majestic Million 与 `360netlab/DGA`，不采用底层数据再分发权未能确认的 360netlab 示例，并改用记录完整的 Mendeley Data v1。
- [ ] 在提交或公开分发前，独立确认 Mendeley 上游材料及派生样本的再分发权；当前数据集页面标注 CC BY 4.0，但上游第三方权利未确认，若不能确认则替换该来源并重新生成产物。
- [x] 同时核实原始快照与其域名派生清单的课程使用/再分发条件；不能提交的文件逐项写入 `.gitignore`，并在 `data/sources.md` 标记为本机准备项，不得因生成了派生 CSV 就假定其可提交。
- [x] 对每个原始来源分配稳定 source ID。许可允许时，将未修改的来源快照保存到 `data/raw/snapshots/`；否则只保留下载 URL、版本/日期、许可、转换步骤和 SHA-256，不提交受限文件。
- [x] 将正常和难负样本整理成来源分开的 UTF-8 文本输入；每行仅一个域名。难负样本目标约 100 个，每个域名都要能从 `data/sources.md` 的 source ID、URL/公开文档和选择理由核查。将公开 DGA 整理成 `domain,family` 两列表格，保留原始家族值。`data/sources.md` 必须把每份整理输入映射到 source ID、来源类别和对应原始快照。
- [x] 整理输入文件名固定为 `normal_list__<source_id>.txt`、`hard_negative__<source_id>.txt`、`public_dga__<source_id>.csv`；loader 只读取 `data/raw/` 顶层匹配文件，原件快照放在 `snapshots/` 子目录，不进入 loader。
- [x] 保存固定 PSL 到 `data/raw/public_suffix_list.dat`，包含 PRIVATE 区并记录日期、许可和 SHA-256。准备固定词根表到 `data/raw/manual_word_roots.txt`，至少两个不同合法词根、无空行，记录其来源/创建方式、版本和原始文件 SHA-256。
- [x] 在 `data/sources.md` 记录下载链接、版本/获取日期、许可、标签依据、文件名、SHA-256、纳入规则以及最终原始/有效/排除/去重数量。许可或标签含义无法核实时停止，不用无来源数据凑数。
- [x] 对每个不允许提交的原始文件或整理输入，在 `.gitignore` 添加具体路径规则；允许再分发的 PSL、项目词根表及其余数据仍可跟踪，禁止用 `data/raw/` 这种宽泛规则隐藏全部输入。
- [x] 检查所有生成脚本只读取本地 `data/raw/`；下载步骤与数据生成步骤分离，生成期间不发 DNS 查询、不联网。

来源文件校验命令：

```powershell
Get-ChildItem data/raw -File -Recurse | Get-FileHash -Algorithm SHA256
```

**预期：** 每个进入数据集的域名都能追溯到已记录来源；原始许可和原始文件哈希有据可查。

## Task 2：同步契约语义和固定 seed

**文件：** `docs/plans/data-contracts.md`、`config.json`。

- [x] 同步修订契约 v1 中 `dns_log.csv`、`splits.csv` 与 `features.csv` 对 `algorithm` 的说明：三种枚举仅表示项目手工生成算法，其他来源留空；类别由 `label` 与 `source` 联合判断。保持五类共享产物的表头、列顺序和字段数不变。
- [x] 将 `config.json.seed` 设为整数 `20261101`，其余 D1 模型和窗口配置不变。
- [x] 校验 JSON 可以解析，并用差异检查确认没有改动五类契约表头或 D1 模型凭证边界。

验证：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -c "import json; c=json.load(open('config.json', encoding='utf-8')); assert c['seed'] == 20261101; print('config OK')"
if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed' }
```

**预期：** 打印 `config OK`；契约仍为 v1 且无结构性表头变更。

## Task 3：先写手工 DGA 生成测试

**文件：** `tests/test_gen_dga.py`。

- [x] 为 `generate_dga_domains(master_seed, word_roots_path, reserved_domains=(), count_per_algorithm=100)` 编写测试，要求算法 ID 顺序为 `rand_alpha`、`word_digit`、`md5_date`，每种默认生成 100 条，完整域名总计 300 条且全局唯一。
- [x] 测试文件使用 `from src import gen_dga`，在测试体中调用 `gen_dga.generate_dga_domains(...)`；不要从模块直接导入尚不存在的名字，以便 pytest collection 成功并得到运行期红灯。
- [x] 固定同一主 seed 和词根文件，运行生成两次并断言记录列表完全相同；更改主 seed 后，至少一个生成域名变化。
- [x] 对每条记录断言字段为 `domain,algorithm,seed,params`；`seed` 非空；`params` 可解析为 JSON 对象；按算法与记录内 seed/params 重建时得到相同域名。
- [x] 为 `.com` 后缀、标签长度/字符集和三种生成规则分别增加断言：随机字母长度为 12–18；词根为 1–2 个且数字为 2–4 位；MD5 截断长度为 12–16。测试按 `YYYYMMDD|sample_seed` UTF-8 字节输入复核摘要。
- [x] 单测明确 `rebuild_dga_domain(algorithm, seed, params, word_roots_path)`：三种算法逐条重建；词根表哈希不匹配、未知算法或参数与 seed 推导值不一致时抛出 `ValueError`。
- [x] 用已占用域名测试确定性碰撞重试：先生成各算法 index `0001` 的候选并将其加入 reserved set，再验证正式结果跳到 `index 0002` 且 seed/params 对应成功候选；另用两个连续碰撞验证上限及越界行为。
- [x] 测试 `write_dga_csv` 的固定列顺序、UTF-8 无 BOM、JSON 稳定序列化、重复写入字节一致和写入失败时保留既有目标。
- [x] 提供包含固定词根的 `tmp_path` fixture；测试不得依赖真实网络、外部 DNS 或本机已有数据文件。

运行并确认测试因生成函数尚不存在而失败：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_gen_dga.py -q
if ($LASTEXITCODE -ne 1) { throw "Expected runtime test failures with exit code 1, got $LASTEXITCODE" }
```

**预期：** FAIL 指向 `src.gen_dga.generate_dga_domains` 尚未实现，而不是 fixture、导入或测试语法错误。

## Task 4：实现确定性 DGA 生成器

**文件：** `src/gen_dga.py`、`tests/test_gen_dga.py`。

- [x] 实现 API 契约中的 `generate_dga_domains`，以固定算法顺序生成 records；每个样本 seed 使用 `<master_seed>:<algorithm>:<index:04d>`，index 从 `0001` 起。
- [x] `rand_alpha` 对每个 sample seed 新建 `random.Random(seed)`，先 `randint(12, 18)` 取得 length，再逐字符 `choice('abcdefghijklmnopqrstuvwxyz')`；`params` 精确写入 `length,tld`。
- [x] `word_digit` 按文件行序读取词根，去除首尾空白但不排序；要求至少两个唯一、合法词根。每个 sample seed 新建 `random.Random(seed)`，按 `root_count=randint(1, 2)`、`sample(roots, root_count)`、`digit_count=randint(2, 4)`、逐位 `choice('0123456789')` 的顺序构造标签。`params` 精确写入 `wordlist_sha256,root_count,digit_count,tld`；`rebuild_dga_domain` 复用同一抽取序列并验证 params 与推导值相同。
- [x] `md5_date` 用 `random.Random(seed).randint(12, 16)` 取得 `truncation_length`，再对 `20261101|<sample_seed>` UTF-8 字节计算 MD5，取摘要十六进制前 `truncation_length` 位；`params` 精确写入 `date,truncation_length,tld`。
- [x] 发现生成域名已存在于 `reserved_domains` 或本次任一算法已生成集合时，按同算法的下一个 4 位 index 确定性重试；`collision_retry_limit` 必须在 1–100，超出范围、index 超过 `9999` 或达到碰撞上限即抛出错误。`generate_dga_domains` 只返回完整记录列表，调用方不得在成功返回前写输出文件。
- [x] 使用 `open(..., encoding='utf-8', newline='')` 与 `csv.DictWriter(..., lineterminator='\n')` 写 UTF-8 无 BOM、固定表头 `domain,algorithm,seed,params`；`params` 用 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(',', ':'))` 序列化；按 `rand_alpha`、`word_digit`、`md5_date` 固定算法顺序、每种按成功 index 升序写出。
- [x] `write_dga_csv` 先写同目录临时文件，再用 `Path.replace` 替换目标；异常时删除临时文件，原有目标文件不被部分覆盖。

运行 DGA 测试：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_gen_dga.py -q
if ($LASTEXITCODE -ne 0) { throw 'DGA tests failed' }
```

**预期：** 所有 DGA 测试通过；重复生成结果逐字节一致；生成器本身不读写 API 凭证、不联网。

## Task 5：先写域名规范化和来源读取测试

**文件：** `tests/test_prepare_data.py`。

- [x] 使用 `from src import prepare_data` 导入模块，在测试体内访问待实现函数，确保 pytest collection 成功并通过运行期断言获得红灯。
- [x] 测试 `normalize_domain(value)`：去首尾空白、去一个末尾根点、小写化、IDNA 转 ASCII；覆盖 `BÜCHER.example.` 转成 `xn--bcher-kva.example`。
- [x] 参数化测试拒绝空值、IP 字面量、通配符、空标签、首尾连字符、含非法字符的标签、超过 63 字符的标签及超过 253 字符的域名。
- [x] 测试来源 loader 只读取预定格式：`normal_list__*.txt`、`hard_negative__*.txt` 和带固定 `domain,family` 表头的 `public_dga__*.csv`；文件名、CSV 表头、缺失列或空 family 错误应给出文件/行号并失败，不能静默跳过。单个非法域名不使批次中止，而是生成带原因的 exclusion 记录。
- [x] 测试公开 DGA family 分层抽样：构造 family-a/b/c/d 分别 101/90/79/60 条，共 330 条；limit=300 时配额必须分别为 92/82/72/54。同一 seed/输入重排后选择结果相同；不同 seed 的层内排序由 SHA-256 公式决定；小于 300 条时所有有效去重样本均入选。
- [x] 测试传入临时 PSL 文件时，`example.co.uk` 的 eTLD+1 为 `example.co.uk`，`a.account.blogspot.com` 在 PRIVATE suffix 启用时的 eTLD+1 为 `account.blogspot.com`。
- [x] 通过 monkeypatch 阻断 `socket.socket.connect` 与 `connect_ex`，构造 extractor 并执行 PSL 查询；必须显式使用本地 PSL 的 `file://` URI、`cache_dir=None`、`fallback_to_snapshot=False`、`include_psl_private_domains=True`，测试在没有外网的条件下通过。

先运行并确认失败发生在待实现函数：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_prepare_data.py -q
if ($LASTEXITCODE -ne 1) { throw "Expected runtime test failures with exit code 1, got $LASTEXITCODE" }
```

**预期：** FAIL 指向规范化、来源读取或本地 PSL 函数缺失；测试 collection 本身成功。

## Task 6：实现离线规范化与来源读取

**文件：** `src/prepare_data.py`、`tests/test_prepare_data.py`。

- [x] 实现 `normalize_domain(value)`。去首尾空白、去末尾根点、用 Python 3.11 标准库 IDNA codec 转小写 ASCII；验证标签非空、长度 1–63、标签只含字母/数字/连字符且首尾不是连字符、整体长度不超过 253；拒绝 IP、通配符和空值并抛出 `ValueError`。
- [x] 实现 `create_extractor(psl_path)`，要求 PSL 文件存在且非空；用 `tldextract.TLDExtract(suffix_list_urls=(psl_path.resolve().as_uri(),), cache_dir=None, fallback_to_snapshot=False, include_psl_private_domains=True)` 创建解析器。不得使用默认在线 URL 或内置快照兜底。
- [x] 实现 `load_source_rows(raw_dir)`，要求至少各有一个 normal、hard-negative、public-DGA 整理输入文件；按来源文件名前缀推导契约 `source`/`label`/`algorithm`，按相对路径排序读取。正常/难负域名文件逐行读取；公开 DGA CSV 必须精确使用 `domain,family` 两列。保留原始域名、source ID、文件路径和原始行号；`family` 校验时去首尾空白，原始快照仍保留原始字段，公开 family 只用于冲突与审计，不写入共享 `algorithm` 字段。
- [x] 实现 `normalize_source_rows(rows)`：成功记录产生 `NormalizedSourceRow`；`normalize_domain` 的 `ValueError` 转为 `ExcludedRow(source_id, source_path, line_number, raw_domain, reason)` 并纳入统计。CSV 结构错误、缺失/空 family、文件无法读取属于输入错误，附文件与行号上下文抛错。
- [x] 正常榜单来源映射为 `label=0, source=normal_list, algorithm=''`，难负样本映射为 `label=0, source=hard_negative, algorithm=''`；公开 DGA 映射为 `label=1, source=public_dga, algorithm=''`。手工 DGA 记录稍后映射为 `label=1, source=manual_dga` 及其契约算法 ID。
- [x] 实现 `select_public_dga(rows, limit, master_seed)`，流水线调用时固定 `limit=300`：同一规范化域名同 family 去重；同域名多 family 在抽样前报冲突；候选不超过 300 条时全部纳入，超出时按本计划“API 与确定性契约”中的最大余数配额及 SHA-256 层内排序选样，并返回稳定排序结果。
- [x] 针对无来源文件、错误 CSV 表头、无效 PSL 和非法域名使用清晰异常；不联网、不静默回退、不静默修正来源标签。

运行聚焦测试：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_prepare_data.py -q
if ($LASTEXITCODE -ne 0) { throw 'Preparation normalization/source tests failed' }
```

**预期：** 规范化、来源格式和 PSL 私有后缀测试通过；临时 fixture 删除后测试仍可重复运行。

## Task 7：先写去重、冲突和分组划分测试

**文件：** `tests/test_prepare_data.py`。

- [x] 使用小型内存 fixture 测试相同规范化 FQDN、相同标签和相同算法时去重；统计中保留重复来源计数。
- [x] 测试同一 FQDN 跨正常/DGA 标签时抛出冲突错误；同一公开 DGA FQDN 的 family 不同也抛错；错误包含 FQDN、source ID 和文件行号。
- [x] 测试确定性划分：相同输入即使行顺序不同也输出相同 split；主 seed 改变时允许 split 变化。
- [x] 测试 eTLD+1 不泄漏：属于同一可注册域的多个子域必须同 split；`example.com` 与 `example.co.uk` 是不同 group。
- [x] 使用固定 PSL 和足够多的独立注册域 fixture，断言三种手工算法分别覆盖 train/val/test；另用包含一个大 group 的 fixture 验证算法不拆 group，并按设计报告实际比例，不把 60/20/20 设为不可能满足的硬阈值。
- [x] 用等价候选 split 的小 fixture 验证并列时固定选择顺序为 train、val、test；按 D2 公式核算每个候选的归一化平方损失，避免实现自行更换目标函数。
- [x] 固定测试 PSL 与域名列表在 `tmp_path`，不得使用仓库内真实数据或公网请求。

先运行新增测试，确认失败原因是划分/冲突逻辑未实现：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_prepare_data.py -q
if ($LASTEXITCODE -ne 1) { throw "Expected runtime test failures with exit code 1, got $LASTEXITCODE" }
```

## Task 8：实现去重、冲突校验和固定分组划分

**文件：** `src/prepare_data.py`、`tests/test_prepare_data.py`。

- [x] 实现 `merge_and_validate(rows)`：按规范化 FQDN 合并正常/难负与抽样公开 DGA 的完全重复记录；若同 FQDN 的 label/source/algorithm 语义冲突，或公开 family 不一致，立即失败。错误包含域名、source ID、文件和行号；不按来源优先级静默覆盖。
- [x] 相同语义重复项折叠为一个 `DomainRecord`，完整 provenance 保留在 `sources`，并计入 `RunSummary.deduplicated`；共享字段仅写契约枚举，不把 source ID 或公开 family 填入 `source`/`algorithm`。
- [x] 用 `manual_domain_records(records)` 将已通过 reserved-domain 碰撞校验的 `DgaRecord` 映射为 `label=1, source=manual_dga`、保留算法 ID 的 `DomainRecord`；手工域名只经 `generate_dga_domains` 生成，不从来源文件伪装成公开样本。
- [x] 实现 `assign_splits(records, extractor, master_seed)`：用离线 extractor 为每条记录计算 eTLD+1；缺少可注册域名立即失败；以 eTLD+1 为不可拆分 group；目标比例固定 `{train: 0.6, val: 0.2, test: 0.2}`；分层 key 为 `(label, source, algorithm)`。
- [x] group 按域名数降序排序，同大小按 `sha256(f'{master_seed}|{etld_plus_one}'.encode('utf-8')).hexdigest()` 升序。每次模拟把当前 group 加入 train/val/test，按 `L = Σ_s [((n_s-r_s*N)/max(1,r_s*N))² + Σ_c ((n_{s,c}-r_s*N_c)/max(1,r_s*N_c))²]` 计算候选结果，取最低损失；完全并列按 train、val、test 顺序。
- [x] 分配后校验每个 eTLD+1 恰好属于一个 split，三种手工算法各自覆盖三个 split；无法满足时抛错并列出缺失算法/split 和实际比例。60/20/20 作为损失函数优化目标，不设统一硬比例容差；group 泄漏和算法覆盖优先。

运行划分测试：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_prepare_data.py -q
if ($LASTEXITCODE -ne 0) { throw 'Preparation merge/split tests failed' }
```

**预期：** 固定 PSL/seed 下结果确定、无注册域泄漏，标签冲突会阻止输出。

## Task 9：先写端到端流水线测试

**文件：** `tests/test_prepare_data.py`。

- [x] 在 `tmp_path` 下构造最小仓库：`data/raw/` 中放来源分开的正常/难负文本、`domain,family` 公开 DGA CSV、含 `com` / `co.uk` / PRIVATE `blogspot.com` 的本地 PSL、至少两个合法词根；同时覆盖 PSL 公共后缀顶级项和未收录后缀的显式排除；配置文件将 `seed` 设为 `20261101`，PSL 路径使用相对项目根目录的 `data/raw/public_suffix_list.dat`。
- [x] 在生成器和划分单测已通过后，为 `run_data_preparation(project_root, config_path)` 编写集成测试：脚本从本地输入生成三个 D2 产物，`splits.csv` 包含三种来源，手工 DGA 每种算法 100 条；同一临时仓库连续运行两次，三个文件逐字节一致。
- [x] 用 `csv` 标准库断言 `dga_domains.csv` 和 `splits.csv` 的列名、顺序、数量、唯一域名及字段语义；`normal_domains.txt` 每行唯一且按字典序排列；所有 eTLD+1 只属于一个 split。
- [x] 在测试中 monkeypatch `socket.socket.connect` 与 `connect_ex` 为立即失败，端到端运行仍须通过，证明流水线只读本地来源/PSL、不访问外部网络或 DNS。
- [x] 预先在三个输出位置写入 sentinel 内容，再构造标签冲突输入调用流水线；断言命令失败且三个 sentinel 文件逐字节保持不变。另创建 `data/dns_log.csv` sentinel，成功和失败两条路径都须保持其内容不变。

首次运行端到端测试：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/test_prepare_data.py -q
if ($LASTEXITCODE -ne 1) { throw "Expected runtime test failures with exit code 1, got $LASTEXITCODE" }
```

**预期：** 测试收集成功，失败点是 `run_data_preparation` 尚未实现，不是 fixture、导入或网络拦截设置错误。

## Task 10：实现 D2 流水线、CLI 与最终产物

**文件：** `src/prepare_data.py`、`src/gen_dga.py`、`tests/test_prepare_data.py`、`data/normal_domains.txt`、`data/dga_domains.csv`、`data/splits.csv`。

- [x] 实现 `run_data_preparation(project_root, config_path)`：校验配置 seed 为整数 `20261101`，读取来源与本地 PSL，规范化并统计排除项；对全部候选检查格式、重复与 family 冲突；固定 PSL 无法解析 eTLD+1 的行按 source ID/路径/行号/原始域名/原因显式排除，再对剩余候选按配额选取最多 300 个公开 DGA。直接调用 `assign_splits` 时无 eTLD+1 仍报错。
- [x] 将全部有效正常/难负域名及入选公开 DGA 传入 `reserved_domains`，生成每算法 100 条手工 DGA；调用 `manual_domain_records(records)` 映射为 `label=1, source=manual_dga` 与对应算法，再与已合并的公开来源记录拼接、按固定 PSL 分组划分。
- [x] 在来源、标签、域名、算法覆盖、group 和输出表头全部通过校验前不修改目标文件。三个目标先各自写入同目录临时文件，全部写成功后再逐个 `Path.replace`；清理失败留下的临时文件。不得创建、读取或改写 `data/dns_log.csv`。
- [x] 输出 `data/normal_domains.txt` 为 UTF-8、LF 换行、每行一个规范化唯一正常/难负域名、按 ASCII 字典序排列；输出 `data/dga_domains.csv` 表头严格为 `domain,algorithm,seed,params`，按固定算法顺序及 sample index 排列；输出 `data/splits.csv` 表头严格为 `domain,label,source,algorithm,split`，按 domain ASCII 字典序排列。CSV 均为 UTF-8 无 BOM，使用 `newline=''` 与 `lineterminator='\n'` 保证跨平台字节稳定。
- [x] 通过 `python -m src.prepare_data` 运行 CLI：从当前仓库根目录读取 `config.json`、`data/raw/` 并返回 `main() -> int` 退出码；输入/冲突失败时非零退出，错误输出保留来源文件、行号和错误类别，不覆盖现有产物。
- [x] CLI 输出并由 `RunSummary` 提供每来源 raw/valid/excluded/deduplicated 数、逐条排除明细、公开 DGA 每 family available/quota/selected、标签与算法计数、各 split 数量/比例、每算法手工数量/唯一数/碰撞重试数/前三个示例域名、全局碰撞重试数、PSL SHA-256 与输入文件 SHA-256；公开 DGA family 不得写入契约 CSV 字段。
- [x] 运行本地端到端测试和真实来源数据准备流程，确认来源规模/许可、词根表/PSL hash 与 `data/sources.md` 对应；把实际统计人工填入 `data/sources.md`，脚本本身不联网下载且不自动改写来源说明。

验证命令：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m src.prepare_data
if ($LASTEXITCODE -ne 0) { throw 'D2 data preparation failed' }
Get-FileHash data/normal_domains.txt, data/dga_domains.csv, data/splits.csv -Algorithm SHA256
```

**预期：** 数据规模符合设计目标或在来源不足时如实记录；重复运行三个文件哈希一致；`data/dns_log.csv` 未生成或改写。

## Task 11：文档同步与 D2 完整验收

**文件：** `README.md`、`data/sources.md`、`docs/plans/data-contracts.md`、`docs/plans/d2-data-preparation-design.md`、本计划。

- [x] README 记录本地来源输入格式、`python -m src.prepare_data` 命令、数据许可注意事项和 DGA 标签不代表现实恶意的限制；不写任何本机绝对路径或 API Key。
- [x] 自审设计与实现：契约算法 ID 一致；公开 DGA 保留原家族在来源原件但不进入共享字段；正常榜单 `source=normal_list`、难负样本 `source=hard_negative`；仅三种手工算法进入 `algorithm`。
- [x] 检查输入文件许可和 Git 变更，确保未提交禁止再分发的数据集、下载缓存或运行环境目录；只保留许可证允许的快照和要求的派生数据。
- [x] 运行全部 pytest、`pip check`、配置 JSON 校验和 `git diff --check`；检查最终三个共享数据 CSV 表头、列顺序、唯一性和 split group 不重叠。
- [x] 报告最终正常样本数、难负样本数、公开 DGA 数、手工 DGA 数、排除/冲突数、各 split 比例、来源/PSL 哈希，以及未满足的目标和原因。不得将模拟域名数据描述成真实 DNS 观测。

最终验证命令：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3' # 按本机安装位置调整
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
& $python -m pytest tests/ -v
if ($LASTEXITCODE -ne 0) { throw 'Full test suite failed' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'pip dependency check failed' }
& $python -c "import json; c=json.load(open('config.json', encoding='utf-8')); assert c['seed'] == 20261101; print('config OK')"
if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'git diff whitespace check failed' }
```

**D2 完成边界：** 数据来源可追溯、生成器可复现、域名划分无 eTLD+1 泄漏、三个 D2 共享输出符合契约且测试通过。`data/dns_log.csv`、行为频率模拟、日志统计和 D3 检查点仍不在本计划范围。
