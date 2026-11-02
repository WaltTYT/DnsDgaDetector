# D3 DNS 查询日志生成实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. All generated logs must follow the approved D3 design and data contract v1.

**Goal:** 实现离线、可复现的合成 DNS 查询日志生成器，按固定配额生成 50,000 条事件，并提供可独立复算的运行摘要。

**Architecture:** `src.gen_log` 负责配置与 `splits.csv` 校验、确定性配额和行为组生成、契约 CSV 写出及 JSON 摘要。生成器只读本地 D2 划分和 PSL，不发起 DNS 请求、不联网；所有输入与配额校验、事件构造及摘要计算成功后，才原子替换 `data/dns_log.csv`。

**Tech Stack:** Windows PowerShell、Python 3.11.17、标准库 `csv`、`json`、`hashlib`、`random`、`ipaddress`、`datetime`、`tempfile`，现有 `tldextract==5.4.0` 与 `pytest==9.1.1`；不增加依赖，不调用 dnspython 发起查询。

---

## 依据与边界

- 设计依据：[D3 DNS 查询日志生成设计](../plans/d3-dns-query-log-design.md)，用户已评审通过。
- 项目验收依据：[项目路线图](project-roadmap.md) 2.1、2.2、D3 检查点及 7.2；输出字段遵循 [数据契约 v1](../plans/data-contracts.md)。
- 只生成 `data/dns_log.csv`；不改 `splits.csv`、`dga_domains.csv`、D2 输入、五类产物表头、特征、阈值、提示模板或 LLM 模块。
- 事件是离线合成数据；不执行 DNS 查询，不读取真实网络流量，不联网获取 PSL 或其他数据。
- 骨架 docstring 中提到的“数据统计表”按已通过设计落实为 CLI stdout 的 JSON 运行摘要；不新增持久化统计文件，也不改动受保护的模块规格 docstring。
- Mendeley 上游材料和派生域名再分发权尚未独立确认。自动化测试只使用 `tmp_path` 自建数据；基于当前仓库域名生成的 `data/dns_log.csv` 只能本机验证，不得提交或公开分发。
- 不在本计划中提交、合并或推送。执行阶段若要提交，需另行取得用户明确许可，并先确认相关数据许可。

## 文件映射

| 文件 | 责任 |
|---|---|
| `config.json` | 在现有 `log` 对象中加入 D3 默认参数；保留当前 seed、模型、阈值、预算和 PSL 路径，不含密钥 |
| `src/gen_log.py` | 从 docstring 骨架实现离线校验、配额、行为组、时间戳、序列化、摘要及命令行入口 |
| `tests/test_gen_log.py` | 使用本地临时 fixture 覆盖纯函数、离线、契约、确定性、错误和 CLI 行为 |
| `.gitignore` | 增加精确规则 `data/dns_log.csv`，防止本机许可受限的派生输出意外进入提交 |
| `data/dns_log.csv` | 运行时目标文件；只在成功生成后原子替换，不作为测试 fixture 或提交产物 |

执行 `.gitignore` 任务前，先查看现有差异并保留已经存在的 D2 来源忽略规则；只追加上述单一路径，不忽略整个 `data/` 或 `data/raw/`。`docs/plans/data-contracts.md`、`docs/plans/d3-dns-query-log-design.md`、`src/prepare_data.py` 和 D2 数据文件均不在本计划修改范围内。

## 接口约定

`src/gen_log.py` 至少提供以下接口，测试从模块导入而不直接导入尚不存在的符号：

| 名称 | 签名/字段 | 责任 |
|---|---|---|
| `GeneratorConfig` | 根级 seed/window、log 配额、模拟起点、split 时长、客户端池/比例、normal/DGA 行为权重、混合窗口比例、qtype 权重 | 通过配置校验后的不可变生成参数 |
| `DomainRecord` | `domain: str, label: int, source: str, algorithm: str, split: str` | 一条已通过数据契约语义检查的 D2 域名记录 |
| `QueryEvent` | `timestamp: datetime, client_ip: str, qname: str, qtype: str, label: int, source: str, algorithm: str, split: str, behavior: str, stable_order: int` | 单条内存事件；`behavior` 和 `stable_order` 不输出到 CSV |
| `RunSummary` | `seed, input_sha256, output_sha256, target_rows, actual_rows, split_counts, label_counts, source_counts, qtype_counts, behavior_counts, counts_by_split, mixed_window_counts, unique_qname_count, aggregate_sample_count, split_time_ranges, synthetic` | stdout JSON 摘要字段；总体和每 split 的分类计数可复算 |
| `load_config` | `(config_path: Path) -> GeneratorConfig` | 读取并严格校验配置 |
| `largest_remainder` | `(total: int, weights: Mapping[str, float], tie_order: Sequence[str]) -> dict[str, int]` | 校验权重及固定顺序并分配整数配额 |
| `allocate_event_quotas` | `(split_domain_counts: Mapping[str, int], total_rows: int, dga_ratio: float) -> dict[str, dict[str, int]]` | 返回每 split 的 total/normal/DGA 精确配额 |
| `read_split_records` | `(splits_path: Path) -> list[DomainRecord]` | 严格读取契约 v1 输入 |
| `validate_domain_pools` | `(records: Sequence[DomainRecord], psl_path: Path) -> None` | 校验标签池、语义及 eTLD+1 隔离 |
| `build_events` | `(records: Sequence[DomainRecord], config: GeneratorConfig) -> list[QueryEvent]` | 根据配额和行为规则构造完整内存事件 |
| `serialize_log` | `(events: Sequence[QueryEvent]) -> bytes` | 按契约列序序列化为 UTF-8 无 BOM、LF 的 CSV 字节 |
| `write_log` | `(csv_bytes: bytes, target: Path) -> str` | 将已校验的 CSV 字节写入同目录临时文件，校验临时文件哈希后原子替换目标并返回 SHA-256 |
| `build_summary` | `(events: Sequence[QueryEvent], input_sha256: Mapping[str, str], output_sha256: str, config: GeneratorConfig) -> RunSummary` | 从已构造事件和文件哈希汇总运行统计 |
| `validate_summary` | `(summary: RunSummary, events: Sequence[QueryEvent], csv_bytes: bytes) -> None` | 校验摘要与内存事件及其 CSV 表示一致 |
| `run_log_generation` | `(project_root: Path, config_path: Path \| None = None) -> RunSummary` | 对指定项目根目录运行完整流水线 |
| `main` | `() -> int` | 从当前工作目录读取配置并输出 JSON 摘要或错误 |

`allocate_event_quotas` 默认返回 `{"train": {"total": 30003, "normal": 28503, "dga": 1500}, "val": {"total": 10006, "normal": 9506, "dga": 500}, "test": {"total": 9991, "normal": 9491, "dga": 500}}`。`RunSummary` 的 `counts_by_split` 对每个 split 分别提供 label/source/qtype/behavior 子计数；还需包含每 split 的混合标签窗口数、唯一 qname 数、`(client_ip, window_start, qname)` 聚合样本数、时间范围及 `synthetic: true`。输入哈希键固定为 `config.json`、`data/splits.csv` 和 PSL 相对路径，另报输出 SHA-256。`behavior_counts` 来自内存事件的内部行为标签，不写入 CSV；其余可从 CSV 重算的摘要字段必须通过独立复算校验。

## 确定性规则

- 从根级 `seed`、`window_seconds`、`paths.psl_snapshot` 以及设计文档表 2 的 `log` 配置读取参数；默认目标 50,000 行、DGA 2,500 行。
- 对每一个整数配额使用最大余数法；总 DGA 数用 `floor(total_rows * dga_ratio + 0.5)` 取整。余数并列顺序固定为 split：`train, val, test`；正常场景：`routine, burst`；DGA 场景：`high_frequency, low_frequency, word_digit`；qtype：`A, AAAA, CNAME, TXT, NS`。
- 将随机流 seed 固定为 `int.from_bytes(sha256(f"{seed}|d3|{split}|{label}|{behavior}|{purpose}".encode("utf-8")).digest(), "big")`，再用 `random.Random`；不同 split、标签、场景、用途使用不同 `purpose`，不使用内建 `hash()`。
- 对场景事件数 `n`，组数为 `ceil(n / max_size)`。依次从仍能让剩余组满足 `[min_size, max_size]` 的可行整数区间均匀抽取组大小，末组取剩余值；`n=0` 不建组，正数无法满足最小组大小则报错。组内事件使用相同 qname、客户端及分钟窗口。
- 每 split 为 `ceil(high_frequency_group_count * mixed_window_fraction)` 个 DGA 高频组各配一个同 split、同客户端、同分钟窗口、不同 qname 的正常日常事件；配对查询属于既有日常事件配额，不增加目标行数。
- 所有窗口以 UTC epoch 每 60 秒对齐；时间范围按 train/val/test 顺序各占 `split_period_hours`，默认 24 小时，并依次从 `simulation_start_utc` 偏移 0、1、2 个时段。突发及高频窗口内相邻请求间隔按设计取整数毫秒 `[1000, 5000]`，完整序列跨度不超过 55 秒。最终按 `(timestamp, stable_order)` 排序，再编号 `event_id`。
- qtype 在每个 `(split, label)` 配额内按最大余数法分配，再以独立随机流打乱，避免 qtype 与场景顺序绑定。客户端使用 RFC 5737 TEST-NET 地址池；活动客户端数按 `floor(pool_size * active_client_fraction + 0.5)` 计算，事件组按固定随机流分配给活动/普通客户端群，使活动客户端承载事件比例接近 `active_event_fraction`。混合高频组及其配对查询作为同一个放置单元。按每 split 事件行复算活动客户端比例，验收误差不超过 ±2 个百分点。
- 输出固定契约列序、UTF-8 无 BOM、LF。输入、配额、事件和摘要字段校验全部通过后，才将 CSV 字节写入同目录临时文件；从临时文件复核 SHA-256 后，使用 `Path.replace` 替换目标。序列化、摘要校验、临时写入或替换任一步失败，都不得部分覆盖已有目标，并清理临时文件。

## 验证环境

按项目规范使用本机 `dns-dga` 环境；安装根目录由用户本机决定，以下示例可按实际位置调整：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3'
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
```

## Task 1：配置解析与整数配额

**Files:** `config.json`、`src/gen_log.py`、`tests/test_gen_log.py`。

- [x] 在 `tests/test_gen_log.py` 先为模块接口写配额测试：用 split 域名数 `train=2135, val=712, test=711` 和默认 50,000/5% 配置，断言 split 总量 `30003/10006/9991`、DGA 量 `1500/500/500`，并断言每一层配额总和不变。
- [x] 测试最大余数法余数并列顺序、`dga_ratio` 半数向上取整、未知/重复 tie order、负数或非整数总量、NaN/无穷权重、比例和不为 1 的错误。
- [x] 运行 `& $python -m pytest tests/test_gen_log.py -q`，确认首轮失败来自被测模块接口尚未定义，而不是测试 collection 或 fixture 错误。
- [x] 在 `config.json.log` 中按 D3 设计加入 `simulation_start_utc`、`split_period_hours`、`client_pool_size`、`active_client_fraction`、`active_event_fraction`、`normal_behavior_ratio`、`dga_behavior_ratio`、`mixed_window_fraction`、`qtype_weights`；保留现有 `total_rows_approx` 和 `dga_ratio`，不得改动模型或密钥配置。
- [x] 实现 `largest_remainder`、`allocate_event_quotas` 和配置解析/校验；检查 JSON 类型、精确权重键集合、比例和及取值范围，按上述固定顺序处理并列。
- [x] 重跑 `& $python -m pytest tests/test_gen_log.py -q`，配额与配置校验测试通过；用标准库重新解析 `config.json`，确认有效 JSON 且不含 API Key 字段。

## Task 2：split 输入、契约与离线 PSL 校验

**Files:** `src/gen_log.py`、`tests/test_gen_log.py`。

- [x] 为 `read_split_records` 写测试：接受且只接受 `domain,label,source,algorithm,split` 固定表头；测试重复域名、非规范化域名、标签/来源/算法语义错误、未知 split、空 split-label 池和缺失 `word_digit` 池均会失败。
- [x] 测试按固定本地 PSL 拒绝同一 eTLD+1 跨 split；用临时 PSL 覆盖普通后缀和 PRIVATE 规则，并 monkeypatch socket 网络入口为立即抛错，证明解析不依赖网络。
- [x] 实现 `read_split_records(path)` 与 `validate_domain_pools(records, psl_path)`；复用 `src.prepare_data.create_extractor` 的离线 PSL 设置和域名语义，不重新规范化或改写 `splits.csv` 中的值。
- [x] 校验 `label` 为 0/1；`normal_list` 必须为 label 0 且 algorithm 空；`public_dga` 必须为 label 1 且 algorithm 空；`manual_dga` 必须为 label 1 且 algorithm 属于三个契约 ID；split 集合必须为 train/val/test。
- [x] 每个 split 的正常与 DGA 样本池均须非空；`word_digit` 行为只可从同 split 的 `label=1, source=manual_dga, algorithm=word_digit` 域名池选择，不允许 fallback。
- [x] 运行 `& $python -m pytest tests/test_gen_log.py -q`，确认错误输入在输出写入前失败。

## Task 3：行为配额、确定性事件组与混合窗口

**Files:** `src/gen_log.py`、`tests/test_gen_log.py`。

- [x] 先测试每 split 的正常/DGA 行为配额、组大小边界与总行数保持；测试同组事件共用 qname/client/window，不同组默认窗口不重叠。
- [x] 测试对每 split 高频 DGA 组以 `ceil(group_count * mixed_window_fraction)` 选组，配对一条正常日常事件；配对不得增加总行数，必须同客户端/窗口、异 qname、同 split，且每 split 至少有一个混合标签窗口。先从日常事件配额中预留相同数量的单条事件，再对剩余事件分组，避免从已成组查询中拆出配对事件。
- [x] 测试同一 seed/config/input 两次构造的 `QueryEvent` 完全相同；随机流采用稳定摘要 seed，并验证仅改变一个 split 的输入行序不会改变其他 split 事件。
- [x] 测试每个事件的 qname 和 `label/source/algorithm/split` 都来自同一条合法 split 输入记录；每个 split 的 24 小时时段同时包含两种 label，qtype 配额按同一组权重分别计算，不将标签、来源或算法拼入 qname。
- [x] 实现 `derive_rng`、`allocate_behavior_quotas`、`partition_event_groups` 与 `build_behavior_groups`。使用设计中固定 tie order 和可行组大小抽样；正常日常、合法突发、高频 DGA、低频 DGA、词根 DGA 按设计配额生成。
- [x] 实现 TEST-NET 客户端池和固定随机流客户端分配；为每个 `(client_ip, split)` 跟踪占用窗口，混合事件作为一个放置单元，活动客户端承载的行比例与 `active_event_fraction` 相差不超过 ±2 个百分点。
- [x] 实现混合窗口配对后，从日志重算 client-window F/I，验证正常与 DGA 都出现低频窗口及 5–12 次窗口；正常突发/高频组的相邻事件间隔均符合设计区间。
- [x] 重跑 `& $python -m pytest tests/test_gen_log.py -q`，本任务的组行为、混合窗口与确定性测试通过。

## Task 4：qtype、时间排序与契约 CSV

**Files:** `src/gen_log.py`、`tests/test_gen_log.py`。

- [x] 先写 qtype/时间/CSV 测试：每个 `(split,label)` 的 qtype 配额精确；同类行为不固定绑定 qtype；时间戳 UTC、ISO 8601 毫秒、split 时间段不重叠、全文件单调不减；event_id 从 1 连续唯一。
- [x] 测试 CSV 精确列序 `event_id,timestamp,client_ip,qname,qtype,label,source,algorithm,split`、UTF-8 无 BOM、LF 行尾、qtype 枚举和所有共享字段遵守契约 v1。
- [x] 实现 `assign_qtypes`：对每个 `(split,label)` 独立做最大余数分配，使用 qtype 专用随机流打乱后赋给事件。
- [x] 实现窗口与 UTC 时间戳：train 起于 `simulation_start_utc`，val/test 分别偏移一个/两个 `split_period_hours`（默认即 24/48 小时）；使用 60 秒 UTC epoch 窗口、毫秒精度、行为所需的分散/聚集偏移，混合窗口全序列跨度不超过 55 秒。
- [x] 按 `(timestamp, stable_order)` 稳定排序后生成连续 `event_id`；用 `csv.DictWriter` 和 `lineterminator="\n"` 写固定列序，行为类别和内部序号不得输出。
- [x] 重跑 `& $python -m pytest tests/test_gen_log.py -q`，契约、时间、qtype 和排序测试通过。

## Task 5：运行摘要、原子替换与命令行入口

**Files:** `.gitignore`、`src/gen_log.py`、`tests/test_gen_log.py`。

- [x] 测试摘要至少包含输入哈希（config、splits、PSL）、输出哈希、seed、目标/实际总数、label/source/split/qtype/behavior 计数、每 split 混合窗口计数、唯一 qname、聚合键数、split 时间范围和 `synthetic=true`。从 CSV 独立复算行数、标签/来源/split/qtype 计数、混合标签窗口、唯一 qname、聚合键数、时间范围和输出哈希；`behavior_counts` 仅与内存事件的内部行为标签核对，不宣称可从 CSV 重建。另测试 CSV 每行与内存事件序列化一致，即使篡改算法并同步摘要哈希也必须失败。
- [x] 通过 monkeypatch `Path.replace` 或目标目录写入失败，测试已有 sentinel `dns_log.csv` 字节不变且临时文件清理；输入/配置/PSL错误也必须发生在替换之前。
- [x] 测试 `main()` 从当前目录读取 `config.json`、`data/splits.csv` 和配置 PSL；成功时 stdout 只有可解析 JSON 摘要，失败时返回非零并输出错误到 stderr，不打印 API Key 或原始域名列表。
- [x] 在独立临时项目中调用 `run_log_generation` 和 `main()` 做端到端测试；子进程通过 `sitecustomize.py` 封锁并自检 TCP、UDP 发送及正反向 DNS socket 入口；确认生成成功、输出哈希可复算、连续两次生成逐字节一致。
- [x] 实现 `serialize_log(events)`、`build_summary(...)`、`validate_summary(...)`、`write_log(csv_bytes, target)`、`run_log_generation(project_root, config_path=None)` 和 `main()`。严格按顺序执行：校验输入与配额；构造并校验全部事件；序列化 CSV 字节；计算输入和预期输出哈希；建立摘要并对照事件及 CSV 字节校验；将字节写入目标同目录临时文件并复核其 SHA-256；最后才调用 `Path.replace`。任一前置步骤失败时不接触现有目标文件。
- [x] 在 `.gitignore` 追加精确规则 `data/dns_log.csv`；先审阅已有 `.gitignore` 差异并保留 D2 现有规则。普通 `git check-ignore -v data/dns_log.csv` 对已跟踪路径不输出；`git check-ignore -v --no-index data/dns_log.csv` 确认新增规则匹配。仓库根目录的 CSV 已被 Git 跟踪，本计划未修改其内容或索引，因此忽略规则当前不能保护该既有文件。
- [x] 运行 `python -m src.gen_log` 的 CLI 单测（以临时项目 cwd 和仓库 `PYTHONPATH` 执行），成功输出 JSON、失败返回码非零；目标文件只存在于临时项目。

## Task 6：全套验收与本机重放

**Files:** `src/gen_log.py`、`tests/test_gen_log.py`、`config.json`、`.gitignore`；运行时输出 `data/dns_log.csv`。

- [x] 运行定向测试：`& $python -m pytest tests/test_gen_log.py -q`，所有失败先定位并修复，不屏蔽测试或降低断言（135 passed）。
- [x] 运行全套测试：`& $python -m pytest tests/ -q`，确认 D2、D5/D6 现有测试无回归（199 passed）。
- [x] 在临时 fixture 项目用默认 D3 配置做 50,000 行离线端到端重放，断言 47,500 正常/2,500 DGA、该 fixture 的 split 配额可按最大余数法复算、每 split 有混合窗口、CSV 合同与摘要复算一致，并记录运行输出哈希：`62a46684e3c21f93eb7b5ac0fb8b80cf54b987b2ce796ccb1cf94203a27dab56`。
- [ ] 根目录 `data/dns_log.csv` 已确认存在且由 Git 跟踪；因来源/哈希未核验，未读取、覆盖或删除，也未从索引移除。取得数据来源和覆盖授权前不得在仓库根目录运行生成器。
- [x] 对仓库根目录运行忽略规则检查（普通 `git check-ignore` 因文件已跟踪无输出；`--no-index` 命中新增规则）；检查 `git status --short` 和 `git diff --check`。未在根目录生成日志，未触碰 Git 索引；`git diff --check` 通过，现存 D2 文本文件仅有 LF/CRLF 转换警告。
- [x] 不提交或推送。`data/dns_log.csv` 在上游许可确认或更换数据源前始终本机保留。

## D3 完成判据

- 离线生成 50,000 行，正常/DGA 行比例精确为 95/5，split 和各行为/qtype 配额按固定最大余数规则可复算。
- 输出严格遵循数据契约 v1；跨 split 域名及 eTLD+1 泄漏、元数据语义错误、缺失行为池均会在写盘前失败。
- 固定输入和参数生成结果逐字节一致；正常突发、低频 DGA、词根型 DGA及混合标签窗口均覆盖，客户端级 F/I 重叠能从原始日志重算。
- 生成无网络与 DNS 请求；摘要哈希和计数可复算；失败不损坏已有目标文件。
- 定向测试与完整 `tests/` 均通过；当前 Mendeley 派生数据只作本机验证，不提交。
