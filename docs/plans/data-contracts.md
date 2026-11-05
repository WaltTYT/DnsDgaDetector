# 数据契约（SCHEMA）v2

> 状态：**v2，项目维护者维护**。本契约定义五类共享数据产物；无需逐个组员审批。兼容性破坏变更时升级契约版本，并同步更新生产者、消费者与测试。
> v2 变更（2026-11-05）：`source` 枚举新增 `hard_negative`，把难负样本从 `normal_list` 中独立出来，供 D10/D12 分组统计；CSV 表头与列序不变，各模块的 `source` 枚举校验须同步。
> 依据：[项目路线图](../specs/project-roadmap.md) 第 2.2、2.4、2.5、7.2 节。
> 通用规则：所有时间戳为 UTC，ISO 8601 格式 `YYYY-MM-DDTHH:MM:SS.mmmZ`（毫秒精度，DGA 高频场景需要）；域名一律规范化（小写、去末尾根点、IDNA 编码 ASCII）；CSV 用 UTF-8 无 BOM，逗号分隔，禁止字段内出现未转义逗号。

## 1. data/dns_log.csv — 原始查询日志（组员A 写，组员B/组长 读）

规范表头（列名与顺序固定）：

```text
event_id,timestamp,client_ip,qname,qtype,label,source,algorithm,split
```

| 列 | 类型 | 取值/格式 | 说明 |
|---|---|---|---|
| event_id | int | 从 1 递增，全文件唯一 | 排序键，与 timestamp 共同定序 |
| timestamp | str | ISO 8601 UTC 毫秒 | 单调不减 |
| client_ip | str | IPv4 点分十进制 | 来自固定客户端池（池大小与分布见生成配置） |
| qname | str | 规范化域名 | 特征与复核的唯一输入来源 |
| qtype | str | `A` / `AAAA` / `CNAME` / `TXT` / `NS` | 主场景以 A 为主 |
| label | int | `0`=正常 `1`=DGA | **元数据**，禁止进入特征与 LLM 输入 |
| source | str | `normal_list` / `hard_negative` / `public_dga` / `manual_dga` | **元数据**；`hard_negative` 表示正常集合中的难负样本（`label=0`），仅用于分组统计 |
| algorithm | str | `rand_alpha` / `word_digit` / `md5_date` 仅用于 `source=manual_dga`；其他来源留空 | **元数据**；样本类别由 `label` 与 `source` 共同确定 |
| split | str | `train` / `val` / `test` | **元数据**，禁止跨集合读取 |

## 2. data/splits.csv — 域名划分（组员A 写，全员读）

规范表头（列名与顺序固定）：`domain,label,source,algorithm,split`。`source` 取上表四个枚举值；`hard_negative` 与正常榜单同为 `label=0`、`algorithm` 留空，仅用于分组统计。`algorithm` 仅对 `source=manual_dga` 使用三种项目手工生成算法 ID，其他来源留空；样本类别由 `label` 与 `source` 共同确定。其余语义同上；一行一个唯一规范化域名；同一可注册域（eTLD+1）的所有行 split 必须一致。

## 3. data/dga_domains.csv — 手工 DGA 生成记录（组员A 写）

规范表头（列名与顺序固定）：`domain,algorithm,seed,params`。`seed` 为可序列化的非空字符串；`params` 为合法 JSON 对象字符串（如 `{"len":12,"tld":".com"}`）；同 algorithm+seed+params 必须可重生成同一域名。

## 4. results/features.csv — 聚合样本表（组员B 写特征列，组长追加结论列）

一行 = 一个 `(client_ip, window_start, qname)` 聚合样本。

规范表头（列名与顺序固定）：

```text
client_ip,window_start,qname,label,source,algorithm,split,H,F,I,E,B,R,llm_verdict,llm_fail_reason,hybrid
```

| 列 | 类型 | 取值/格式 | 写方 |
|---|---|---|---|
| client_ip, window_start, qname | — | 聚合键；window_start 为 UTC 整分钟 `YYYY-MM-DDTHH:MM:00Z` | B |
| label, source, algorithm, split | — | 同 dns_log 语义，由域名真值继承；`source` 取四个枚举值，`hard_negative` 与正常榜单同为 `label=0`；`algorithm` 仅对 `source=manual_dga` 使用项目生成算法 ID，其他来源留空；样本类别由 `label` 与 `source` 共同确定；同键冲突即报错。仅作评估元数据，不进入特征向量或 LLM 输入 | B |
| H | float | 香农熵（bit/字符），保留 4 位小数 | B |
| F | int | 该客户端该窗口总查询次数 | B |
| I | float 或空 | 窗口内相邻查询间隔中位数（秒，3 位小数）；查询<3 次留空 | B |
| E, B, R | int | 规则命中标志 `0/1`，`R = E OR B` | B |
| llm_verdict | str | `na`（未送审）/ `DGA` / `normal` / `uncertain` | 组长 |
| llm_fail_reason | str | 空 / `timeout` / `network_error` / `auth_error` / `model_or_endpoint_error` / `rate_limit` / `server_error` / `http_error` / `format_error` / `no_api_key` / `credential_error` / `config_error` / `budget_exhausted` / `no_cache`（非空即按 uncertain 参与判定并单列统计） | 组长 |
| hybrid | int | 最终结论 `0/1`：`hybrid = B OR (E AND llm_verdict != normal)` | 组长 |

`llm_verdict=na` 表示样本未送审；有效响应返回 `DGA` / `normal` / `uncertain`。模型有效返回 `uncertain` 时 `llm_fail_reason` 留空；调用失败或缓存缺失时 `llm_verdict=uncertain` 且填写对应原因。无须送审的样本 `llm_fail_reason` 留空。

## 5. results/llm_cache.jsonl — LLM 调用缓存（组长写，评估读）

UTF-8 编码，每行恰好一个独立 JSON 对象；每个 `cache_key` 只保留一条已成功且通过格式校验的缓存记录。对象键顺序不具语义，以下字段必须全部出现且不得出现未定义字段：

```
cache_key      str  64 位小写十六进制 SHA-256；输入为下面五项组成的规范 JSON 字节串：input、model、endpoint、prompt_template_hash、params
input          obj  {"qname": ..., "registrable_label": ...}   ← 仅此两项，无真值
model          str  如 GLM-4-Flash
endpoint       str  HTTPS 接口标识，只含 scheme、host、path；不得含 userinfo、query 或 fragment
prompt_template_hash  str  提示模板文件完整 SHA-256（64 位小写十六进制）
params         obj  {"temperature": 0, ...} 全部生成参数
attempt_count  int  成功前实际 HTTP 请求次数（含重试），至少 1
http_status    int  成功响应的 2xx 状态码
raw_response   str  最终 2xx API 原始响应正文；不含请求头或凭证
parsed         obj  已校验的 {"verdict": "DGA|normal|uncertain", "reason": ...}
latency_ms     int  从首次请求到成功结果的总耗时（毫秒，含重试等待）
created_at     str  ISO 8601 UTC
```

`cache_key` 的输入对象使用 UTF-8 JSON 序列化：`ensure_ascii=False`、键按字典序排序、分隔符为 `(',', ':')`，再对序列化字节计算 SHA-256。只缓存成功且 `parsed` 通过校验的记录；API、网络或格式失败不写入缓存，失败原因记录在特征/指标结果中，后续运行允许重试。缓存缺失时特征表记录 `llm_fail_reason=no_cache`，禁止编造 verdict。

红线：任何字段不得包含 API Key、Authorization 头或真值/生成来源元数据；`input` 只能包含域名判断所需字段。

## 6. 变更规则

1. 项目维护者负责本契约的增补与版本管理；兼容性破坏变更必须升级主版本，并同步更新生产者、消费者、测试和提交说明（`docs(schema : vN 变更说明)`）。无需逐个组员审批。
2. 第 4/5 节结构变更时，旧 `llm_cache.jsonl` 中 `prompt_template_hash` 或参数不一致的条目自动失效，不得冒充新结果。
3. 各模块加载文件时必须校验表头/字段与本契约一致（CSV 列名、顺序、数量；JSONL 必需字段集合和类型），不一致直接报错，不静默兼容。
4. 版本记录：v1（2026-11-01）建立五类产物契约；v2（2026-11-05）`source` 枚举新增 `hard_negative`。
