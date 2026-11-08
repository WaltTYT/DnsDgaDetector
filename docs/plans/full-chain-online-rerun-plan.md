# 全链路联网复现验证执行计划（保留缓存备份版）

- status: 待执行
- owner: 组长
- date: 2026-11-08
- 说明: 按项目文档约定，执行计划通常放 `docs/specs/`；本文件按用户要求存放于 `docs/plans/`。
- 依据: docs/specs/project-roadmap.md（D7 冻结点、2.5 调用预算与缓存）、AGENTS.md 2.2

## 1. 目标

在不丢失当前 LLM 缓存与冻结状态的前提下，验证"数据准备 → 日志生成 → 特征 → 基线拟合 → 规则 → LLM 联网复核 → 评估"全链路可从头复现，并与现有产物对比。

## 2. 前置条件

- [ ] 网络可达 `https://open.bigmodel.cn`
- [ ] API Key 就绪：环境变量 `ZHIPU_API_KEY` 或根目录 `api.txt`（已 gitignore，密钥不落任何产物）
- [ ] 确认本次解冻理由并记录（D7 后改动需说明）：验证全链路可复现性，非调整参数
- [ ] 预算确认：本次预计联网请求约 90 次（远低于 800 上限）

## 3. 执行步骤

### Step 1 备份现有产物

```powershell
mkdir results\pre_rerun_backup
Copy-Item results\llm_cache.jsonl, results\baseline.json, results\run_manifest.json, results\metrics.json, results\features.csv results\pre_rerun_backup\
```

记录备份时 `data/splits.csv`、`data/dns_log.csv` 的 SHA-256（对照 manifest `input_sha256`），用于重跑后比对。

### Step 2 解除冻结

```powershell
$m = Get-Content results\run_manifest.json -Raw | ConvertFrom-Json
$m.freeze_check = "unfrozen"
$m | ConvertTo-Json -Depth 100 | Set-Content results\run_manifest.json -Encoding UTF8
```

### Step 3 清空 LLM 缓存（触发真实联网请求）

```powershell
Remove-Item results\llm_cache.jsonl
```

### Step 4 全链路在线运行

```powershell
conda activate dns-dga
python run_all.py --from data --online 2>&1 | Tee-Object results\d13_full_rerun_online.log
```

### Step 5 离线回放一致性验证

```powershell
python run_all.py --from features --offline 2>&1 | Tee-Object results\d13_offline_replay.log
```

两次运行的 metrics 应完全一致（LLM 全走新缓存）。

## 4. 对比检查点

| 检查项 | 预期 | 判定 |
|---|---|---|
| `data/splits.csv` SHA-256 | 与备份记录一致 | 不一致 → 数据准备不可复现，需排查 |
| `data/dns_log.csv` SHA-256 | 与备份记录一致 | 同上 |
| `results/features.csv`（重算部分） | 行数与 H/F/I 列一致 | 不一致 → 特征计算漂移 |
| `baseline.json` 阈值 | 与重拟合结果一致；**可能不同于当前冻结值**（d12_full_run.log 显示历史阈值 H=3.5369 与冻结值 3.5466 存在数据版本差异，属已知现象） | 差异需归因到输入数据版本，而非代码 |
| LLM verdict 分布 | 与备份缓存的 DGA/normal/uncertain 计数接近 | 少量差异可接受（temperature=0 但服务端非严格确定） |
| `metrics.json` 规则口径指标 | 与备份一致 | 不一致 → 规则/评估环节有问题 |
| `metrics.json` 混合口径指标 | 接近备份 | 差异大 → 记录并归因 LLM 输出波动 |

## 5. 收尾

- [ ] 恢复冻结：把 `freeze_check` 改回 `"frozen"`，并同步 `thresholds`、`input_sha256` 为新一次拟合结果（若决定采用新基线），或整体还原备份（若决定保留旧基线）
- [ ] 保留 `results/pre_rerun_backup/` 至验证结论写入报告后再清理（清理需用户确认）
- [ ] 在 report/实习报告.md 素材区记录：运行时间、两次回放一致性结论、verdict 差异清单

## 6. 回滚方案

任何步骤失败或结论为"不可复现"时：

```powershell
Copy-Item results\pre_rerun_backup\* results\ -Force
```

即可恢复到执行前状态（manifest 含 frozen 标记与旧哈希）。

## 7. 风险与红线自查

- 基线拟合仅读 split=train + label=0，解冻重拟合不触碰 test，符合数据隔离红线
- 全程不得将 Key 写入 log、manifest、缓存或报告
- `--online` 运行会覆盖 `run_manifest.json`，备份必须在 Step 1 先行完成
- 若中途断网：offline 模式对缺失缓存按 uncertain 处理，不会崩溃，可稍后补跑 `--online`（已有缓存自动命中，只补缺失项）
