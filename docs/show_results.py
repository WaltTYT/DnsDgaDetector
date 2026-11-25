# 演示脚本：读取 results 下的指标与运行清单，打印规则/混合方案在聚合与唯一域名两种口径下的指标摘要
import json
from pathlib import Path


def as_percent(value):
    # 百分比格式化：None 显示“无数据”，否则保留两位小数
    return "无数据" if value is None else f"{value:.2%}"


def main():
    # 汇总入口：定位项目根目录，读取 metrics.json 与 run_manifest.json，按测试集输出指标与改判统计
    root = Path(__file__).resolve().parents[1]
    metrics = json.loads((root / "results" / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "results" / "run_manifest.json").read_text(encoding="utf-8"))
    # 以阶段名为键索引运行清单，便于取特征行数与规则告警数
    stages = {stage["name"]: stage for stage in manifest["stages"]}
    # 以下四项分别对应：规则/混合的聚合口径与唯一域名口径指标
    rule = metrics["rule"]["test"]
    hybrid = metrics["hybrid"]["test"]
    unique_rule = metrics["unique_domain"]["test"]["rule"]
    unique_hybrid = metrics["unique_domain"]["test"]["hybrid"]
    reversal = metrics["reversal_detail"]["test"]

    print("=" * 62)
    print("DNS DGA 检测演示结果")
    print(f"聚合样本：{stages['features']['rows']:,}    规则告警：{stages['rules']['alerts']}")
    print(
        f"冻结阈值：H={manifest['thresholds']['H']}  "
        f"F={manifest['thresholds']['F']}  I={manifest['thresholds']['I']}"
    )
    print("-" * 62)
    print(
        f"测试集聚合样本  规则：FPR={as_percent(rule['fpr'])}  "
        f"精确率={as_percent(rule['precision'])}  召回率={as_percent(rule['recall'])}"
    )
    print(
        f"测试集聚合样本  混合：FPR={as_percent(hybrid['fpr'])}  "
        f"精确率={as_percent(hybrid['precision'])}  召回率={as_percent(hybrid['recall'])}"
    )
    print(
        f"测试集唯一域名  规则：FPR={as_percent(unique_rule['fpr'])}  "
        f"精确率={as_percent(unique_rule['precision'])}"
    )
    print(
        f"测试集唯一域名  混合：FPR={as_percent(unique_hybrid['fpr'])}  "
        f"精确率={as_percent(unique_hybrid['precision'])}"
    )
    print("-" * 62)
    print(f"混合正确撤回误报：{reversal['correct_withdraw_fp']['count']} 条")
    print(f"混合错误放行告警：{reversal['wrong_release_tp']['count']} 条")
    print(f"LLM 缓存覆盖：{metrics['cache_coverage']['test']['rate']:.0%}")
    print("=" * 62)


if __name__ == "__main__":
    main()
