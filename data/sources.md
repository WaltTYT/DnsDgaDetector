# 数据来源记录（组员A，D2 填写）

每项来源必须记录：名称、下载链接、版本或获取日期、许可、标签依据。

## 正常域名榜单（Majestic Million 等）

### 来源
- **名称**：Mendeley Data《Legitimate domains and DGA categorized morphologically and by families》中的 `family=legit` 子集
- **数据集 DOI**：`10.17632/nhvyvytn2h.1`（Version 1，2025-11-12 发布）
- **URL**：`https://data.mendeley.com/datasets/nhvyvytn2h/1`
- **许可**：数据集页面标注 CC BY 4.0；上游 Majestic Million 及 DGArchive 的再分发权尚未独立确认，派生文件仅在本机使用
- **标签依据**：该子集由作者于 2025-09-28 采集的 Majestic Million 组成，按榜单原始顺序保存；本清单只作为良性近似，不宣称域名绝对安全
- **原始快照**：`data/raw/snapshots/mendeley/dga_legit.csv`，133,777,367 字节，SHA-256 `ad783f5dbef10bf2836d37f107db202c1a5b76f053c45ec394b81bdb42ccca1f`，已被 `.gitignore` 忽略
- **纳入规则**：按文件原始顺序取 `family=legit` 的前 3,000 个唯一规范化域名，不做随机打乱
- **产物**：`data/raw/normal_list__mendeley_majestic.txt`，3,000 行，SHA-256 `09c07c40da92fa47686239d6f2cc5a8d5e59abb02ff4c99222e1149ed4b2c5de`

## 公开 DGA 数据集（优先核验 360netlab/DGA 可用性）

### 来源
- **名称**：Mendeley Data《Legitimate domains and DGA categorized morphologically and by families》
- **数据集 DOI**：`10.17632/nhvyvytn2h.1`（Version 1，2025-11-12 发布）
- **URL**：`https://data.mendeley.com/datasets/nhvyvytn2h/1`
- **许可**：CC BY 4.0；上游 DGArchive 材料再分发权尚未独立确认，派生样本仅在本机使用
- **标签依据**：作者整理自 DGArchive（2025-09-28 至 2025-10-28），按家族与形态标注；共 4,090,661 行，其中 DGA 3,092,348 行、160 个家族、5 种形态
- **原始快照**：同上一节的 `dga_legit.csv`，已在 `.gitignore` 中忽略
- **纳入规则**：排除 `family=legit`；按固定 seed `20261101` 对 160 个家族分配配额（每家族基础 1 条，余量按 `SHA-256(seed|public_dga|family|<family>)` 升序发放），家族内按 `SHA-256(seed|public_dga|<family>|<domain>)` 升序取前若干条；域名去重并排除与正常清单重复项
- **产物**：`data/raw/public_dga__mendeley.csv`，300 行、160 个家族，表头 `domain,family`，SHA-256 `5e65abc8d9fd7fb007aee1f8967edf7834a723f6f631fdde69e7090efe3e18d2`
- **说明**：域名清单不是真实 DNS 查询日志；D3 生成的查询事件为合成行为

## PSL 快照（固定本地文件，离线运行不得联网更新）

### 来源
- **名称**：Public Suffix List 官方快照
- **URL**：`https://publicsuffix.org/list/public_suffix_list.dat`
- **获取日期**：2026-11-06
- **许可**：Mozilla Public License 2.0
- **内容**：包含 PRIVATE 区（`===BEGIN PRIVATE DOMAINS===`），启用私有后缀
- **文件**：`data/raw/public_suffix_list.dat`，338,546 字节，SHA-256 `019fcc1daca33416846853ee1f3b99ea6cc6f03388ecd4ce3e5317039bcfd004`
- **离线要求**：`prepare_data`、`gen_log`、`features` 运行时只读取本快照或库内置快照，不得联网更新

## 手工 DGA 词根表（项目自有）

- **文件**：`data/raw/manual_word_roots.txt`
- **内容**：20 个固定英文词根，UTF-8、每行一个、无空行，匹配 `[a-z][a-z0-9-]{0,19}` 且不以连字符结尾
- **来源**：本项目自建固定词表，不复制第三方词库
- **SHA-256**：`92f5078ce7c4bcbb1f702b1d47c216ab1bc233d47e3f6dd9b622faa9c287cf44`

## 难负样本（CDN 哈希子域、云服务域名、含数字合法域名，约 100 个）

### 来源
- **名称**：OpenDNS Top Domains List（仓库 `opendns/public-domain-lists`）
- **URL**：`https://raw.githubusercontent.com/opendns/public-domain-lists/master/opendns-top-domains.txt`
- **获取日期**：2026-11-05（该列表按周更新，本清单以当日抓取快照为准）
- **许可**：仓库 README 明确说明 "Both lists are in public domain"，可用于课程研究
- **标签依据**：列表按全球递归解析器 1 小时内查询该域名的唯一客户端 IP 数排序，并已剔除被 OpenDNS 标记为恶意/可疑的域名，因此可作为 `label=0` 的良性近似；本清单不表示这些域名绝对安全
- **原始快照**：`opendns-top-domains.txt`，135,644 字节，10,000 行，SHA-256 `aba7a11689d0d46c927f012952af795c85d735b39831dea32236eb22b2fb4044`。原始快照不入库，按上述 URL 重新获取后用该哈希校验

### 纳入规则
- 只从原始榜单第 1–3000 行（按 OpenDNS 查询流行度排序）中选取，保证样本确有真实解析流量、不是凭空构造的域名
- 三类互补，同一域名只出现在一个文件中，合计 100 条，行号互不重复：
  1. **含数字的合法域名**：可注册域左侧第一个标签含至少一个 ASCII 数字，且标签长度 ≤ 5；按（标签长度, 榜单行号）升序取前 60 条
  2. **短标签、随机拼写的合法域名**：标签不含数字、长度为 5–7、香农熵 ≥ 2.5，人工复核排除知名品牌词后取 20 条，用于模拟熵值接近 DGA 的合法广告/统计域名
  3. **CDN/云平台可注册域名**：人工确认属于 CDN、云存储或云托管服务的可注册域名，且在原始榜单中存在，共 20 条
- 排除色情/盗版站点：`h33t.com`、`1337x.org`、`tube8.com` 未纳入
- 所有条目经 `normalize_domain` 规范化校验：100 行、100 个唯一 FQDN、无非法标签、均可由 PSL 解析出可注册域

### 产物
| 文件 | 条数 | SHA-256 |
|---|---:|---|
| `data/raw/hard_negative__opendns_top10k_digits.txt` | 60 | `7e7838d19e9b773d4345fe2385f81dd0f3f0a8d9be5e12f92e1417e657040d6e` |
| `data/raw/hard_negative__opendns_top10k_dga_like.txt` | 20 | `c948f0374af184be3c3753fa727859816cd07c92e4c2e60ce30101ca430dac78` |
| `data/raw/hard_negative__opendns_top10k_cdn_cloud.txt` | 20 | `149e718eef6e1c9055f938cb0563ecd84cae70689d2fc056f61a49883d51aa23` |

三个文件均为 UTF-8、每行一个规范化 FQDN、无表头、字母序稳定排序。`source_id` 由文件名取得（`opendns_top10k_digits`、`opendns_top10k_dga_like`、`opendns_top10k_cdn_cloud`），D2 加载时统一记为 `source=hard_negative`、`label=0`、`algorithm` 为空。

### 逐条依据
行号为该域名在原始 `opendns-top-domains.txt` 中的 1 起始行号（等于榜单排名），可下载原始快照后逐行核对。

**含数字的合法域名**（60 条）

| 域名 | 榜单行号 |
|---|---:|
| `163.com` | 1709 |
| `1688.com` | 1900 |
| `24h.com.vn` | 809 |
| `254a.com` | 212 |
| `2mdn.net` | 18 |
| `2o7.net` | 1048 |
| `3.cn` | 2549 |
| `35go.cn` | 2684 |
| `360.cn` | 1023 |
| `3g.cn` | 624 |
| `3lift.com` | 1483 |
| `4at5.net` | 2394 |
| `4wnet.com` | 1543 |
| `51.la` | 2598 |
| `5min.com` | 757 |
| `6pm.com` | 722 |
| `7eer.net` | 2572 |
| `9gag.com` | 1536 |
| `9gag.tv` | 2099 |
| `a9.com` | 851 |
| `ad360.vn` | 1323 |
| `adk2.co` | 915 |
| `adk2.com` | 1223 |
| `afy11.net` | 173 |
| `bfi0.com` | 2267 |
| `c3tag.com` | 727 |
| `c8.net.ua` | 838 |
| `d41.co` | 511 |
| `dwin1.com` | 1197 |
| `ebdr3.com` | 833 |
| `ed4.net` | 1193 |
| `en25.com` | 1155 |
| `esm1.net` | 2145 |
| `flx1.com` | 1071 |
| `gvt1.com` | 733 |
| `h3q.com` | 1923 |
| `io9.com` | 1364 |
| `irs01.com` | 1516 |
| `lp4.io` | 2919 |
| `m6d.com` | 1590 |
| `m6r.eu` | 1250 |
| `ml314.com` | 673 |
| `p161.net` | 494 |
| `pop6.com` | 1281 |
| `pow7.com` | 1363 |
| `r7.com` | 1965 |
| `r7ls.net` | 2272 |
| `rs6.net` | 523 |
| `ru4.com` | 117 |
| `t4ft.de` | 2883 |
| `tr553.com` | 756 |
| `try9.com` | 1189 |
| `v0cdn.net` | 424 |
| `v2cdn.net` | 1245 |
| `v9.com` | 2583 |
| `w3.org` | 609 |
| `w3i.com` | 1781 |
| `w55c.net` | 74 |
| `yb0t.com` | 1227 |
| `z5x.net` | 531 |

**短标签、随机拼写的合法域名**（20 条）

| 域名 | 榜单行号 |
|---|---:|
| `adkmob.com` | 314 |
| `adsonar.com` | 335 |
| `betrad.com` | 52 |
| `cap-mii.net` | 356 |
| `chango.com` | 130 |
| `dl-rms.com` | 353 |
| `dtscout.com` | 316 |
| `gwallet.com` | 106 |
| `jwpltx.com` | 216 |
| `moatads.com` | 132 |
| `mxptint.net` | 344 |
| `nr-data.net` | 111 |
| `owneriq.net` | 154 |
| `revsci.net` | 35 |
| `rfihub.com` | 64 |
| `rfihub.net` | 197 |
| `rundsp.com` | 194 |
| `tabwpm.us` | 362 |
| `tidaltv.com` | 100 |
| `veruta.com` | 299 |

**CDN/云平台可注册域名**（20 条）

| 域名 | 榜单行号 |
|---|---:|
| `akamai.net` | 76 |
| `akamaihd.net` | 5 |
| `aolcdn.com` | 209 |
| `appspot.com` | 283 |
| `bootstrapcdn.com` | 177 |
| `cdninstagram.com` | 334 |
| `cloudflare.com` | 123 |
| `cloudinary.com` | 1299 |
| `dropboxusercontent.com` | 533 |
| `edgecastcdn.net` | 569 |
| `fastly.net` | 320 |
| `googleusercontent.com` | 15 |
| `grvcdn.com` | 371 |
| `herokuapp.com` | 527 |
| `jwpcdn.com` | 204 |
| `msecnd.net` | 244 |
| `trafficmanager.net` | 487 |
| `twimg.com` | 48 |
| `windowsupdate.com` | 62 |
| `ytimg.com` | 14 |

### 已知限制
- 本次未纳入具体的 CDN 哈希子域实例（如 CloudFront 分配的 `<hash>.cloudfront.net` 一类主机名）：D2 约定不发起 DNS 查询，且没有找到可逐条核验归属且稳定可复现的公开主机名清单，为避免构造不可核查的域名，改用 CDN/云平台的可注册域名覆盖该类别
- 若后续取得带完整主机名的公开名单（例如官方文档中固定引用的示例主机名清单），可按同样格式新增 `hard_negative__<source_id>.txt` 并补充本文件，不影响现有 100 条
- 榜单每周更新，换用新快照时域名集合与行号会变化，须同时更新三个派生文件与本节哈希
