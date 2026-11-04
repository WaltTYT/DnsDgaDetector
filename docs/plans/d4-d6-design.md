# D4–D6 设计：教材原理复现、特征工程与规则引擎

> 状态：设计草案，待负责人评审
> 日期：2026-11-04
> 范围：阶段 2 的 D4（DNS 查询演示）、D5（特征与基线）、D6（规则引擎）；不涉及 D7 及以后的 LLM 判断、正式评估或总流程集成。
> 依据：[项目路线图](../specs/project-roadmap.md) 阶段 2、2.2–2.4 节；[数据契约 v1](data-contracts.md)；[课本 DNS 原文](../课本原文-第6章-应用层-DNS与FTP.md) 6.1 节。
> 负责人：组员 B

---

## 0. 当前环境与交接基线

| 项目 | 实际状态 | 与预期的差异 |
|---|---|---|
| DNS nameserver | 系统配置三个：`114.114.114.114`、`192.168.1.1`、`192.168.3.1` | 非交接描述中的 `192.168.2.1`；`dns_demo.py` 需先探测并记录实际 nameserver，所有查询描述以实际为准 |
| PSL 快照 | `data/raw/public_suffix_list.dat` 不存在 | `config.json.paths.psl_snapshot` 指向该文件，但 `tldextract 5.4.0` 自带内置 PSL 快照。两种方案可行：(a) 用 `tldextract` 内置快照 + `include_psl_private_domains=False` + 禁用缓存与联网；(b) 下载一份快照到 `data/raw/`。选 (a)，无需额外文件，tldextract 5.4.0 内置快照经测试可离线解析 `example.co.uk` 等 |
| 运行环境 | Python 3.13.14，numpy 2.4.4，pandas 3.0.3，dnspython 2.9.0，tldextract 5.4.0，pytest 9.1.1，matplotlib | 项目要求 Python 3.11.17；3.13 下关键库全部导入成功，版本差异记录在 baseline.json 中 |
| D1–D3 数据交接 | `data/dns_log.csv`、`data/splits.csv` 未在仓库；`results/features.csv`、`data/dga_domains.csv` 仅有表头 | 特征计算和基线拟合暂不能用真实 D1–D3 数据端到端验证；代码实现先保证逻辑正确，待负责人提供已授权的数据集后再做端到端运行。测试全部使用 `tmp_path` 自建 fixture |

---

## 1. D4 教材原理复现：DNS 查询演示

### 1.1 目标

D4 的定位是**教材原理的可验证复现**，不是真实网络行为观测：

1. 用 dnspython 对少量已知正常域名做受控查询，记录请求/响应并逐字段解码。
2. 准确解释 stub→递归解析器 与 递归解析器→根/TLD/权威 的区别，**不把普通 Resolver 调用当成逐级迭代演示**，不声称看到了递归解析器内部向根/TLD/权威的查询。
3. 保留最少数量的抓包证据（若环境允许）。

**验收**：能够从 `dns_demo.py` 的输出看到 DNS Header、Question、Answer、NS、Additional 各节的完整解码，能区分递归和迭代的设置位，能在报告中准确复述教材第 6 章 6.1.3 节的查询流程。

### 1.2 输入与输出

| 方向 | 内容 |
|---|---|
| 输入 | 内置硬编码的少量正常域名（`www.baidu.com`、`www.qq.com`、`www.example.com`、`mail.cctv.com`、`github.com`）；默认查询类型 A；nameserver 从系统配置读取或显式指定 |
| 输出（命令行） | 每次查询的格式化打印：查询名、类型、nameserver、递归/迭代标志位、Opcode、RCODE、Header 标志、Question 节、Answer 节（RR 类型、TTL、RDATA）、Authority 节、Additional 节、耗时 |
| 输出（文件，可选） | `report/evidence/dns_demo_YYYYMMDD.json`：结构化 JSON 记录，包含原始报文的二进制表示（base64）与解码后字段；脱敏，不含凭证。抓包 `.pcapng` 由操作者手工在 Wireshark 中保存，文件名含日期，过滤器 `udp.port == 53 or tcp.port == 53` |

### 1.3 设计决策

#### 1.3.1 dnspython Resolver 行为与递归/迭代

dnspython 的 `dns.resolver.Resolver` 向配置的 nameserver 发送**递归查询请求**（RD=1）。客户端（stub resolver）只和本地/配置的递归解析器交互一次，看不到递归解析器内部做的根→TLD→权威的迭代。教材第 267 页明确说："主机向本地域名服务器的查询一般都采用递归查询"，"本地域名服务器向根域名服务器的查询通常采用迭代查询"。

因此 D4 的脚本只能：

- **正确设置查询报文中的 RD 位** = 1，**依赖位 RA 由响应返回**；
- **展示响应报文中的 RA 位**：若递归解析器返回 `RA=1`，说明服务器支持递归；
- **展示 Authority 节中的 NS 记录**（若响应非权威）和 SOA 记录；
- **展示 Additional 节中的 AAAA 记录**（根服务器提示的下一步服务器地址）；
- 在输出和报告中明确**声明**：这些内容是"递归解析器返回给 stub 的答复"，不是"我们观测到的根/TLD/权威内部查询"。

D4 **不做**的事情：

- 不尝试直接向根服务器发起查询（`a.root-servers.net` 的 IP 在当前网络可能被拦截或不返回权威答复）；
- 不宣称看到了根→TLD→权威的完整查询链路；
- 不把 Authority 节中出现的 NS 记录等同于"我们亲自做了迭代查询"。

#### 1.3.2 查询域名单与场景

选择 5–8 个覆盖不同情况的域名：

| 域名 | 场景 | 预期 RCODE | 预期 Answer 特征 |
|---|---|---|---|
| `www.baidu.com` | 国内常用，CDN | NOERROR | 多条 A 记录，TTL 较短 |
| `www.qq.com` | 国内常用 | NOERROR | 可能有 CNAME 链 |
| `www.example.com` | IANA 保留，稳定 | NOERROR | 固定 104.x 和 172.x IP |
| `mail.cctv.com` | 教材举例 | NOERROR | 可能 CNAME + A |
| `github.com` | 国外服务 | NOERROR | AAAA 可能有 |
| `this-domain-should-not-exist-xyz12345.example` | NXDOMAIN 演示 | NXDOMAIN | Answer 为空，Authority 含 SOA |

每次运行按顺序查询，每个域名查询一次；整个演示总共 ≤ 8 次查询，不构成外网依赖。

#### 1.3.3 报文解码字段清单

对每个响应，脚本逐字段打印（字段名与 RFC 1035 一致）：

```
Header  12 字节：ID, QR, Opcode, AA, TC, RD, RA, Z, RCODE, QDCOUNT, ANCOUNT, NSCOUNT, ARCOUNT
Question: QNAME, QTYPE, QCLASS
Answer  : 每条 RR 的 NAME, TYPE, CLASS, TTL, RDLENGTH, RDATA（按类型解码 A→IPv4, AAAA→IPv6, CNAME→目标域, NS→服务器域, TXT→字符串, SOA→权威源信息）
Authority: 同上（通常 SOA 或 NS）
Additional: 同上
```

RDATA 解码由 dnspython 库直接提供 `.to_text()` 或按类型专门处理。

#### 1.3.4 错误码（RCODE）展示

教材第 266 页提到的常见 RCODE 需展示：

| RCODE 值 | 名称 | 含义 | D4 演示方式 |
|---:|---|---|---|
| 0 | NOERROR | 正常 | 正常域名查询 |
| 3 | NXDOMAIN | 域名不存在 | 对不存在域名查询 |
| 2 | SERVFAIL | 服务器失败 | 可能，不专门构造 |
| 5 | REFUSED | 服务器拒绝 | 可能，不专门构造 |

#### 1.3.5 抓包证据

操作者用 Wireshark 在发出查询前启动捕获，应用过滤器 `udp.port == 53 or tcp.port == 53`，查询完成后停止并保存 `report/evidence/dns_demo_YYYYMMDD.pcapng`。脚本输出和抓包截图在报告中交叉引用。

#### 1.3.6 离线/测试模式

- `python -m src.dns_demo --dry-run`：只打印将要查询的域名和配置的 nameserver，不实际发送请求。测试环境中使用此模式避免依赖外网。
- `python -m src.dns_demo --nameserver <ip>`：显式指定 nameserver，覆盖系统默认。便于在已知 nameserver 场景下复现。
- 所有单元测试使用 `--dry-run` 或 monkeypatch `dns.resolver.Resolver.resolve`，不发送真实网络请求。

### 1.4 D4 交付物

| 交付物 | 位置 | 验收 |
|---|---|---|
| DNS 查询演示脚本 | `src/dns_demo.py` | 命令行可运行；输出包含完整报文解码；有 `--dry-run` 和 `--nameserver` 选项 |
| 结构化 JSON 记录 | `report/evidence/dns_demo_*.json` | 包含查询、响应、耗时、错误码；脱敏 |
| 抓包 pcapng | `report/evidence/dns_demo_*.pcapng` | Wireshark 过滤器可验证 |
| 解析流程说明 | 报告第一部分引用 | 区分 stub/递归；不夸大递归内部行为 |

### 1.5 D4 明确不做

- 不下载或抓取完整递归查询链路的报文；
- 不实现真正的迭代 DNS 客户端；
- 不把 D4 结果用于特征工程；
- 不宣称任何关于 DGA 的检测结论。

---

## 2. D5 特征工程与基线

### 2.1 目标

D5 路线图产出：`features.py`（三项特征计算）、`baseline.py`（阈值拟合）、`results/baseline.json` 和基线分布图。核心约束：

1. **聚合键**：`(client_ip, window_start, qname)`，其中 `window_start` 为 UTC 整分钟 `YYYY-MM-DDTHH:MM:00Z`。
2. **PSL 必须离线**：用固定本地快照（tldextract 内置 5.4.0 自带），禁用 `cache_dir` 和联网更新。
3. **基线只看 train + label=0**：基线拟合不得触碰 val/test 或 label=1。
4. **元数据隔离**：`label/source/algorithm/split` 只用于评估和基线样本筛选，**不得进入特征向量**。

### 2.2 特征计算口径

#### 2.2.1 域名预处理

所有 qname 先规范化（小写、去末尾根点、IDNA）。规范化逻辑与 D3 一致（D3 已写入 dns_log.csv），features.py 做防御性再规范化，保证健壮性。

#### 2.2.2 可注册域标签提取（用于熵 H）

用 tldextract 的内置快照 + `include_psl_private_domains=False`：

```python
ext = tldextract.TLDExtract(include_psl_private_domains=False, 
                            cache_dir=None,
                            fallback_to_snapshot=False)
result = ext.extract(qname)
```

提取的"标签"为 `result.domain`（即公共后缀 `result.suffix` 左侧、公共后缀之前的第一个 label）。

示例：

| qname | suffix | 标签（entropy 输入） | 说明 |
|---|---|---|---|
| `mail.cctv.com` | `com` | `cctv` | 二级域 |
| `sub.example.co.uk` | `co.uk` | `example` | 正确识别公共后缀 |
| `github.com` | `com` | `github` | 二级域 |

**非法标签处理**：当 `result.domain == ""`（无法提取）、qname 是 IP 字面量（`^\d+\.\d+\.\d+\.\d+$`）、或 qname 含 `xn--` Punycode 时，在 features.csv 的对应行记录状态字段（见 2.2.5）。

#### 2.2.3 熵值 H

香农熵（bit/字符）：

```
H(label) = -Σ p(c) * log₂(p(c))   对 label 中出现的每个字符 c
```

- 输入：提取的可注册域标签（不是完整 qname）。
- 计算：按每个字符在标签中的相对频率 `p(c) = count(c) / len(label)`。
- 保留 4 位小数，写入 `H` 列。
- 短标签（长度 1–3）标注状态，说明有限样本偏差。
- 熵计算的字符集：整个 ASCII 可打印字符，连字符 `-` 纳入计算。

#### 2.2.4 窗口频率 F

同客户端（`client_ip`）在该 60 秒窗口内的**全部查询次数**（所有 qname 合计）。

- 计算单元：`(client_ip, window_start)`，每个窗口一个 F 值。
- 关联方式：同一 `(client_ip, window_start)` 内所有聚合样本（不同 qname）共享同一个 F 值。
- 注意：这意味着混合窗口（既有正常又有 DGA 的窗口）中的正常样本**继承**该窗口的 F 值，这是路线图明确要求的，不是 bug。
- F 写入 features.csv 的 `F` 列。

#### 2.2.5 相邻查询间隔中位数 I

同客户端在该窗口内相邻查询时间差的**中位数**（秒，保留 3 位小数）。

- 计算单元：`(client_ip, window_start)`，与 F 同粒度。
- 步骤：窗口内事件按 timestamp 升序，计算相邻事件差（秒），取中位数。
- **少于 3 次查询时，I 记为空值（`None`/`NaN`）**，不填 0，不填字符串 "NA"。
- 缺失 I **不触发间隔规则**（由 rule_engine.py 在检查时跳过缺失值）。
- I 写入 features.csv 的 `I` 列，缺失时 CSV 对应位置为空。

### 2.3 baseline 阈值拟合

#### 2.3.1 输入筛选

从 dns_log.csv 中筛选：`split == "train" AND label == 0`。

- 这是基线拟合的**唯一**数据来源。
- 读取 dns_log.csv 时用 pandas，筛选后再做特征计算。
- 如果有人不小心把 val/test 或 label=1 的数据混入拟合，代码应能检测并报错（D5 不做自动修复，发现问题直接 fail-fast）。

#### 2.3.2 阈值计算

| 特征 | 统计对象 | 分位数 | 比较运算符 | 说明 |
|---|---|---:|---|---|
| H | 训练集正常域的**唯一**可注册域标签上的熵分布 | P99 | `>`（严格大于） | 对重复域名去重后再算分位数，避免高频域名过度加权 |
| F | 训练集正常**窗口级**频率分布（每窗口一个值，所有正常客户端、所有窗口） | P99 | `>`（严格大于） | 窗口级，不按域名聚合；与 F 的计算粒度一致 |
| I | 训练集正常**窗口级**间隔分布（只统计有 ≥3 次查询的窗口） | P1 | `<`（严格小于） | 缺失值不参与分位数计算；P1 是"短间隔"阈值的正确方向 |

**分位数实现**：使用 `numpy.percentile(data, q, method='linear')`（numpy 默认线性插值，与 pandas `Series.quantile(interpolation='linear')` 等价）。

#### 2.3.3 P1=0 的边界情况

若训练集正常窗口的间隔分布中 P1 == 0（理论上可能，当大多数窗口只有少量事件且间隔刚好为整秒），baseline.json 中记录 `I_threshold=0.0` 并标注 `"i_rule_potentially_ineffective": true`，rule_engine.py 的间隔检查逻辑仍按 `< 0.0` 执行（此时不会有样本命中）。

#### 2.3.4 baseline.json 输出格式

```json
{
  "version": 1,
  "window_seconds": 60,
  "fitted_on": {"split": "train", "label": 0},
  "fitted_at": "2026-11-04T12:00:00Z",
  "python_version": "3.13.14",
  "numpy_version": "2.4.4",
  "pandas_version": "3.0.3",
  "psl_source": "tldextract==5.4.0 built-in snapshot",
  "features": {
    "H": {"quantile": 0.99, "threshold": 4.18, "n_unique_labels": 2135, "n_missing": 0, "method": "numpy.percentile method=linear"},
    "F": {"quantile": 0.99, "threshold": 32,  "n_windows": 4500,   "n_missing": 0, "method": "numpy.percentile method=linear"},
    "I": {"quantile": 0.01, "threshold": 2.15, "n_windows_valid": 3800, "n_windows_total": 4500, "i_rule_potentially_ineffective": false, "method": "numpy.percentile method=linear"}
  },
  "freeze_check": "not frozen until D7"
}
```

同时输出基线分布图：`results/figures/baseline_histograms.png`，三个子图分别是 H、F、I 的分布直方图，阈值位置用红色竖线标注。

### 2.4 features.csv 写入契约（v1 第 4 节）

features.py 写出 features.csv，**严格遵循数据契约 v1 的固定表头与列顺序**：

```text
client_ip,window_start,qname,label,source,algorithm,split,H,F,I,E,B,R,llm_verdict,llm_fail_reason,hybrid
```

D5 只填前 11 列（到 `R`）；`llm_verdict`、`llm_fail_reason`、`hybrid` 留空给组长 D10。D5 在写出时**只填入规则判定后的 E/B/R 值**（E/B/R 由 rule_engine.py 调用后回填）。

features.csv 的生成流程：

```
dns_log.csv
  │
  ▼  features.py
  ├─ 规范化 qname
  ├─ PSL 提取标签 → 计算 H
  ├─ 按 (client_ip, window_start) 计算 F 和 I
  ├─ 聚合到 (client_ip, window_start, qname) 粒度
  │
  ▼  rule_engine.py
  ├─ 加载 baseline.json 阈值
  ├─ 计算 E (H > threshold_H)
  ├─ 计算 B (F > threshold_F OR I < threshold_I)，缺失 I 跳过间隔检查
  ├─ 计算 R = E OR B
  │
  ▼
  results/features.csv（D5+D6 产出，前 11 列完整）
```

实际实现上 features.py 和 rule_engine.py 可以分模块：features.py 先产出聚合+特征值的中间 DataFrame，rule_engine.py 加载阈值后计算 E/B/R 并写出完整 features.csv。

### 2.5 边界与错误处理

| 情况 | 处理 |
|---|---|
| dns_log.csv 缺失 | 报错并退出；测试用 tmp_path 构造 fixture |
| dns_log.csv 表头不匹配契约 | 用 pandas `pd.read_csv(...).columns.tolist()` 与契约列序精确比较，不一致直接 raise |
| PSL 快照文件缺失但 config.json 指向它 | 运行时 fallback 到 tldextract 内置快照并在 stderr 警告；不静默忽略 |
| qname 是 IP 字面量 | H 记空值、状态标记；不静默赋 0 |
| qname 无法提取可注册域标签（`result.domain == ""`） | 同上 |
| label 长度为 1–3 | 不报错，但在 baseline.json 中记录短标签占比 |
| I 缺失（<3 次查询） | I 记 NaN；rule_engine 计算 B 时跳过间隔部分 |
| split 不是 train/val/test | 报错 |
| label 不是 0/1 | 报错 |

---

## 3. D6 规则引擎

### 3.1 目标

实现纯规则判定：`E = 熵规则命中`，`B = 频率规则命中 OR 间隔规则命中`，`R = E OR B`。输出每条规则的命中证据。在验证集上比较少量预定义阈值候选组合，确定 D7 前的基线阈值。

### 3.2 规则定义

| 规则 | 判定逻辑 | 输入 | 输出 | 备注 |
|---|---|---|---|---|
| 熵规则 E | `H > baseline.H.threshold` | 每个样本的 H 值 | 0/1 | 严格大于，不是 >= |
| 频率规则 | `F > baseline.F.threshold` | 每个样本的 F 值 | 0/1 | 同上 |
| 间隔规则 | `I < baseline.I.threshold` | 每个样本的 I 值 | 0/1 | 严格小于；I 缺失时该规则不生效（按 False 处理） |
| 行为规则 B | `频率规则 OR 间隔规则` | 频率规则、间隔规则结果 | 0/1 | 只要其一命中即触发 |
| 最终规则 R | `E OR B` | E、B | 0/1 | E 或 B 任一命中即告警 |

### 3.3 阈值候选比较（验证集）

`config.json.threshold_candidates` 已经预留了候选数组：

```json
"threshold_candidates": {
    "H_quantile": [0.99],
    "F_quantile": [0.99],
    "I_quantile": [0.01]
}
```

D6 的验证集上比较策略：

1. 先用 D5 已完成的 P99/P99/P1 基线阈值跑一遍，得到第一轮 R 结果。
2. **仅在验证集**上，用候选组合重新拟合阈值并评估，候选组合在 config.json 中预定义（当前只有一组）。
3. 若后续需要加候选，在 config.json 中追加数组元素（例如 `H_quantile: [0.98, 0.99]`），代码自动遍历笛卡尔积。
4. **绝不触碰 test 集**，阈值选择完成后冻结。

**为什么只用 config.json 预定义的候选，不自动搜索**：避免把验证集当测试集过度调参。路线图说"比较少量预先列出的分位数组合"。

### 3.4 输出与证据

规则引擎为每个样本计算 E/B/R，同时记录可追溯的证据：

```
样本 (10.0.0.5, 2026-11-01T12:05:00Z, xk9q2m7n3p4z.com)
  H=4.31 (threshold=4.18)  → E=1  [entropy_hit: H > 4.18]
  F=28   (threshold=32)    → 频率规则=0
  I=5.2  (threshold=2.15)  → 间隔规则=0
  B = 0 OR 0 = 0
  R = 1 OR 0 = 1  [entropy_rule_hit]
```

在 features.csv 中，E/B/R 直接写 0/1。报告中按样本展示证据摘要，对每类规则统计：总命中数、TP/FP/FN/TN、代表性误报和漏报案例。

### 3.5 客户端行为连带告警

路线图 2.3 节提醒："客户端级行为规则可能连带标记同窗口的正常域名"。原因：

- F 和 I 是 `(client_ip, window_start)` 粒度的；
- 混合窗口中（既有正常 qname 又有 DGA qname 的窗口），正常 qname 继承该窗口的 F 和 I；
- 当窗口内 DGA 高频查询导致 F 超标时，同窗口正常 qname 也会被频率规则命中。

D6 代码中需显式**注释并输出**这一现象的统计：

- 统计 features.csv 中 `label=0 AND B=1` 的样本数；
- 按 split 分别报告；
- 这些连带告警中的一部分是**不可避免的**（客户端行为视角），LLM 复核阶段可对其中 E=0 但 B=1 的样本选择放行与否的策略——这属于 D7，当前不处理，D6 只做统计和标注。

### 3.6 rule_engine.py 接口契约

```python
# 输入
features_df = pd.read_csv(...)  # 来自 features.py 的聚合+特征 DataFrame
baseline = load_baseline("results/baseline.json")

# 计算 E/B/R
features_df["E"] = (features_df["H"] > baseline["features"]["H"]["threshold"]).astype(int)
# 频率规则
freq_hit = (features_df["F"] > baseline["features"]["F"]["threshold"])
# 间隔规则：I 非空时判断，缺失时跳过
interval_hit = features_df["I"].notna() & (features_df["I"] < baseline["features"]["I"]["threshold"])
features_df["B"] = (freq_hit | interval_hit).astype(int)
features_df["R"] = (features_df["E"] | features_df["B"]).astype(int)

# 输出
features_df.to_csv("results/features.csv", index=False)
```

### 3.7 D6 验证集阈值候选比较流程

```python
for h_q in config["threshold_candidates"]["H_quantile"]:
    for f_q in config["threshold_candidates"]["F_quantile"]:
        for i_q in config["threshold_candidates"]["I_quantile"]:
            # 只用 train+label=0 重新拟合阈值
            baseline = fit_baseline(train_normal_df, H_quantile=h_q, F_quantile=f_q, I_quantile=i_q)
            # 在 val 上计算规则
            val_df = compute_rules(val_df, baseline)
            # 评估
            metrics = compute_accuracy(val_df, truth_col="label", pred_col="R")
            # 记录
            candidate_results.append({"H_q": h_q, "F_q": f_q, "I_q": i_q, **metrics})
# 选择最优组合（或只有一组时直接用），更新 baseline.json 冻结
```

---

## 4. 设计文档到代码的映射

| 设计节 | 源文件 | 测试文件 |
|---|---|---|
| §1 D4 DNS 查询演示 | `src/dns_demo.py` | （D4 是网络演示，测试用 `--dry-run` + monkeypatch） |
| §2.2–2.3 特征计算 | `src/features.py` | `tests/test_features.py` |
| §2.3–2.4 基线拟合 | `src/baseline.py` | （包含在 test_features.py 或独立） |
| §3 规则引擎 | `src/rule_engine.py` | `tests/test_rule_engine.py`（新增） |
| config.json 阈值候选 | — | `tests/test_rule_engine.py` 验证候选遍历逻辑 |
| results/baseline.json 格式 | — | `test_features.py` 验证 JSON schema |
| results/features.csv 契约 | — | `test_features.py` + `test_rule_engine.py` 验证表头顺序 |

## 5. 测试策略

### 5.1 test_features.py（补全）

| 测试场景 | 断言 |
|---|---|
| 香农熵手动计算 | `"hello"` → H ≈ 1.9219（手动算）；所有唯一字符等概率时 H = log₂(n) |
| PSL 离线提取 | `mail.cctv.com` → 标签 `cctv`；`sub.example.co.uk` → 标签 `example`；禁用联网后仍能正确解析 |
| UTC 整分钟对齐 | `2026-11-01T12:05:45.123Z` → window_start `2026-11-01T12:05:00Z`；`2026-11-01T12:05:59.999Z` → 同上；跨分钟边界的 `2026-11-01T12:06:00.000Z` → `2026-11-01T12:06:00Z` |
| 间隔缺失 | 窗口内只有 2 次查询 → I=NaN；3 次及以上 → 正确计算中位数 |
| 非法 qname | IP 字面量、`xn--` 标签、空域名 → 状态标记，H 为空，不静默赋 0 |
| F/I 混合窗口关联 | 构造一个窗口：事件 1、5、10 秒，来自同一 client_ip → F=3，I=中位数(4,5)=4.5；同窗口另一 qname 的聚合样本继承同样 F 和 I |
| baseline 只读 train+label=0 | 传入含 val/test/label=1 的数据 → 只在 train+label=0 子集上拟合；缺失 train+label=0 → 报错 |
| baseline.json schema | 检查必需键、版本号、fitted_on 约束 |
| baseline 阈值比较运算符 | H 用 >、F 用 >、I 用 <；验证边界值（等于阈值时不命中） |
| baseline P1=0 边界 | 构造间隔全为 1 秒的分布 → P1=0，baseline 记录 `i_rule_potentially_ineffective=true` |
| features.csv 表头契约 | 与 data-contracts.md v1 第 4 节精确比较，列名和顺序一致 |

### 5.2 test_rule_engine.py（新增）

| 测试场景 | 断言 |
|---|---|
| 规则命中逻辑（手动样例） | 构造 5 行特征 DataFrame，手动算 E/B/R，与 rule_engine 输出比对 |
| 间隔缺失时不触发 | I 为 NaN 的样本，间隔规则 False；B 只由频率规则决定 |
| 客户端行为连带告警 | 构造混合窗口（正常+DGA），正常样本的 F/I 超标 → B=1，R=1 |
| config.json 候选遍历 | 传入多组 quantile 候选，验证笛卡尔积生成正确、遍历顺序稳定 |
| 验证集阈值选择不触碰 test | 候选比较流程只取 split=val，不会把 split=test 送进拟合或评估 |
| rule_engine 不读取 label/source/algorithm/split 做决策 | E/B/R 只由 H/F/I 和阈值决定，断言计算时未访问元数据列 |
| baseline.json 缺失/损坏 | 报错并退出，不静默使用默认阈值 |

### 5.3 dns_demo.py 测试

用 `--dry-run` + monkeypatch：

| 场景 | 断言 |
|---|---|
| 域名列表打印 | dry-run 模式下输出将查询的域名和 nameserver |
| nameserver 覆盖 | `--nameserver` 参数正确生效 |
| 响应解码完整性 | monkeypatch 返回已知响应，断言各节字段被正确打印 |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 无真实 dns_log.csv | 测试全部用 tmp_path fixture；代码对 dns_log.csv 缺失直接报错；待负责人提供已授权数据集后做端到端验证 |
| tldextract 版本升级破坏离线行为 | 锁定 tldextract==5.4.0；设计文档已注明用内置快照而非外部文件 |
| P1=0 导致间隔规则失效 | baseline.json 显式记录并标注；rule_engine 的间隔检查逻辑仍按 `< 0.0` 执行（此时自然不会命中） |
| 环境 DNS 被劫持导致 D4 查询返回伪造结果 | D4 输出如实打印 RCODE 和应答内容，不把可疑应答当作正常记录；D4 查询只是教材演示，特征工程不依赖真实 DNS 查询结果 |
| features.csv 与组长（D10）追加的列不兼容 | 严格遵循契约 v1 第 4 节表头与顺序；D5/D6 只填前 11 列，组长 D10 追加最后 3 列；任何契约变更先升级版本 |

## 7. 交付检查清单

- [ ] `src/dns_demo.py`：命令行可运行，输出完整报文解码，有 `--dry-run` 和 `--nameserver`
- [ ] `src/features.py`：PSL 离线，熵/频率/间隔计算正确，边界处理完整
- [ ] `src/baseline.py`：只拟合 train+label=0，分位数/阈值/缺失统计完整，输出 baseline.json 和分布图
- [ ] `src/rule_engine.py`：E/B/R 逻辑正确，缺失间隔跳过，验证集候选比较不触碰 test
- [ ] `tests/test_features.py` 补全：熵、PSL、窗口、间隔、非法标签、混合窗口、baseline schema、比较运算符
- [ ] `tests/test_rule_engine.py` 新增：E/B/R 手动样例、间隔缺失、连带告警、候选遍历、验证集隔离、元数据隔离
- [ ] 现有 D1–D3 测试保持通过
- [ ] 完整 pytest 运行：无回归
- [ ] 自检清单全部通过（AGENTS.md §10）
