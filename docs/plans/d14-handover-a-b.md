# D14 换机重放交接文档（组员 A / 组员 B）

> 用途：D14 只剩“换机重放”两项未完成。本文说明组员 A、B 各自要在自己的机器上做什么、怎么留证、怎么回填记录。
> 权威依据：`docs/specs/project-roadmap.md` D14、`report/D14_演示与换机验证记录.md` 第 4 节与第 5 节。
> 原则：只做离线重放，不改数据、不改阈值、不改提示模板、不提交、不推送。

## 1. 共同目标

在各自另一台机器上，用完整项目目录重跑一次冻结后的离线流水线，证明结果不依赖开发机路径、缓存状态或网络环境，然后把结果填进记录表 2 的对应列。

- 组员 A：填写记录表 2 的“组员 A”列
- 组员 B：填写记录表 2 的“组员 B”列

两人步骤完全相同，只是机器和填写列不同。

## 2. 开始前：拿到完整项目目录

不要只 `git clone`。仓库不含 `data/dns_log.csv` 等数据文件，克隆后无法从特征阶段运行。

必须拿到完整项目目录（整包拷贝或课程数据包），并确认以下文件存在：

```powershell
cd '你的项目目录'
'data/dns_log.csv','data/splits.csv','results/features.csv','results/baseline.json','results/llm_cache.jsonl','results/run_manifest.json','results/metrics.json' |
    ForEach-Object { "{0}  {1}" -f (Test-Path $_), $_ }
```

全部应为 `True`。任一为 `False` 就先找组长补文件，不要自行重建数据。

## 3. 环境准备

要求 Python 3.11.x，Conda 环境名 `dns-dga`。

如果本机已有该环境：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3'
$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
& $python --version
```

如果本机没有，按项目根目录的 `environment.yml` 创建；Miniconda 可装在自己的用户目录，不需要管理员权限：

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3'
$conda = Join-Path $installRoot 'Scripts\conda.exe'
& $conda env create --file environment.yml
```

已有环境但依赖不全时：

```powershell
& $python -m pip install -r requirements.txt
& $python -m pip check
```

`$installRoot` 按自己机器实际安装位置调整；不确定时用 `where.exe python` 或直接激活 `conda activate dns-dga` 后用 `python`。

## 4. 校验数据哈希

用 `results/run_manifest.json` 里的 `input_sha256` 校验本机输入是否与开发机一致：

```powershell
$manifest = Get-Content results\run_manifest.json -Raw | ConvertFrom-Json
foreach ($item in $manifest.input_sha256.PSObject.Properties) {
    $path = $item.Name
    if (Test-Path $path) {
        $hash = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower()
        if ($hash -eq $item.Value) { "OK        $path" } else { "MISMATCH  $path" }
    } else {
        "MISSING   $path"
    }
}
```

应全部为 `OK`。出现 `MISMATCH` 说明项目包被改动过，先停下并反馈，不要继续跑，也不要改 `run_manifest.json`。

## 5. 执行离线重放

在项目根目录执行，并同时留证：

```powershell
$sw = [Diagnostics.Stopwatch]::StartNew()
& $python run_all.py --from features --offline 2>&1 |
    Tee-Object -FilePath "report\evidence\d14_replay_$env:COMPUTERNAME.txt"
$code = $LASTEXITCODE
$sw.Stop()
"exit=$code elapsed_sec=$([math]::Round($sw.Elapsed.TotalSeconds,1))"
```

注意：

- 不要加 `--online`，不要设置 `ZHIPU_API_KEY`。
- 运行前可以复制一份 `results\` 备份；运行会覆盖 `results\metrics.json`、`results\run_manifest.json` 与图表。
- 耗时以本机实测为准，冷启动慢一些正常；判定看退出码和指标，不看秒数。
- 输出有中文时若出现乱码，先执行 `chcp 65001` 再运行。

## 6. 读取结果并与基准比对

```powershell
$m = Get-Content results\metrics.json -Raw | ConvertFrom-Json
"rule   fp=$($m.rule.test.fp) fn=$($m.rule.test.fn) P=$($m.rule.test.precision) R=$($m.rule.test.recall)"
"hybrid fp=$($m.hybrid.test.fp) fn=$($m.hybrid.test.fn) P=$($m.hybrid.test.precision) R=$($m.hybrid.test.recall)"
"subset val=$($m.subset_assertions.val) test=$($m.subset_assertions.test)"

$r = Get-Content results/run_manifest.json -Raw | ConvertFrom-Json
"cache_hits=$($r.llm.cache_hits) requested_unique=$($r.llm.requested_unique) mode=$($r.mode)"
```

基准值（必须完全一致）：

| 项目 | 基准 |
|---|---|
| test 规则 | P=47.50%、R=13.87%、FP=21、FN=118 |
| test 混合 | P=54.29%、R=13.87%、FP=16、FN=118 |
| 正确撤回 / 错误放行 | 5 / 0 |
| 缓存命中 | 87 / 87，覆盖 100% |
| `subset_assertions` | val、test 均为 passed |
| 退出码 | 0 |

一致就记为通过；不一致时保留完整输出，不要改数据或阈值来“凑”结果，并立即反馈。

## 7. 回填记录表 2

打开 `report/D14_演示与换机验证记录.md` 第 4 节，只填自己那一列：

| 字段 | 怎么填 |
|---|---|
| 日期时间 | 实际执行时间，注明时区 |
| 执行人 / 机器名 | 姓名与 `$env:COMPUTERNAME` |
| 操作系统 / Python 版本 | `[Environment]::OSVersion.VersionString` 与 `$python --version` |
| 项目包来源 | 整包拷贝 / 课程数据包，写清来源 |
| 数据哈希是否与 manifest 一致 | 第 4 节全部 OK 才写“一致” |
| 耗时 / 退出码 | 第 5 节打印的 `elapsed_sec` 与 `exit` |
| `cache_hits` / `requested_unique` | 第 6 节读到的值，预期 87 / 87 |
| test 规则 P / R | 第 6 节读到的值 |
| test 混合 P / R | 第 6 节读到的值 |
| `subset_assertions` | val、test 是否 passed |
| 异常与处理 | 没有就写“无”；有就写现象与处理，不要隐瞒 |
| 结论 | 一致且退出码 0 写“通过”，否则如实写不通过及原因 |

同时把自己的截图放进 `report/evidence/`，命名建议：

- `d14_replay_<机器名>_window.png`：运行窗口与结果
- `d14_replay_<机器名>_hash.png`：哈希校验全 OK 的窗口

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `FileNotFoundError: data/dns_log.csv` | 只克隆了仓库，改用完整项目目录 |
| `ModuleNotFoundError: tldextract` 等 | 未装依赖，执行 `pip install -r requirements.txt` |
| 哈希 `MISMATCH` | 项目包被改动或拷贝不完整，重新拿包，不要手动改清单 |
| 指标与基准不同 | 数据或缓存不是冻结版本；保留输出并反馈，禁止改阈值/改标签 |
| 缓存命中小于 87 | `results/llm_cache.jsonl` 缺失或被改动；不要在线补，反馈组长 |
| 中文乱码 | 先 `chcp 65001`，或设置 `$env:PYTHONIOENCODING='utf-8'` |
| 路径含空格或中文报错 | PowerShell 中用引号包裹路径，或把项目放到纯英文路径再试 |

## 9. 交付物与红线

交付给组长：

1. 记录表 2 自己那一列已填写的 `report/D14_演示与换机验证记录.md`
2. 本机运行日志 `report/evidence/d14_replay_<机器名>.txt`
3. 运行窗口与哈希校验截图各一张

红线：

- 不使用 `--online`，不发起 LLM 请求，不读取 API Key
- 不修改 `data/`、`config.json`、`results/baseline.json`、提示模板与缓存
- 不把测试集结果用于调参
- 未经组长同意不提交、不推送
