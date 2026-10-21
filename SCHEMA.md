# 数据契约（SCHEMA）v1-草案

> 状态：**草案，待三人 D1 评审定稿**。定稿后任何修改必须三人同意，并同步升级版本号。
> 依据：plan.md 2.2、2.4、2.5、7.2。
> 通用规则：所有时间戳为 UTC，ISO 8601 格式 `YYYY-MM-DDTHH:MM:SS.mmmZ`（毫秒精度，DGA 高频场景需要）；域名一律规范化（小写、去末尾根点、IDNA 编码 ASCII）；CSV 用 UTF-8 无 BOM，逗号分隔，禁止字段内出现未转义逗号。

## 1. data/dns_log.csv — 原始查询日志（组员A 写，组员B/组长 读）

| 列 | 类型 | 取值/格式 | 说明 |
|---|---|---|---|
| event_id | int | 从 1 递增，全文件唯一 | 排序键，与 timestamp 共同定序 |
| timestamp | str | ISO 8601 UTC 毫秒 | 单调不减 |
| client_ip | str | IPv4 点分十进制 | 来自固定客户端池（池大小与分布见生成配置） |
| qname | str | 规范化域名 | 特征与复核的唯一输入来源 |
| qtype | str | `A` / `AAAA` / `CNAME` / `TXT` / `NS` | 主场景以 A 为主 |
| label | int | `0`=正常 `1`=DGA | **元数据**，禁止进入特征与 LLM 输入 |
| source | str | `normal_list` / `public_dga` / `manual_dga` | **元数据** |
| algorithm | str | `rand_alpha` / `word_digit` / `md5_date` / 空（正常样本） | **元数据** |
| split | str | `train` / `val` / `test` | **元数据**，禁止跨集合读取 |

## 2. data/splits.csv — 域名划分（组员A 写，全员读）

`domain,label,source,algorithm,split`：语义同上；一行一个唯一域名；同一可注册域（eTLD+1）的所有行 split 必须一致。

## 3. data/dga_domains.csv — 手工 DGA 生成记录（组员A 写）

`domain,algorithm,seed,params`：params 为 JSON 字符串（如 `{"len":12,"tld":".com"}`）；同 algorithm+seed+params 必须可重生成同一域名。

## 4. results/features.csv — 聚合样本表（组员B 写特征列，组长追加结论列）

一行 = 一个 `(client_ip, window_start, qname)` 聚合样本。

| 列 | 类型 | 取值/格式 | 写方 |
|---|---|---|---|
| client_ip, window_start, qname | — | 聚合键；window_start 为 UTC 整分钟 `YYYY-MM-DDTHH:MM:00Z` | B |
| label, source, algorithm, split | — | 同 dns_log 语义，由域名真值继承；同键冲突即报错 | B |
| H | float | 香农熵（bit/字符），保留 4 位小数 | B |
| F | int | 该客户端该窗口总查询次数 | B |
| I | float 或空 | 窗口内相邻查询间隔中位数（秒，3 位小数）；查询<3 次留空 | B |
| E, B, R | int | 规则命中标志 `0/1`，`R = E OR B` | B |
| llm_verdict | str | `na`（未送审）/ `DGA` / `normal` / `uncertain` | 组长 |
| llm_fail_reason | str | 空 / `timeout` / `rate_limit` / `server_error` / `format_error` / `no_cache`（非空即按 uncertain 参与判定并单列统计） | 组长 |
| hybrid | int | 最终结论 `0/1`：`hybrid = B OR (E AND verdict != normal)` | 组长 |

## 5. results/llm_cache.jsonl — LLM 调用缓存（组长写，评估读）

一行一个 JSON 对象，字段：

```
cache_key      str  sha256(规范化输入 + model + prompt_template_hash + 生成参数 + 接口标识)
input          obj  {"qname": ..., "registrable_label": ...}   ← 仅此两项，无真值
model          str  如 GLM-4-Flash
prompt_template_hash  str  模板文件 sha256 前 12 位
params         obj  {"temperature": 0, ...} 全部生成参数
raw_response   str  API 原始返回文本（不截断）
parsed         obj|null  {"verdict": ..., "reason": ...}；校验失败为 null
latency_ms     int
created_at     str  ISO 8601 UTC
```

红线：任何字段不得包含 API Key；缓存缺失时 `llm_fail_reason=no_cache`，禁止编造 verdict。

## 6. 变更规则

1. 修改本文件必须三人之一发起、其余两人明确同意，提交信息用 `docs(schema : vN 变更说明)`。
2. 第 4/5 节结构变更时，旧 `llm_cache.jsonl` 中 `prompt_template_hash` 或参数不一致的条目自动失效，不得冒充新结果。
3. 各模块加载文件时必须校验表头/字段与本契约一致（列名、顺序、数量），不一致直接报错，不静默兼容。
