import json
from pathlib import Path


def as_percent(value):
    return "无数据" if value is None else f"{value:.2%}"


def main():
    root = Path(__file__).resolve().parents[1]
    metrics = json.loads((root / "results" / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "results" / "run_manifest.json").read_text(encoding="utf-8"))
    stages = {stage["name"]: stage for stage in manifest["stages"]}
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
