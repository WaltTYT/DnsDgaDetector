# D1 设计：环境、模型连通性与数据契约

> 状态：设计评审已通过，D1 实施中。
> 日期：2026-11-01
> 范围：阶段 1 的 D1；不包含 D2 数据准备和 D3 日志生成。
> 当前进度：五类数据契约 v1 已定稿；GLM-4-Flash 真实 JSON 冒烟已通过（HTTP 200，耗时 2514 ms），脱敏证据见 `report/evidence/d1_glm_smoke.json`。

## 1. 目标

D1 为后续开发建立可复建的 Python 环境，确认 GLM-4-Flash 的真实 API 连通性与 JSON 响应能力，落实密钥和配置边界，并制定五类共享数据产物的 v1 契约。D1 只验证开发前置条件，不实现 DNS 检测流水线或 LLM 正式复核逻辑。

完成后应能回答：

- 环境能否从仓库配置创建，直接依赖版本是否与锁定文件一致？
- 当前账号能否访问配置的 GLM-4-Flash 模型并得到可解析的 JSON？
- 密钥是否只通过环境变量或被忽略的本地文件提供，且不会进入日志、结果或 Git？
- 五类共享数据文件的字段、格式和语义是否定义完整且互相一致？

## 2. 当前基线与差异

本节记录 D1 设计起草时（2026-11-01）对 worktree 的初始检查和拟处理差异；实际完成状态以本文“当前进度”和实施记录为准。

| 项目 | 当前仓库状态 | D1 处理 |
|---|---|---|
| Python 环境定义 | `environment.yml` 固定 Python 3.11.17，并通过 pip 安装 `requirements.txt` | 保持单一依赖来源；创建或复用环境后核对解释器与包版本 |
| 直接依赖 | `requirements.txt` 已固定 dnspython、requests、tldextract、numpy、pandas、matplotlib、pytest 的版本 | 不在 D1 无依据升级；只有兼容性实测失败且项目维护者批准时才调整 |
| GLM 配置 | `config.json` 已设模型名 `GLM-4-Flash`、30 秒超时、最多 3 次重试和 temperature 0；`base_url` 为空 | 用实施时官方接口说明核对端点后填写；冒烟调用本身只发一次请求，不套用正式流程的重试 |
| 密钥读取 | `src/llm_judge.py` 已提供 `get_api_key()`，优先读取 `ZHIPU_API_KEY`，再回退根目录 `api.txt` | 冒烟工具复用该函数，不另行读取密钥文件 |
| 密钥忽略 | `.gitignore` 已忽略 `api.txt`，`git check-ignore` 可匹配 | 保留现有规则；验证时只检查忽略状态，不读取或打印密钥内容 |
| 本机 Conda | 设计开始时 PATH 和既有候选路径未找到 Conda；指定环境解释器也不存在 | 已在当前用户目录 `%LOCALAPPDATA%\Miniconda3` 安装 Miniconda，并在其默认环境目录创建 `dns-dga`；其他组员可按各自本机安装位置调整命令 |
| API 凭证 | 设计起草时当前会话未设置 `ZHIPU_API_KEY`，worktree 根目录没有 `api.txt` | 后续已配置为本机当前 Windows 用户的持久环境变量，并完成真实 JSON 冒烟；凭证不存仓库 |
| 契约草案 | 设计开始时 `docs/plans/data-contracts.md` 尚未定稿 | 明确五类数据产物的字段契约并发布 v1，由项目维护者管理版本 |

README 目前记载环境已创建并验证，但本次会话无法在计划指定位置找到该环境。D1 执行时应以当前本机实际解释器和依赖检查结果为准，再修正 README 的环境状态与命令，不能把旧描述当作本机验证证据。

## 3. 设计决策

### 3.1 环境与依赖

- `environment.yml` 负责 Conda 环境名、Python 3.11.17 和 pip 引导；`requirements.txt` 是直接 Python 依赖及其精确版本的唯一清单。Conda 环境创建通过该文件安装依赖，不维护第二份包版本列表。
- 当前机器的 Miniconda 安装到 `%LOCALAPPDATA%\Miniconda3`，项目环境使用 `dns-dga` 并置于 Miniconda 默认环境目录。若目标环境已经存在，先检查而不删除或重建；本次安装和环境创建均在当前用户可写目录内，不需要管理员权限。其他组员可使用各自合适的本地路径。
- Conda 与 pip 缓存放在当前用户 Miniconda 目录下的本地缓存中。缓存、环境目录和凭证不提交到仓库。
- 环境验收检查 Python 版本、七项直接依赖的已安装版本及 `python -m pip check`。若锁定版本不可解析或互相冲突，先记录完整诊断，再讨论变更，不通过无记录的升级绕过问题。

### 3.2 模型冒烟验证

评审决定将冒烟命令并入 `src/llm_judge.py`，从项目根目录使用 `python -m src.llm_judge --smoke` 调用。它复用已有的 `get_api_key()`，但与 D7-D9 的正式判断、候选筛选、重试、缓存和改判保持分离。

冒烟工具的职责限定为：

1. 从 `config.json` 读取模型名、官方接口地址、超时和生成参数。官方 HTTP API 文档给出的接口为 `https://open.bigmodel.cn/api/paas/v4/chat/completions`，使用 Bearer Token 和 JSON 请求体（[官方文档](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)，核对日期：2026-11-01）。
2. 通过 `src.llm_judge.get_api_key()` 取得凭证；缺少凭证时在发送请求前失败。
3. 使用已安装的 `requests` 向配置端点发送一个最小 HTTPS JSON POST。凭证只放在 Bearer Authorization 请求头，不放入 URL 或请求体；请求体包含配置的模型名、消息、生成参数及 `response_format={"type":"json_object"}`。该服务端 JSON mode 已通过真实请求验证；本地仍严格解析响应 JSON。
4. 验证 HTTP 状态、响应中模型标识及正文 JSON；不把“收到 HTTP 200”单独算作 JSON 冒烟成功。
5. 只执行一次请求，不自动重试。认证、模型不存在、限流和服务端错误分别记录，修正配置或凭证后由操作者重新运行。

从项目根目录以 `python -m src.llm_judge --smoke` 运行。成功或失败结果写入 `report/evidence/d1_glm_smoke.json`，仅保存时间、模型名、接口标识（不含凭证或查询参数）、HTTP 状态、耗时、可用的请求 ID/usage、JSON 校验结果和错误类别。不得保存 Authorization 头、API Key 或完整请求/响应调试转储。缺少 Key 时在发送请求前失败并标为 `no_api_key`；该文件是连通性证据，不作为 DGA 判断样本或模型准确率数据。

官方模型文档将 GLM-4-Flash 描述为免费模型，但免费政策、额度及限流仍以执行当天的官方说明和账号实测为准，不在报告中预设固定额度。`config.json` 的 `base_url` 按上述官方 HTTP API 文档填写；2026-11-01 的真实请求已验证当时账号、模型 ID 和端点可用。

### 3.3 配置与密钥

- `config.json` 仅保存非敏感参数。模型名、端点、超时、重试数、temperature 和预算均不得包含凭证。
- 环境变量 `ZHIPU_API_KEY` 优先；仅在本地需要时才创建根目录 `api.txt` 作为回退。不得要求组员将真实 Key 写入仓库文件、命令行参数、截图或共享记录。
- 检查密钥安全时只验证环境变量是否设置、`api.txt` 是否被 Git 忽略以及 Git diff 中没有密钥；不输出变量值，也不打开 `api.txt`。
- D1 冒烟的单次请求策略与后续正式调用策略分开。超时/限流的指数退避最多 3 次属于 D8，不在 D1 冒烟中提前实现。

### 3.4 数据契约 v1

`docs/plans/data-contracts.md` 定义五类共享数据产物：`dns_log.csv`、`splits.csv`、`dga_domains.csv`、`features.csv` 和 `llm_cache.jsonl`。CSV 契约明确固定表头和列顺序；JSONL 契约明确必需字段和缓存键算法；调用失败类别由 `features.csv.llm_fail_reason` 记录。标签及生成来源等元数据只用于评估，不进入特征计算或 LLM 输入。

本契约由项目维护者制定和维护，不设置逐个组员审批门槛。兼容性破坏变更时升级契约版本，并同步更新生产者、消费者和测试；每个模块仍须校验输入表头或 JSONL 必需字段，不得静默接收不兼容数据。

## 4. D1 交付物与职责

| 交付物 | 位置 | 验收内容 |
|---|---|---|
| Conda 环境定义 | `environment.yml`、`requirements.txt` | 环境能创建或已存在；版本与清单一致；pip 依赖检查通过 |
| GLM 冒烟入口 | `src/llm_judge.py` | `python -m src.llm_judge --smoke` 复用统一密钥读取；请求一次；校验模型和 JSON；失败时不泄露凭证 |
| 模型调用证据 | `report/evidence/d1_glm_smoke.json` | 记录真实调用元数据及结果；没有 Key；说明免费/限流实测情况 |
| 初始运行配置 | `config.json` | 端点经官方说明核验；仅有非敏感参数；模型/超时/生成参数可追溯 |
| 数据契约 v1 | `docs/plans/data-contracts.md` | 五类数据产物的字段、顺序、类型、空值和 JSONL 记录格式完整；版本状态准确 |
| 环境说明 | `README.md` | 命令与真实验证状态一致；明确当前用户安装位置、无需管理员权限的创建命令和验证方式 |

环境安装与版本核验由组员 A 负责；数据契约由项目维护者制定和维护；GLM 凭证由账号负责人本地配置，冒烟结果由执行者记录。任何成员都不得把共享账号 Key 写入提交。

## 5. 验收标准

- 当前机器 Miniconda 安装路径为 `%LOCALAPPDATA%\Miniconda3`；`dns-dga` 环境使用 Python 3.11.17，或将有依据且经组员同意的版本差异记录下来。
- 七项直接依赖的版本与 `requirements.txt` 一致，`python -m pip check` 返回成功。
- `api.txt` 被 `.gitignore` 匹配；`config.json`、日志、调用记录和 Git diff 中均无 API Key。
- 有效凭证与经核实的模型端点可完成一次真实调用；HTTP 请求成功，模型标识符合配置，正文通过 JSON 解析与 `status == "ok"` 检查。记录调用时间和耗时。
- 请求失败时保存脱敏的失败类别；不得伪造成功记录，也不得将失败冒充模型判断。
- 五类数据契约字段完整、互相一致，JSONL 的缓存键与错误字段可复现；契约以 v1 发布，由项目维护者管理后续版本。
- README 与实际环境状态一致。环境检查不等同于 DNS 抓包、检测流水线或 DGA 评估已经完成。

## 6. 错误处理与安全边界

| 情况 | 处理 |
|---|---|
| Conda 安装或环境创建失败 | 保留安装器校验和、命令输出及错误信息；不静默转用系统 Python，不删除或重建已有环境 |
| Key 缺失 | 在请求前失败，提示本地配置方式；不发起匿名请求 |
| 401/403 | 记录认证失败，停止自动重试并由账号负责人检查 Key/权限 |
| 404 或模型无效 | 核对官方接口文档和模型 ID 后再运行 |
| 429、网络超时或 5xx | 本次冒烟只记录失败，不循环重试；正式调用的有限重试留待 D8 |
| 响应非 JSON 或字段不符 | 记为格式校验失败；不判为成功，也不把响应输入检测评估 |
| 契约字段或模块接口存在歧义 | 由项目维护者确定唯一字段语义并更新契约版本；同步更新生产者、消费者和测试 |

## 7. 明确不在 D1 范围内

- 下载公开正常域名、公开 DGA 数据集或 PSL；这些属于 D2。
- 生成手工 DGA 域名、执行数据划分或构造 DNS 查询日志；分别按 D2/D3 计划推进。
- 实现 LLM 候选筛选、缓存、限次重试、语义改判和最终检测流水线；这些属于后续阶段。
- 进行真实 DNS 抓包、规则效果比较、FPR/FNR 统计或声称系统具备实时检测能力。
