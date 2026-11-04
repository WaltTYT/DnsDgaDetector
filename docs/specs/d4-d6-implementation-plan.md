# D4–D6 实施计划

> 状态：执行中
> 日期：2026-11-04
> 范围：D4（dns_demo.py）、D5（features.py + baseline.py）、D6（rule_engine.py）及对应测试
> 前置依赖：[D4-D6 设计文档](../plans/d4-d6-design.md)、[数据契约 v1](../plans/data-contracts.md)、[项目路线图](project-roadmap.md) 阶段 2

---

## 1. 任务拆解

| # | 模块 | 文件 | 测试 | 核心交付 |
|---|---|---|---|---|
| 1 | D4 DNS 查询演示 | `src/dns_demo.py` | 内嵌 dry-run 测试 | 完整报文解码输出，stub/递归区别说明 |
| 2 | D5 特征计算 | `src/features.py` | `tests/test_features.py`（补全） | 熵 H、频率 F、间隔 I，窗口对齐，非法标签处理 |
| 3 | D5 基线拟合 | `src/baseline.py` | 同 test_features.py | 只拟合 train+label=0，输出 baseline.json + 分布图 |
| 4 | D6 规则引擎 | `src/rule_engine.py` | `tests/test_rule_engine.py`（新增） | E/B/R，缺失间隔跳过，验证集候选比较 |

## 2. 实施顺序

```
步骤 1: dns_demo.py    （独立，先写好便于手动验证）
步骤 2: features.py    （核心特征，baseline 和 rule_engine 都依赖）
步骤 3: baseline.py    （依赖 features.py 的特征计算）
步骤 4: rule_engine.py （依赖 baseline.py 的阈值）
步骤 5: test_features.py 补全
步骤 6: test_rule_engine.py 新增
步骤 7: 完整 pytest 运行 + 回归检查
```

## 3. 接口契约

### 3.1 features.py

```python
def normalize_qname(qname: str) -> str:
    """规范化：小写、去末尾根点"""

def extract_label(qname: str) -> tuple[str, str]:
    """
    用 tldextract 离线提取可注册域标签。
    返回 (label, status)；status: ok / ip_literal / punycode / no_domain
    """

def shannon_entropy(s: str) -> float:
    """计算香农熵，保留 4 位小数；空串返回 NaN"""

def align_to_window_start(ts_iso: str) -> str:
    """UTC ISO 时间戳对齐到整分钟窗口起点"""

def compute_features(log_df: pd.DataFrame) -> pd.DataFrame:
    """
    输入：dns_log.csv 格式 DataFrame
    输出：features 聚合 DataFrame，键 (client_ip, window_start, qname)
    列：client_ip, window_start, qname, label, source, algorithm, split,
        H, F, I（规则列 E/B/R 留空给 rule_engine）
    """
```

### 3.2 baseline.py

```python
def fit_baseline(features_df: pd.DataFrame,
                 H_quantile: float = 0.99,
                 F_quantile: float = 0.99,
                 I_quantile: float = 0.01) -> dict:
    """
    只在 split=train AND label=0 样本上拟合阈值。
    返回 baseline dict（同设计文档 2.3.4 JSON schema）。
    """

def save_baseline(baseline: dict, path: str) -> None:
    """写 baseline.json"""

def load_baseline(path: str) -> dict:
    """读 baseline.json 并校验 schema"""

def plot_baseline_histograms(features_df: pd.DataFrame,
                              baseline: dict,
                              output_dir: str) -> None:
    """画 H/F/I 直方图 + 阈值竖线"""
```

### 3.3 rule_engine.py

```python
def compute_rules(features_df: pd.DataFrame, baseline: dict) -> pd.DataFrame:
    """
    填 E/B/R 列。
    E = H > threshold
    B = (F > threshold) OR (I < threshold AND I 非缺失)
    R = E OR B
    """

def evaluate_candidates(features_df: pd.DataFrame,
                        config: dict) -> list[dict]:
    """
    只在 split=val 上比较 config.json.threshold_candidates 的
    笛卡尔积组合；返回每组的混淆矩阵和指标。
    """

def summarize_client_contamination(features_df: pd.DataFrame) -> dict:
    """
    统计 label=0 AND B=1 的连带告警数，按 split 分组。
    """
```

## 4. 数据契约检查点

- [ ] features.csv 列名和顺序与 data-contracts.md v1 第 4 节完全一致
- [ ] baseline.json schema 与设计文档 2.3.4 一致
- [ ] dns_demo.py 输出不写入任何受限数据文件
- [ ] baseline 不触碰 split=val/test 或 label=1
- [ ] rule_engine 的 E/B/R 只由 H/F/I 和阈值决定，不读取 label/source/algorithm/split

## 5. 验收

- 所有单元测试通过（pytest tests/ -v）
- 设计文档 §7 交付检查清单全部勾选
- 自检清单（AGENTS.md §10）全部通过
