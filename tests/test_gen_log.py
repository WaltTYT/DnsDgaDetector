import csv
from collections import Counter
import hashlib
import ipaddress
import io
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import gen_log
from src.prepare_data import DomainRecord


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
SPLIT_DOMAIN_COUNTS = {"train": 2135, "val": 712, "test": 711}


def make_config():
    # 构造与 config.json 一致的默认日志生成配置（seed=20261101、约 50000 行、DGA 占 5%）
    return {
        "seed": 20261101,
        "window_seconds": 60,
        "log": {
            "total_rows_approx": 50000,
            "dga_ratio": 0.05,
            "simulation_start_utc": "2026-11-01T00:00:00.000Z",
            "split_period_hours": 24,
            "client_pool_size": 100,
            "active_client_fraction": 0.20,
            "active_event_fraction": 0.50,
            "normal_behavior_ratio": {"routine": 0.80, "burst": 0.20},
            "dga_behavior_ratio": {
                "high_frequency": 0.40,
                "low_frequency": 0.35,
                "word_digit": 0.25,
            },
            "mixed_window_fraction": 0.20,
            "qtype_weights": {
                "A": 0.80,
                "AAAA": 0.15,
                "CNAME": 0.03,
                "TXT": 0.01,
                "NS": 0.01,
            },
        },
        "paths": {"psl_snapshot": "data/raw/public_suffix_list.dat"},
    }


def load_config_data(tmp_path, data):
    # 将给定字典写入临时 config.json 后交给 gen_log.load_config，便于验证配置校验规则
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return gen_log.load_config(path)


def test_default_event_quotas_conserve_every_allocation_layer():
    # 验证默认配额逐层守恒：总行 50000、DGA 2500、正常 47500，且 total 等于 normal 加 dga
    quotas = gen_log.allocate_event_quotas(SPLIT_DOMAIN_COUNTS, 50000, 0.05)

    assert {split: quota["total"] for split, quota in quotas.items()} == {
        "train": 30003,
        "val": 10006,
        "test": 9991,
    }
    assert {split: quota["dga"] for split, quota in quotas.items()} == {
        "train": 1500,
        "val": 500,
        "test": 500,
    }
    assert sum(quota["total"] for quota in quotas.values()) == 50000
    assert sum(quota["dga"] for quota in quotas.values()) == 2500
    assert sum(quota["normal"] for quota in quotas.values()) == 47500
    assert all(
        quota["total"] == quota["normal"] + quota["dga"]
        for quota in quotas.values()
    )


def test_largest_remainder_uses_supplied_order_for_equal_remainders():
    # 验证余数相同时按传入 tie_order 决定归属（third 优先），且分配总数不变
    result = gen_log.largest_remainder(
        2,
        {"first": 0.5, "second": 0.25, "third": 0.25},
        ("third", "second", "first"),
    )

    assert result == {"first": 1, "second": 0, "third": 1}
    assert sum(result.values()) == 2


def test_event_quotas_round_half_up_for_global_dga_total():
    # 验证全局 DGA 总量按四舍五入取整：1 行乘 0.5 得 1，并落在 train
    quotas = gen_log.allocate_event_quotas(SPLIT_DOMAIN_COUNTS, 1, 0.5)

    assert sum(quota["dga"] for quota in quotas.values()) == 1
    assert quotas["train"] == {"total": 1, "normal": 0, "dga": 1}


def test_event_quotas_use_exact_domain_count_ties():
    # 验证 total 配额在域名数相同时用精确值打破平局（train 1、val 0、test 2）
    quotas = gen_log.allocate_event_quotas(
        {"train": 1, "val": 1, "test": 7}, 3, 0.05
    )

    assert {split: quota["total"] for split, quota in quotas.items()} == {
        "train": 1,
        "val": 0,
        "test": 2,
    }


def test_event_quotas_use_exact_dga_split_ties():
    # 验证 DGA 总量 3 在三个 split 平局时按精确比较均分（各 1）
    quotas = gen_log.allocate_event_quotas(
        {"train": 2, "val": 1, "test": 3}, 6, 0.5
    )

    assert {split: quota["dga"] for split, quota in quotas.items()} == {
        "train": 1,
        "val": 1,
        "test": 1,
    }


def test_event_quotas_reject_zero_total_rows():
    # 验证总行数为 0 时抛 ValueError
    with pytest.raises(ValueError):
        gen_log.allocate_event_quotas(SPLIT_DOMAIN_COUNTS, 0, 0.05)


@pytest.mark.parametrize(
    "tie_order",
    [("first", "unknown"), ("first", "first", "second")],
)
def test_largest_remainder_rejects_unknown_or_duplicate_tie_order(tie_order):
    # 参数化验证 tie_order 含未知键或重复键时抛 ValueError
    with pytest.raises(ValueError):
        gen_log.largest_remainder(
            1, {"first": 0.5, "second": 0.5}, tie_order
        )


@pytest.mark.parametrize("total", [-1, 1.5, True])
def test_largest_remainder_rejects_negative_or_non_integer_total(total):
    # 参数化验证 total 为负数、小数或布尔值时抛 ValueError
    with pytest.raises(ValueError):
        gen_log.largest_remainder(total, {"first": 1.0}, ("first",))


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), -float("inf")])
def test_largest_remainder_rejects_non_finite_weights(weight):
    # 参数化验证权重为 NaN 或正负无穷时抛 ValueError
    with pytest.raises(ValueError):
        gen_log.largest_remainder(
            1, {"first": weight, "second": 0.5}, ("first", "second")
        )


def test_largest_remainder_rejects_weights_that_do_not_sum_to_one():
    # 验证权重之和不为 1 时抛 ValueError
    with pytest.raises(ValueError):
        gen_log.largest_remainder(
            10, {"first": 0.6, "second": 0.3}, ("first", "second")
        )


@pytest.mark.parametrize("total", [-1, 1.5, True])
def test_event_quotas_reject_negative_or_non_integer_total(total):
    # 参数化验证 allocate_event_quotas 拒绝负数、小数或布尔 total
    with pytest.raises(ValueError):
        gen_log.allocate_event_quotas(SPLIT_DOMAIN_COUNTS, total, 0.05)


def test_load_config_reads_d3_defaults_as_immutable_values():
    # 验证默认配置各字段取值正确，且配置对象与嵌套权重映射均不可变
    config = gen_log.load_config(CONFIG_PATH)

    assert config.seed == 20261101
    assert config.window_seconds == 60
    assert config.total_rows_approx == 50000
    assert config.dga_ratio == 0.05
    assert config.simulation_start_utc.isoformat() == "2026-11-01T00:00:00+00:00"
    assert config.split_period_hours == 24
    assert config.client_pool_size == 100
    assert config.active_client_fraction == 0.20
    assert config.active_event_fraction == 0.50
    assert dict(config.normal_behavior_ratio) == {"routine": 0.80, "burst": 0.20}
    assert dict(config.dga_behavior_ratio) == {
        "high_frequency": 0.40,
        "low_frequency": 0.35,
        "word_digit": 0.25,
    }
    assert config.mixed_window_fraction == 0.20
    assert dict(config.qtype_weights) == {
        "A": 0.80,
        "AAAA": 0.15,
        "CNAME": 0.03,
        "TXT": 0.01,
        "NS": 0.01,
    }
    with pytest.raises(FrozenInstanceError):
        config.seed = 1
    with pytest.raises(TypeError):
        config.qtype_weights["A"] = 0.0


def test_generator_config_copies_and_freezes_direct_mapping_inputs():
    # 验证 GeneratorConfig 复制传入映射：改动原字典不影响配置，配置内映射不可写
    normal_weights = {"routine": 0.80, "burst": 0.20}
    dga_weights = {
        "high_frequency": 0.40,
        "low_frequency": 0.35,
        "word_digit": 0.25,
    }
    qtype_weights = {
        "A": 0.80,
        "AAAA": 0.15,
        "CNAME": 0.03,
        "TXT": 0.01,
        "NS": 0.01,
    }
    config = gen_log.GeneratorConfig(
        seed=20261101,
        window_seconds=60,
        psl_snapshot="data/raw/public_suffix_list.dat",
        total_rows_approx=50000,
        dga_ratio=0.05,
        simulation_start_utc=datetime(2026, 11, 1, tzinfo=timezone.utc),
        split_period_hours=24,
        client_pool_size=100,
        active_client_fraction=0.20,
        active_event_fraction=0.50,
        normal_behavior_ratio=normal_weights,
        dga_behavior_ratio=dga_weights,
        mixed_window_fraction=0.20,
        qtype_weights=qtype_weights,
    )

    source_mappings = {
        "normal_behavior_ratio": (normal_weights, "routine", 0.60),
        "dga_behavior_ratio": (dga_weights, "high_frequency", 0.20),
        "qtype_weights": (qtype_weights, "A", 0.60),
    }
    original_values = {
        name: dict(getattr(config, name)) for name in source_mappings
    }
    for mapping, key, value in source_mappings.values():
        mapping[key] = value

    source_isolation = {
        name: dict(getattr(config, name)) == original_values[name]
        for name in source_mappings
    }
    config_mutations_rejected = []
    for name, (_mapping, key, _value) in source_mappings.items():
        try:
            getattr(config, name)[key] = -1.0
        except TypeError:
            config_mutations_rejected.append(True)
        else:
            config_mutations_rejected.append(False)

    assert source_isolation == {name: True for name in source_mappings}
    assert config_mutations_rejected == [True, True, True]


# 覆盖 root 与 log 两级的非法类型与范围：布尔充整数、字符串、0 或越界比例等
@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("root", "seed", True),
        ("root", "window_seconds", "60"),
        ("log", "total_rows_approx", 50000.0),
        ("log", "dga_ratio", True),
        ("log", "split_period_hours", 0),
        ("log", "client_pool_size", 0),
        ("log", "active_client_fraction", 1.1),
        ("log", "active_event_fraction", -0.1),
        ("log", "mixed_window_fraction", 0.0),
        ("log", "simulation_start_utc", "2026-11-01T00:00:01.000Z"),
    ],
)
def test_load_config_rejects_invalid_types_and_ranges(tmp_path, section, key, value):
    # 参数化验证配置项类型错误（布尔、字符串、浮点整数）或取值越界时加载失败
    data = make_config()
    target = data if section == "root" else data["log"]
    target[key] = value

    with pytest.raises(ValueError):
        load_config_data(tmp_path, data)


def test_load_config_rejects_excess_timestamp_fractional_precision(tmp_path):
    # 验证时间戳小数秒超过毫秒精度（9 位）时被拒绝
    data = make_config()
    data["log"]["simulation_start_utc"] = "2026-11-01T00:00:00.0000000001Z"

    with pytest.raises(ValueError):
        load_config_data(tmp_path, data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("normal_behavior_ratio", {"routine": 1.0}),
        (
            "dga_behavior_ratio",
            {"high_frequency": 0.4, "low_frequency": 0.35, "other": 0.25},
        ),
        ("qtype_weights", {"A": 1.0}),
    ],
)
def test_load_config_requires_exact_weight_keys(tmp_path, key, value):
    # 参数化验证权重映射的键集合必须与契约完全一致，缺键或多键均报错
    data = make_config()
    data["log"][key] = value

    with pytest.raises(ValueError):
        load_config_data(tmp_path, data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("normal_behavior_ratio", {"routine": 0.7, "burst": 0.2}),
        (
            "dga_behavior_ratio",
            {"high_frequency": 0.4, "low_frequency": 0.3, "word_digit": 0.2},
        ),
        (
            "qtype_weights",
            {"A": 0.7, "AAAA": 0.15, "CNAME": 0.03, "TXT": 0.01, "NS": 0.01},
        ),
    ],
)
def test_load_config_rejects_invalid_weight_sums(tmp_path, key, value):
    # 参数化验证三类权重映射之和不为 1 时报错
    data = make_config()
    data["log"][key] = value

    with pytest.raises(ValueError):
        load_config_data(tmp_path, data)


SPLIT_HEADER = ["domain", "label", "source", "algorithm", "split"]


def make_split_rows():
    # 构造 splits 样例：train/val/test 各含 normal、public_dga 与 manual_dga(word_digit) 三行
    rows = []
    for split in ("train", "val", "test"):
        rows.extend(
            [
                [f"normal-{split}.com", "0", "normal_list", "", split],
                [f"public-{split}.com", "1", "public_dga", "", split],
                [f"word-{split}.com", "1", "manual_dga", "word_digit", split],
            ]
        )
    return rows


def write_split_csv(path, rows, header=None):
    # 按指定表头写出 splits.csv，默认使用契约表头
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(SPLIT_HEADER if header is None else header)
        writer.writerows(rows)
    return path


@pytest.fixture
def local_psl(tmp_path):
    # 提供包含 ICANN 段（com、uk、co.uk）与 PRIVATE 段（blogspot.com）的本地 PSL 快照
    path = tmp_path / "public_suffix_list.dat"
    path.write_text(
        "\n".join(
            [
                "// ===BEGIN ICANN DOMAINS===",
                "com",
                "uk",
                "co.uk",
                "// ===END ICANN DOMAINS===",
                "// ===BEGIN PRIVATE DOMAINS===",
                "blogspot.com",
                "// ===END PRIVATE DOMAINS===",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_read_split_records_accepts_exact_contract_header_and_preserves_values(
    tmp_path,
):
    # 验证契约表头的 splits.csv 能被完整读入，各字段原样保留
    rows = make_split_rows()
    path = write_split_csv(tmp_path / "splits.csv", rows)

    records = gen_log.read_split_records(path)

    assert [
        (record.domain, record.label, record.source, record.algorithm, record.split)
        for record in records
    ] == [
        (domain, int(label), source, algorithm, split)
        for domain, label, source, algorithm, split in rows
    ]


@pytest.mark.parametrize(
    "header",
    [
        ["label", "domain", "source", "algorithm", "split"],
        ["domain", "label", "source", "algorithm", "split", "extra"],
        ["domain", "label", "source", "algorithm", "split", "split"],
        ["domain", "label", "source", "split"],
    ],
)
def test_read_split_records_rejects_any_non_contract_header(tmp_path, header):
    # 参数化验证列顺序变化、多列、重复列或缺少列的表头都被拒绝
    path = write_split_csv(tmp_path / "splits.csv", make_split_rows(), header)

    with pytest.raises(ValueError):
        gen_log.read_split_records(path)


# mutation 表示要篡改的列，expected_value 为写入的非法值
@pytest.mark.parametrize(
    ("mutation", "expected_value"),
    [
        ("label", "2"),
        ("label", "01"),
        ("label", "1.0"),
        ("label", " 1"),
        ("source", "unknown_source"),
        ("normal_label", "1"),
        ("normal_algorithm", "rand_alpha"),
        ("public_algorithm", "word_digit"),
        ("manual_label", "0"),
        ("manual_algorithm", "unknown_algorithm"),
        ("split", "validation"),
    ],
)
def test_read_split_records_rejects_invalid_contract_semantics(
    tmp_path, mutation, expected_value
):
    # 参数化验证标签、来源、算法与 split 的语义非法时（如 label=2、normal 行带算法）报错
    rows = make_split_rows()
    row_index, column_index = {
        "label": (0, 1),
        "source": (0, 2),
        "normal_label": (0, 1),
        "normal_algorithm": (0, 3),
        "public_algorithm": (1, 3),
        "manual_label": (2, 1),
        "manual_algorithm": (2, 3),
        "split": (0, 4),
    }[mutation]
    rows[row_index][column_index] = expected_value
    path = write_split_csv(tmp_path / "splits.csv", rows)

    with pytest.raises(ValueError):
        gen_log.read_split_records(path)


def test_read_split_records_rejects_duplicate_domains(tmp_path):
    # 验证 splits.csv 内出现重复域名时报错
    rows = make_split_rows()
    rows.append(rows[0].copy())
    path = write_split_csv(tmp_path / "splits.csv", rows)

    with pytest.raises(ValueError, match="duplicate"):
        gen_log.read_split_records(path)


@pytest.mark.parametrize(
    "bad_row",
    [[], ["only-domain"], ["a.com", "0", "normal_list", "", "train", "extra"]],
)
def test_read_split_records_rejects_malformed_rows(tmp_path, bad_row):
    # 参数化验证空行、列数不足或列数过多的数据行报错
    rows = make_split_rows()
    rows.insert(0, bad_row)
    path = write_split_csv(tmp_path / "splits.csv", rows)

    with pytest.raises(ValueError, match="row|field|column|empty"):
        gen_log.read_split_records(path)


def test_read_split_records_rejects_empty_file(tmp_path):
    # 验证空文件因缺少表头报错
    path = tmp_path / "splits.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty|header|CSV"):
        gen_log.read_split_records(path)


def test_read_split_records_rejects_header_without_data_rows(tmp_path):
    # 验证只有表头没有数据行时报错
    path = write_split_csv(tmp_path / "splits.csv", [])

    with pytest.raises(ValueError, match="data row|empty|records"):
        gen_log.read_split_records(path)


def test_read_split_records_accepts_every_manual_algorithm(tmp_path):
    # 验证 rand_alpha 与 md5_date 等全部手工 DGA 算法行均可被接受
    rows = make_split_rows()
    rows.extend(
        [
            ["rand-train.com", "1", "manual_dga", "rand_alpha", "train"],
            ["md5-train.com", "1", "manual_dga", "md5_date", "train"],
        ]
    )
    path = write_split_csv(tmp_path / "splits.csv", rows)

    records = gen_log.read_split_records(path)

    assert [record.algorithm for record in records[-2:]] == ["rand_alpha", "md5_date"]


def test_read_split_records_accepts_hard_negative_rows(tmp_path):
    # 验证 hard_negative 行可被接受且其算法列必须为空
    rows = make_split_rows()
    rows.extend(
        [
            ["hard-train.com", "0", "hard_negative", "", "train"],
            ["hard-val.com", "0", "hard_negative", "", "val"],
            ["hard-test.com", "0", "hard_negative", "", "test"],
        ]
    )
    path = write_split_csv(tmp_path / "splits.csv", rows)

    records = gen_log.read_split_records(path)

    assert [record.source for record in records[-3:]] == ["hard_negative"] * 3
    assert [record.algorithm for record in records[-3:]] == [""] * 3


@pytest.mark.parametrize("domain", ["Normal-train.com", "normal-train.com."])
def test_read_split_records_rejects_domains_that_need_normalization(tmp_path, domain):
    # 参数化验证大写或带根点的未归一化域名被拒绝
    rows = make_split_rows()
    rows[0][0] = domain
    path = write_split_csv(tmp_path / "splits.csv", rows)

    with pytest.raises(ValueError, match="normalized"):
        gen_log.read_split_records(path)


@pytest.mark.parametrize("split", ["train", "val", "test"])
@pytest.mark.parametrize("missing_label", [0, 1])
def test_validate_domain_pools_requires_both_labels_in_every_split(
    tmp_path, local_psl, split, missing_label
):
    # 参数化验证每个 split 必须同时包含正负样本，缺任一类即报错
    rows = [
        row
        for row in make_split_rows()
        if not (row[4] == split and int(row[1]) == missing_label)
    ]
    records = gen_log.read_split_records(
        write_split_csv(tmp_path / "splits.csv", rows)
    )

    with pytest.raises(ValueError, match="pool|coverage|split"):
        gen_log.validate_domain_pools(records, local_psl)


@pytest.mark.parametrize("split", ["train", "val", "test"])
def test_validate_domain_pools_requires_word_digit_in_each_split(
    tmp_path, local_psl, split
):
    # 参数化验证每个 split 都必须含 word_digit 手工样本
    rows = [
        row
        for row in make_split_rows()
        if row[3] != "word_digit" or row[4] != split
    ]
    records = gen_log.read_split_records(write_split_csv(tmp_path / "splits.csv", rows))

    with pytest.raises(ValueError, match="word_digit|coverage|pool"):
        gen_log.validate_domain_pools(records, local_psl)


def test_validate_domain_pools_rejects_boolean_labels(tmp_path, local_psl):
    # 验证标签被替换为布尔值时按语义校验报错（True 不等同于 1）
    records = gen_log.read_split_records(
        write_split_csv(tmp_path / "splits.csv", make_split_rows())
    )
    records[0] = replace(records[0], label=True)

    with pytest.raises(ValueError, match="label|semantics"):
        gen_log.validate_domain_pools(records, local_psl)


def test_validate_domain_pools_rejects_domain_without_registrable_suffix(
    tmp_path, local_psl
):
    # 验证无法解析 eTLD+1 的域名（localhost）在池校验时报错
    rows = make_split_rows()
    rows[0][0] = "localhost"
    records = gen_log.read_split_records(
        write_split_csv(tmp_path / "splits.csv", rows)
    )

    with pytest.raises(ValueError, match=r"no registered domain|eTLD\+1"):
        gen_log.validate_domain_pools(records, local_psl)


# must_reject 表示该域名组合是否应判定为跨 split 冲突
@pytest.mark.parametrize(
    ("train_domain", "val_domain", "must_reject"),
    [
        ("one.shared.co.uk", "two.shared.co.uk", True),
        ("alpha.blogspot.com", "beta.blogspot.com", False),
        ("one.shared.blogspot.com", "two.shared.blogspot.com", True),
    ],
)
def test_validate_domain_pools_uses_ordinary_and_private_psl_rules(
    tmp_path, local_psl, train_domain, val_domain, must_reject
):
    # 参数化验证普通 PSL（co.uk）与私有 PSL（blogspot.com）规则分别决定跨 split 冲突判定
    rows = make_split_rows()
    rows[0][0] = train_domain
    rows[3][0] = val_domain
    records = gen_log.read_split_records(write_split_csv(tmp_path / "splits.csv", rows))

    if must_reject:
        with pytest.raises(
            ValueError, match=r"eTLD\+1|registrable domain|multiple splits"
        ):
            gen_log.validate_domain_pools(records, local_psl)
    else:
        assert gen_log.validate_domain_pools(records, local_psl) is None


def test_split_parsing_and_pool_validation_do_not_use_network(
    tmp_path, local_psl, monkeypatch
):
    # 验证读取 splits 与池校验全程不访问网络（socket 被替换后仍通过）
    def fail_network(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    path = write_split_csv(tmp_path / "splits.csv", make_split_rows())

    records = gen_log.read_split_records(path)

    assert gen_log.validate_domain_pools(records, local_psl) is None


def make_behavior_records():
    # 构造每个 split 三条记录（normal、public_dga、manual_dga/word_digit），供行为分组测试使用
    records = []
    for split in ("train", "val", "test"):
        records.extend(
            [
                DomainRecord(
                    domain=f"oak-{split}.com",
                    label=0,
                    source="normal_list",
                    algorithm="",
                    etld_plus_one="",
                    split=split,
                    sources=(),
                ),
                DomainRecord(
                    domain=f"cobalt-{split}.net",
                    label=1,
                    source="public_dga",
                    algorithm="",
                    etld_plus_one="",
                    split=split,
                    sources=(),
                ),
                DomainRecord(
                    domain=f"maple-{split}.org",
                    label=1,
                    source="manual_dga",
                    algorithm="word_digit",
                    etld_plus_one="",
                    split=split,
                    sources=(),
                ),
            ]
        )
    return records


def make_behavior_config(tmp_path, total_rows=6000, **log_overrides):
    # 在默认配置上覆盖总行数与 log 字段后加载，得到行为生成测试用配置
    data = make_config()
    data["log"]["total_rows_approx"] = total_rows
    data["log"].update(log_overrides)
    return load_config_data(tmp_path, data)


def flatten_drafts(groups):
    # 展平分组，取出全部事件草稿
    return [event for group in groups for event in group.events]


def test_derive_rng_uses_stable_big_endian_sha256_child_seed():
    # 验证 derive_rng 以 seed|d3|split|label|behavior|purpose 的 SHA-256 大端整数为种子，结果可复现
    seed, split, label, behavior, purpose = (
        20261101,
        "val",
        1,
        "high_frequency",
        "domains",
    )
    material = f"{seed}|d3|{split}|{label}|{behavior}|{purpose}".encode("utf-8")
    expected_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")

    first = gen_log.derive_rng(seed, split, label, behavior, purpose)
    second = gen_log.derive_rng(seed, split, label, behavior, purpose)
    repeated = gen_log.derive_rng(seed, split, label, behavior, purpose)
    expected = random.Random(expected_seed)

    assert [first.getrandbits(64) for _ in range(5)] == [
        expected.getrandbits(64) for _ in range(5)
    ]
    assert [second.getrandbits(64) for _ in range(5)] == [
        repeated.getrandbits(64) for _ in range(5)
    ]


# (事件数, 每组下限, 每组上限) 组合，期望组数为 ceil(事件数/上限)
@pytest.mark.parametrize(
    ("event_count", "minimum", "maximum"),
    [(37, 1, 2), (41, 3, 12), (47, 5, 12), (39, 1, 4)],
)
def test_partition_event_groups_conserves_events_within_feasible_bounds(
    event_count, minimum, maximum
):
    # 参数化验证分组数为 ceil(事件数/上限)，事件总数守恒且每组大小落在区间内
    sizes = gen_log.partition_event_groups(
        event_count, minimum, maximum, random.Random(2026)
    )

    assert len(sizes) == math.ceil(event_count / maximum)
    assert sum(sizes) == event_count
    assert all(minimum <= size <= maximum for size in sizes)


def test_partition_event_groups_handles_empty_and_rejects_infeasible_counts():
    # 验证 0 个事件返回空列表，而无法满足最小分组大小时报错
    assert gen_log.partition_event_groups(0, 5, 12, random.Random(1)) == []
    with pytest.raises(ValueError, match="infeasible|minimum|group"):
        gen_log.partition_event_groups(1, 5, 12, random.Random(1))


def test_allocate_behavior_quotas_uses_fixed_orders_and_conserves_each_label():
    # 验证行为配额按固定顺序用最大余数分配，且各 split 的 normal/dga 总量守恒
    event_quotas = {
        "train": {"total": 11, "normal": 6, "dga": 5},
        "val": {"total": 7, "normal": 4, "dga": 3},
        "test": {"total": 5, "normal": 3, "dga": 2},
    }
    normal_weights = {"routine": 0.5, "burst": 0.5}
    dga_weights = {
        "high_frequency": 1 / 3,
        "low_frequency": 1 / 3,
        "word_digit": 1 / 3,
    }

    result = gen_log.allocate_behavior_quotas(
        event_quotas, normal_weights, dga_weights
    )

    assert result["train"] == {
        "normal": {"routine": 3, "burst": 3},
        "dga": {"high_frequency": 2, "low_frequency": 2, "word_digit": 1},
    }
    assert result["val"] == {
        "normal": {"routine": 2, "burst": 2},
        "dga": {"high_frequency": 1, "low_frequency": 1, "word_digit": 1},
    }
    assert result["test"] == {
        "normal": {"routine": 2, "burst": 1},
        "dga": {"high_frequency": 1, "low_frequency": 1, "word_digit": 0},
    }
    for split, quota in event_quotas.items():
        assert sum(result[split]["normal"].values()) == quota["normal"]
        assert sum(result[split]["dga"].values()) == quota["dga"]


def test_allocate_qtype_quotas_uses_fixed_ties_and_separate_split_label_buckets():
    # 验证 QTYPE 配额按 split 与标签分别分桶，并按固定平局顺序分配
    counts = {
        "train": {0: 1, 1: 2},
        "val": {0: 3, 1: 1},
        "test": {0: 0, 1: 4},
    }
    weights = {qtype: 0.2 for qtype in ("A", "AAAA", "CNAME", "TXT", "NS")}

    result = gen_log.allocate_qtype_quotas(counts, weights)

    assert result == {
        "train": {
            0: {"A": 1, "AAAA": 0, "CNAME": 0, "TXT": 0, "NS": 0},
            1: {"A": 1, "AAAA": 1, "CNAME": 0, "TXT": 0, "NS": 0},
        },
        "val": {
            0: {"A": 1, "AAAA": 1, "CNAME": 1, "TXT": 0, "NS": 0},
            1: {"A": 1, "AAAA": 0, "CNAME": 0, "TXT": 0, "NS": 0},
        },
        "test": {
            0: {"A": 0, "AAAA": 0, "CNAME": 0, "TXT": 0, "NS": 0},
            1: {"A": 1, "AAAA": 1, "CNAME": 1, "TXT": 1, "NS": 0},
        },
    }
    for split, labels in counts.items():
        for label, count in labels.items():
            assert sum(result[split][label].values()) == count


@pytest.mark.parametrize(
    "invalid_label_counts",
    [
        pytest.param({False: 2, 1: 3}, id="bool-label"),
        pytest.param({0: 2, 1.0: 3}, id="non-integer-label"),
    ],
)
def test_allocate_qtype_quotas_rejects_bool_or_non_integer_label_keys(
    invalid_label_counts,
):
    # 参数化验证标签键为布尔或非整数（1.0）时抛 ValueError
    counts = {
        "train": invalid_label_counts,
        "val": {0: 0, 1: 0},
        "test": {0: 0, 1: 0},
    }
    weights = {qtype: 0.2 for qtype in ("A", "AAAA", "CNAME", "TXT", "NS")}

    with pytest.raises(ValueError):
        gen_log.allocate_qtype_quotas(counts, weights)


def test_build_behavior_groups_conserves_per_split_quotas_and_group_bounds(tmp_path):
    # 验证行为组在各 split/标签/行为上的事件数满足配额，组大小在上下界内，总行数等于配置
    config = make_behavior_config(tmp_path)
    records = make_behavior_records()
    split_domain_counts = {split: 3 for split in ("train", "val", "test")}
    event_quotas = gen_log.allocate_event_quotas(
        split_domain_counts, config.total_rows_approx, config.dga_ratio
    )
    expected = gen_log.allocate_behavior_quotas(
        event_quotas, config.normal_behavior_ratio, config.dga_behavior_ratio
    )

    groups = gen_log.build_behavior_groups(records, config)

    bounds = {
        "routine": (1, 2),
        "burst": (3, 12),
        "high_frequency": (5, 12),
        "low_frequency": (1, 2),
        "word_digit": (1, 4),
    }
    for split in ("train", "val", "test"):
        for label_name, label in (("normal", 0), ("dga", 1)):
            for behavior, expected_events in expected[split][label_name].items():
                matching = [
                    group
                    for group in groups
                    if group.split == split
                    and group.label == label
                    and group.behavior == behavior
                ]
                assert sum(len(group.events) for group in matching) == expected_events
                minimum, maximum = bounds[behavior]
                assert all(minimum <= len(group.events) <= maximum for group in matching)

    assert len(flatten_drafts(groups)) == config.total_rows_approx
    assert {
        (event.split, event.label) for event in flatten_drafts(groups)
    } == {
        (split, label) for split in ("train", "val", "test") for label in (0, 1)
    }


def test_groups_share_qname_client_and_utc_minute_and_nonpairs_do_not_collide(
    tmp_path,
):
    # 验证同组共享 qname、客户端 IP 与整分钟 UTC 窗口；同一窗口至多两条异标签组且不碰撞
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(make_behavior_records(), config)
    groups_by_window = {}

    for group in groups:
        assert group.domain.domain == group.qname
        assert group.window_start.tzinfo is timezone.utc
        assert group.window_start.second == 0
        assert group.window_start.microsecond == 0
        assert {event.qname for event in group.events} == {group.qname}
        assert {event.client_ip for event in group.events} == {group.client_ip}
        assert {event.window_start for event in group.events} == {group.window_start}
        groups_by_window.setdefault(
            (group.split, group.client_ip, group.window_start), []
        ).append(group)

    for window_groups in groups_by_window.values():
        if len(window_groups) == 1:
            continue
        assert len(window_groups) == 2
        first, second = window_groups
        assert first.placement_unit_id == second.placement_unit_id
        assert first.split == second.split
        assert first.client_ip == second.client_ip
        assert first.window_start == second.window_start
        assert first.label != second.label
        assert first.qname != second.qname


def test_mixed_windows_pair_reserved_routine_singletons_without_adding_rows(tmp_path):
    # 验证混合窗口把预留的单事件 routine 组与 high_frequency 组配对，且不增加总行数
    config = make_behavior_config(tmp_path, mixed_window_fraction=0.2)
    records = make_behavior_records()
    event_quotas = gen_log.allocate_event_quotas(
        {split: 3 for split in ("train", "val", "test")},
        config.total_rows_approx,
        config.dga_ratio,
    )
    behavior_quotas = gen_log.allocate_behavior_quotas(
        event_quotas, config.normal_behavior_ratio, config.dga_behavior_ratio
    )

    groups = gen_log.build_behavior_groups(records, config)

    assert len(flatten_drafts(groups)) == config.total_rows_approx
    for split in ("train", "val", "test"):
        high_groups = [
            group
            for group in groups
            if group.split == split and group.behavior == "high_frequency"
        ]
        paired_count = math.ceil(
            len(high_groups) * config.mixed_window_fraction
        )
        pairs = {}
        for group in groups:
            if group.split == split and group.paired_group_id is not None:
                pairs.setdefault(group.placement_unit_id, []).append(group)
        assert len(pairs) == paired_count
        assert paired_count >= 1
        for pair in pairs.values():
            assert len(pair) == 2
            normal = next(group for group in pair if group.label == 0)
            dga = next(group for group in pair if group.label == 1)
            assert normal.behavior == "routine"
            assert len(normal.events) == 1
            assert dga.behavior == "high_frequency"
            assert normal.qname != dga.qname
            assert normal.split == dga.split == split
            assert normal.client_ip == dga.client_ip
            assert normal.window_start == dga.window_start
        assert sum(
            len(group.events)
            for group in groups
            if group.split == split and group.label == 0 and group.behavior == "routine"
        ) == behavior_quotas[split]["normal"]["routine"]


def test_behavior_group_construction_is_repeatable_and_split_order_independent(
    tmp_path,
):
    # 验证构建结果可重复，且 train 记录顺序变化不影响 val/test 分组
    config = make_behavior_config(tmp_path)
    records = make_behavior_records()

    first = gen_log.build_behavior_groups(records, config)
    second = gen_log.build_behavior_groups(records, config)
    train_reordered = [
        *reversed([record for record in records if record.split == "train"]),
        *(record for record in records if record.split != "train"),
    ]
    changed_train_order = gen_log.build_behavior_groups(train_reordered, config)

    assert first == second
    for split in ("val", "test"):
        assert [group for group in first if group.split == split] == [
            group for group in changed_train_order if group.split == split
        ]


def test_each_draft_copies_metadata_from_its_same_split_label_input_record(tmp_path):
    # 验证每条事件草稿的元数据复制自同 split 同标签的输入记录
    config = make_behavior_config(tmp_path)
    records = make_behavior_records()
    record_by_key = {
        (record.domain, record.label, record.source, record.algorithm, record.split): record
        for record in records
    }

    events = flatten_drafts(gen_log.build_behavior_groups(records, config))

    for event in events:
        source = record_by_key[
            (event.qname, event.label, event.source, event.algorithm, event.split)
        ]
        assert event.record == source
        assert event.qname == source.domain
    assert {event.label for event in events if event.split == "train"} == {0, 1}
    assert {event.label for event in events if event.split == "val"} == {0, 1}
    assert {event.label for event in events if event.split == "test"} == {0, 1}


def test_high_frequency_and_word_digit_groups_select_from_eligible_split_pools(tmp_path):
    # 验证高频组只取同 split 的 DGA 记录，word_digit 组只取同 split 的手工 word_digit 记录
    records = make_behavior_records()
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(records, config)

    for group in groups:
        if group.behavior == "high_frequency":
            assert group.domain.split == group.split
            assert group.domain.label == 1
            assert group.domain in [
                record
                for record in records
                if record.split == group.split and record.label == 1
            ]
        if group.behavior == "word_digit":
            assert group.domain.split == group.split
            assert group.domain.label == 1
            assert group.domain.source == "manual_dga"
            assert group.domain.algorithm == "word_digit"
            assert group.domain in [
                record
                for record in records
                if record.split == group.split
                and record.label == 1
                and record.source == "manual_dga"
                and record.algorithm == "word_digit"
            ]


def test_clients_use_test_net_addresses_and_active_clients_carry_configured_share(
    tmp_path,
):
    # 验证客户端 IP 全在 192.0.2.0/24 等文档网段，活跃客户端承载的事件占比与配置相差不超过 2 个百分点
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(make_behavior_records(), config)
    pool = gen_log._client_pool(config.client_pool_size)
    active_count = math.floor(
        config.client_pool_size * config.active_client_fraction + 0.5
    )
    active_clients = set(pool[:active_count])
    documentation_ranges = tuple(
        ipaddress.ip_network(network)
        for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
    )

    assert len(pool) == config.client_pool_size
    assert all(
        any(ipaddress.ip_address(address) in network for network in documentation_ranges)
        for address in pool
    )
    for split in ("train", "val", "test"):
        events = [
            event
            for event in flatten_drafts(groups)
            if event.split == split
        ]
        active_share = sum(event.client_ip in active_clients for event in events) / len(events)
        assert abs(active_share - config.active_event_fraction) <= 0.02


def test_active_client_count_uses_exact_half_up_fraction_rounding():
    # 验证活跃客户端数按四舍五入计算：100 乘 0.145 得 15
    assert gen_log._active_client_count(100, 0.145) == 15


def test_active_event_share_accepts_exact_two_percentage_point_boundary():
    # 验证活跃事件占比恰好超出 2 个百分点边界时不在容差内（50/100 通过、49/100 不通过）
    assert gen_log._active_event_share_within_tolerance(50, 100, 0.52)
    assert not gen_log._active_event_share_within_tolerance(49, 100, 0.52)


def test_full_active_client_pool_accepts_target_event_share_of_098(tmp_path):
    # 验证活跃客户端比例 1.0、目标事件占比 0.98 时仍可生成，且 IP 数不超过客户端池大小
    config = make_behavior_config(
        tmp_path,
        active_client_fraction=1.0,
        active_event_fraction=0.98,
    )

    groups = gen_log.build_behavior_groups(make_behavior_records(), config)

    assert {event.client_ip for event in flatten_drafts(groups)}
    assert len({event.client_ip for event in flatten_drafts(groups)}) <= config.client_pool_size


def test_client_window_counts_and_behavior_intervals_have_intended_overlap(tmp_path):
    # 验证同窗口同 qname 计数、burst/高频事件间隔 1 到 5 秒且整组跨度不超过 55 秒
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(make_behavior_records(), config)
    events = flatten_drafts(groups)
    qname_counts = {}
    window_events = {}

    for event in events:
        qname_counts.setdefault(
            (event.split, event.label, event.client_ip, event.window_start, event.qname),
            0,
        )
        qname_counts[
            (event.split, event.label, event.client_ip, event.window_start, event.qname)
        ] += 1
        window_events.setdefault(
            (event.split, event.client_ip, event.window_start), []
        ).append(event)

    for label in (0, 1):
        assert any(count in (1, 2) for key, count in qname_counts.items() if key[1] == label)
        assert any(
            5 <= len(window) <= 12
            and {event.label for event in window} == {label}
            for window in window_events.values()
        )
        assert any(
            1 <= len(window) <= 2
            and {event.label for event in window} == {label}
            for window in window_events.values()
        )
    assert all(
        {event.label for event in window} == {0, 1}
        for group in groups
        if group.paired_group_id is not None
        for window in [
            window_events[(group.split, group.client_ip, group.window_start)]
        ]
    )

    for group in groups:
        if group.behavior not in ("burst", "high_frequency"):
            continue
        offsets = sorted(event.offset_ms for event in group.events)
        assert all(1000 <= right - left <= 5000 for left, right in zip(offsets, offsets[1:]))
        assert offsets[-1] - offsets[0] <= 55000

    checked_pairs = set()
    for group in groups:
        if group.paired_group_id is None or group.placement_unit_id in checked_pairs:
            continue
        checked_pairs.add(group.placement_unit_id)
        paired_events = window_events[(group.split, group.client_ip, group.window_start)]
        ordered = sorted(
            paired_events,
            key=lambda event: (
                event.window_start + timedelta(milliseconds=event.offset_ms),
                event.offset_ms,
                event.sequence_index,
            ),
        )
        offsets = [event.offset_ms for event in ordered]
        assert len(ordered) >= 6
        assert all(
            1000 <= right - left <= 5000
            for left, right in zip(offsets, offsets[1:])
        )
        assert offsets[-1] - offsets[0] <= 55000
        assert {event.label for event in ordered} == {0, 1}


def test_behavior_group_windows_stay_inside_each_split_period(tmp_path):
    # 验证各 split 的窗口起点都落在其 24 小时时段内
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(make_behavior_records(), config)

    for split_index, split in enumerate(("train", "val", "test")):
        period_start = config.simulation_start_utc + timedelta(
            hours=split_index * config.split_period_hours
        )
        period_end = period_start + timedelta(hours=config.split_period_hours)
        starts = [group.window_start for group in groups if group.split == split]

        assert starts
        assert all(period_start <= start < period_end for start in starts)


def test_all_behavior_group_event_offsets_stay_inside_their_minute(tmp_path):
    # 验证所有事件偏移量都在 0 到 60000 毫秒之间
    config = make_behavior_config(tmp_path)
    groups = gen_log.build_behavior_groups(make_behavior_records(), config)
    events = flatten_drafts(groups)

    assert events
    assert all(0 <= event.offset_ms < 60000 for event in events)


@pytest.mark.parametrize(
    ("behavior", "label"), [("burst", 0), ("high_frequency", 1)]
)
def test_maximum_sequence_offset_stays_inside_its_minute(
    tmp_path, monkeypatch, behavior, label
):
    # 验证间隔取上界 5 秒时 12 条事件的最后一个偏移仍小于 60000 毫秒
    class UpperBoundRng:
        def randint(self, lower, upper):
            return 5000 if lower == 1000 else upper

    config = make_behavior_config(tmp_path)
    record = next(
        record
        for record in make_behavior_records()
        if record.split == "train" and record.label == label
    )
    spec = gen_log._GroupSpec(
        record=record,
        behavior=behavior,
        group_id="train:boundary",
        placement_unit_id="train:boundary",
        size=12,
    )
    unit = gen_log._PlacementUnit("train", "train:boundary", (spec,))
    monkeypatch.setattr(gen_log, "derive_rng", lambda *_args: UpperBoundRng())

    gen_log._assign_unit_offsets(unit, config)

    assert spec.offsets[-1] < 60000


def test_assign_qtypes_uses_exact_split_label_quotas_without_behavior_binding(
    tmp_path,
):
    # 验证 QTYPE 分配按 split 与标签精确满足配额，重复调用结果一致且不受行为类型绑定
    weights = {qtype: 0.2 for qtype in ("A", "AAAA", "CNAME", "TXT", "NS")}
    config = make_behavior_config(tmp_path, qtype_weights=weights)
    records = make_behavior_records()
    records_by_key = {(record.split, record.label): record for record in records}
    drafts = []
    for split in ("train", "val", "test"):
        for label, behaviors in (
            (0, ("routine", "burst")),
            (1, ("high_frequency", "low_frequency", "word_digit")),
        ):
            record = records_by_key[(split, label)]
            for behavior in behaviors:
                for index in range(150):
                    drafts.append(
                        gen_log.EventDraft(
                            record=record,
                            split=split,
                            label=label,
                            source=record.source,
                            algorithm=record.algorithm,
                            behavior=behavior,
                            qname=record.domain,
                            group_id=f"{split}:{label}:{behavior}:{index}",
                            placement_unit_id=f"{split}:{label}:{behavior}:{index}",
                            client_ip="192.0.2.1",
                            window_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                            offset_ms=index,
                            sequence_index=index,
                        )
                    )

    assigned = gen_log.assign_qtypes(drafts, config)
    repeated = gen_log.assign_qtypes(drafts, config)
    quotas = gen_log.allocate_qtype_quotas(
        {
            split: {
                label: sum(
                    draft.split == split and draft.label == label
                    for draft in drafts
                )
                for label in (0, 1)
            }
            for split in ("train", "val", "test")
        },
        weights,
    )

    assert assigned == repeated
    assert len(assigned) == len(drafts)
    for split in ("train", "val", "test"):
        for label in (0, 1):
            bucket = [
                (draft, qtype)
                for draft, qtype in zip(drafts, assigned)
                if draft.split == split and draft.label == label
            ]
            assert Counter(qtype for _draft, qtype in bucket) == quotas[split][label]
            for behavior in {draft.behavior for draft, _qtype in bucket}:
                assert {
                    qtype
                    for draft, qtype in bucket
                    if draft.behavior == behavior
                } == set(weights)


def test_build_events_assigns_utc_epoch_aligned_non_overlapping_split_times(
    tmp_path,
):
    # 验证事件时间戳为 UTC 毫秒对齐、按时间与 stable_order 排序、三个 split 时段不重叠，且内容与草稿一致
    config = make_behavior_config(tmp_path, total_rows=6000)
    records = make_behavior_records()
    groups = gen_log.build_behavior_groups(records, config)

    events = gen_log.build_events(records, config)

    assert len(events) == config.total_rows_approx
    assert all(event.timestamp.tzinfo is timezone.utc for event in events)
    assert all(event.timestamp.microsecond % 1000 == 0 for event in events)
    assert events == sorted(
        events, key=lambda event: (event.timestamp, event.stable_order)
    )
    assert len({event.stable_order for event in events}) == len(events)
    for event in events:
        epoch_minute = event.timestamp.replace(second=0, microsecond=0).timestamp()
        assert epoch_minute % 60 == 0
    expected_events = Counter(
        (
            draft.split,
            draft.label,
            draft.source,
            draft.algorithm,
            draft.behavior,
            draft.qname,
            draft.client_ip,
            draft.window_start + timedelta(milliseconds=draft.offset_ms),
        )
        for group in groups
        for draft in group.events
    )
    actual_events = Counter(
        (
            event.split,
            event.label,
            event.source,
            event.algorithm,
            event.behavior,
            event.qname,
            event.client_ip,
            event.timestamp,
        )
        for event in events
    )
    assert actual_events == expected_events

    split_ranges = []
    for split_index, split in enumerate(("train", "val", "test")):
        start = config.simulation_start_utc + timedelta(
            hours=split_index * config.split_period_hours
        )
        end = start + timedelta(hours=config.split_period_hours)
        split_events = [event for event in events if event.split == split]
        assert split_events
        assert all(start <= event.timestamp < end for event in split_events)
        split_ranges.append(
            (min(event.timestamp for event in split_events),
             max(event.timestamp for event in split_events))
        )

    assert split_ranges[0][1] < split_ranges[1][0]
    assert split_ranges[1][1] < split_ranges[2][0]

    events_by_window = {}
    for event in events:
        window_start = event.timestamp.replace(second=0, microsecond=0)
        events_by_window.setdefault(
            (event.split, event.client_ip, window_start), []
        ).append(event)
    mixed_windows = [
        window
        for window in events_by_window.values()
        if {event.label for event in window} == {0, 1}
    ]
    assert mixed_windows
    assert all(
        max(event.timestamp for event in window)
        - min(event.timestamp for event in window)
        <= timedelta(seconds=55)
        for window in mixed_windows
    )


def test_build_events_offsets_splits_by_configured_period(tmp_path):
    # 验证 split_period_hours=12 时三个 split 的时段依次偏移 12 小时
    config = make_behavior_config(
        tmp_path,
        total_rows=6000,
        split_period_hours=12,
    )

    events = gen_log.build_events(make_behavior_records(), config)

    for split_index, split in enumerate(("train", "val", "test")):
        period_start = config.simulation_start_utc + timedelta(
            hours=split_index * config.split_period_hours
        )
        period_end = period_start + timedelta(hours=config.split_period_hours)
        split_events = [event for event in events if event.split == split]

        assert split_events
        assert all(period_start <= event.timestamp < period_end for event in split_events)


def test_build_events_qtype_counts_match_each_split_label_quota(tmp_path):
    # 验证事件 QTYPE 统计与 allocate_qtype_quotas 计算的配额一致
    config = make_behavior_config(tmp_path, total_rows=6000)

    events = gen_log.build_events(make_behavior_records(), config)

    counts = {
        split: {
            label: sum(
                event.split == split and event.label == label for event in events
            )
            for label in (0, 1)
        }
        for split in ("train", "val", "test")
    }
    expected = gen_log.allocate_qtype_quotas(counts, config.qtype_weights)
    for split in ("train", "val", "test"):
        for label in (0, 1):
            actual = Counter(
                event.qtype
                for event in events
                if event.split == split and event.label == label
            )
            assert actual == expected[split][label]


def test_serialize_log_emits_sorted_contract_v1_csv_bytes(tmp_path):
    # 验证输出为无 BOM、LF 换行、固定九列表头的契约 CSV，按时间排序且不含内部字段
    config = make_behavior_config(tmp_path, total_rows=6000)
    records = make_behavior_records()
    events = gen_log.build_events(records, config)

    payload = gen_log.serialize_log(list(reversed(events)))
    output_path = tmp_path / "generated.csv"
    output_path.write_bytes(payload)
    raw = output_path.read_bytes()
    decoded = raw.decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    rows = list(reader)
    expected_columns = [
        "event_id",
        "timestamp",
        "client_ip",
        "qname",
        "qtype",
        "label",
        "source",
        "algorithm",
        "split",
    ]

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert reader.fieldnames == expected_columns
    assert all(list(row) == expected_columns for row in rows)
    assert [int(row["event_id"]) for row in rows] == list(range(1, len(rows) + 1))
    assert len({row["event_id"] for row in rows}) == len(rows)
    ordered_events = sorted(
        events, key=lambda event: (event.timestamp, event.stable_order)
    )
    timestamps = [
        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        for row in rows
    ]
    assert timestamps == [event.timestamp for event in ordered_events]
    assert [row["qname"] for row in rows] == [
        event.qname for event in ordered_events
    ]
    assert all(
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
            row["timestamp"],
        )
        for row in rows
    )
    assert all(timestamp.utcoffset() == timedelta(0) for timestamp in timestamps)
    assert timestamps == sorted(timestamps)
    assert {row["qtype"] for row in rows} <= {"A", "AAAA", "CNAME", "TXT", "NS"}

    valid_records = {
        (
            record.domain,
            str(record.label),
            record.source,
            record.algorithm,
            record.split,
        )
        for record in records
    }
    for row in rows:
        assert ipaddress.IPv4Address(row["client_ip"])
        assert str(ipaddress.IPv4Address(row["client_ip"])) == row["client_ip"]
        assert row["qname"] == row["qname"].lower()
        assert not row["qname"].endswith(".")
        assert gen_log.prepare_data.normalize_domain(row["qname"]) == row["qname"]
        assert (
            row["qname"],
            row["label"],
            row["source"],
            row["algorithm"],
            row["split"],
        ) in valid_records
        assert row["label"] in {"0", "1"}
        assert row["source"] in {"normal_list", "public_dga", "manual_dga"}
        assert row["split"] in {"train", "val", "test"}
        if row["source"] == "manual_dga":
            assert row["algorithm"] in {"rand_alpha", "word_digit", "md5_date"}
        else:
            assert row["algorithm"] == ""
    assert b"high_frequency" not in raw
    assert b"low_frequency" not in raw
    assert b"routine" not in raw
    assert b"group_id" not in raw
    assert b"placement_unit_id" not in raw
    assert b"sequence_index" not in raw
    assert b"offset_ms" not in raw


# changes 为对合法事件的字段篡改，message 为期望的错误关键字
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        pytest.param({"client_ip": "8.8.8.8"}, "TEST-NET", id="non-test-net-ip"),
        pytest.param({"qtype": "BOGUS"}, "qtype", id="invalid-qtype"),
        pytest.param(
            {
                "timestamp": datetime(
                    2026,
                    11,
                    1,
                    tzinfo=timezone(timedelta(hours=8)),
                )
            },
            "timestamp",
            id="non-utc-offset",
        ),
        pytest.param({"label": 1}, "semantics", id="source-label-conflict"),
    ],
)
def test_serialize_log_rejects_invalid_query_events(changes, message):
    # 参数化验证非 TEST-NET 地址、非法 QTYPE、非 UTC 时间与标签来源冲突的事件被拒绝
    valid_event = gen_log.QueryEvent(
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
        client_ip="192.0.2.1",
        qname="example.com",
        qtype="A",
        label=0,
        source="normal_list",
        algorithm="",
        split="train",
        behavior="routine",
        stable_order=0,
    )

    with pytest.raises(ValueError, match=message):
        gen_log.serialize_log([replace(valid_event, **changes)])


def test_serialize_log_accepts_hard_negative_events():
    # 验证 hard_negative 来源的事件可被序列化且算法列为空
    event = gen_log.QueryEvent(
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
        client_ip="192.0.2.1",
        qname="example.com",
        qtype="A",
        label=0,
        source="hard_negative",
        algorithm="",
        split="train",
        behavior="routine",
        stable_order=0,
    )

    payload = gen_log.serialize_log([event]).decode("utf-8")

    assert ",hard_negative,," in payload


def test_serialize_log_rejects_non_string_qtype_that_compares_equal_to_valid_value():
    # 验证与 A 相等但不是字符串的 qtype 会被拒绝（按类型而非相等性判定）
    class QtypeLikeA:
        def __eq__(self, other):
            return other == "A"

        def __str__(self):
            return "BOGUS"

    valid_event = gen_log.QueryEvent(
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
        client_ip="192.0.2.1",
        qname="example.com",
        qtype="A",
        label=0,
        source="normal_list",
        algorithm="",
        split="train",
        behavior="routine",
        stable_order=0,
    )

    with pytest.raises(ValueError, match="qtype"):
        gen_log.serialize_log([replace(valid_event, qtype=QtypeLikeA())])


def test_serialize_log_rejects_non_string_split_that_compares_equal_to_valid_value():
    # 验证与 train 相等但不是字符串的 split 会被拒绝
    class SplitLikeTrain:
        def __eq__(self, other):
            return other == "train"

        def __hash__(self):
            return hash("train")

        def __str__(self):
            return "INVALID"

    valid_event = gen_log.QueryEvent(
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
        client_ip="192.0.2.1",
        qname="example.com",
        qtype="A",
        label=0,
        source="normal_list",
        algorithm="",
        split="train",
        behavior="routine",
        stable_order=0,
    )

    with pytest.raises(ValueError, match="split"):
        gen_log.serialize_log([replace(valid_event, split=SplitLikeTrain())])


def test_assign_qtypes_rejects_non_string_split_that_compares_equal_to_valid_value(
    tmp_path,
):
    # 验证 assign_qtypes 同样拒绝与 train 相等但非字符串的 split
    class SplitLikeTrain:
        def __eq__(self, other):
            return other == "train"

        def __hash__(self):
            return hash("train")

        def __str__(self):
            return "INVALID"

    config = make_behavior_config(tmp_path)
    event = gen_log.build_behavior_groups(make_behavior_records(), config)[0].events[0]

    with pytest.raises(ValueError, match="split"):
        gen_log.assign_qtypes([replace(event, split=SplitLikeTrain())], config)


def _task5_summary_events():
    # 构造 7 条固定事件（跨三个 split，含一个混合窗口）供摘要测试
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return [
        gen_log.QueryEvent(
            start, "192.0.2.1", "oak-train.com", "A", 0, "normal_list", "",
            "train", "routine", 0,
        ),
        gen_log.QueryEvent(
            start + timedelta(seconds=1), "192.0.2.1", "cobalt-train.net", "AAAA",
            1, "public_dga", "", "train", "high_frequency", 1,
        ),
        gen_log.QueryEvent(
            start + timedelta(seconds=2), "192.0.2.1", "oak-train.com", "TXT", 0,
            "normal_list", "", "train", "burst", 2,
        ),
        gen_log.QueryEvent(
            start + timedelta(days=1), "192.0.2.1", "oak-val.com", "A", 0,
            "normal_list", "", "val", "routine", 3,
        ),
        gen_log.QueryEvent(
            start + timedelta(days=1, seconds=61), "192.0.2.1", "maple-val.org",
            "CNAME", 1, "manual_dga", "word_digit", "val", "word_digit", 4,
        ),
        gen_log.QueryEvent(
            start + timedelta(days=2), "192.0.2.1", "oak-test.com", "A", 0,
            "normal_list", "", "test", "routine", 5,
        ),
        gen_log.QueryEvent(
            start + timedelta(days=2, seconds=1), "192.0.2.1", "cobalt-test.net",
            "AAAA", 1, "public_dga", "", "test", "high_frequency", 6,
        ),
    ]


@pytest.fixture
def task5_summary_case(tmp_path):
    # 提供摘要测试夹具：配置、事件、CSV 字节与输入文件哈希
    config = make_behavior_config(tmp_path, total_rows=7)
    events = _task5_summary_events()
    csv_bytes = gen_log.serialize_log(events)
    input_hashes = {
        "config.json": "a" * 64,
        "data/splits.csv": "b" * 64,
        config.psl_snapshot: "c" * 64,
    }
    return config, events, csv_bytes, input_hashes


def _task5_require_api(*names):
    # 检查 Task 5 所需 API 是否存在，缺失时直接判定失败
    missing = [name for name in names if not hasattr(gen_log, name)]
    if missing:
        pytest.fail(f"Task 5 API missing: {', '.join(missing)}", pytrace=False)


def _task5_build_summary(case):
    # 调用 build_summary 生成摘要，返回摘要、事件与 CSV 字节
    config, events, csv_bytes, input_hashes = case
    _task5_require_api("build_summary", "validate_summary")
    summary = gen_log.build_summary(
        events, input_hashes, hashlib.sha256(csv_bytes).hexdigest(), config
    )
    return summary, events, csv_bytes


def _task5_write_project(tmp_path, total_rows=6000):
    # 在临时目录搭建最小 D3 项目：config.json、splits.csv 与本地 PSL
    project_root = tmp_path / "d3-project"
    (project_root / "data" / "raw").mkdir(parents=True)
    config_data = make_config()
    config_data["log"]["total_rows_approx"] = total_rows
    (project_root / "config.json").write_text(
        json.dumps(config_data), encoding="utf-8"
    )
    write_split_csv(project_root / "data" / "splits.csv", make_split_rows())
    (project_root / "data" / "raw" / "public_suffix_list.dat").write_text(
        "// ===BEGIN ICANN DOMAINS===\ncom\nnet\norg\n"
        "// ===END ICANN DOMAINS===\n",
        encoding="utf-8",
    )
    return project_root


def _task5_block_network(monkeypatch):
    # 把 socket 相关函数替换为直接断言失败，确保测试离线
    def fail_network(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)


def _task5_run_cli(project_root):
    # 借助 sitecustomize 在子进程内阻断网络后运行 gen_log CLI，并检查阻断自检标记
    repository_root = Path(__file__).resolve().parents[1]
    bootstrap_dir = project_root / "_test_bootstrap"
    bootstrap_dir.mkdir(exist_ok=True)
    marker_path = bootstrap_dir / "socket-block-loaded"
    (bootstrap_dir / "sitecustomize.py").write_text(
        "import os\n"
        "import socket\n"
        "def _blocked(*args, **kwargs):\n"
        "    raise AssertionError('network access attempted in CLI test')\n"
        "socket.create_connection = _blocked\n"
        "socket.getaddrinfo = _blocked\n"
        "socket.gethostbyname = _blocked\n"
        "socket.gethostbyname_ex = _blocked\n"
        "socket.gethostbyaddr = _blocked\n"
        "socket.getnameinfo = _blocked\n"
        "socket.getfqdn = _blocked\n"
        "socket.socket.connect = _blocked\n"
        "socket.socket.connect_ex = _blocked\n"
        "send_names = [name for name in dir(socket.socket) if name.startswith('send')]\n"
        "for name in send_names:\n"
        "    setattr(socket.socket, name, _blocked)\n"
        "required = [(socket, 'create_connection'), (socket, 'getaddrinfo'), "
        "(socket, 'gethostbyname'), (socket, 'gethostbyname_ex'), "
        "(socket, 'gethostbyaddr'), (socket, 'getnameinfo'), (socket, 'getfqdn'), "
        "(socket.socket, 'connect'), (socket.socket, 'connect_ex')]\n"
        "required.extend((socket.socket, name) for name in send_names)\n"
        "assert all(getattr(owner, name, None) is _blocked for owner, name in required)\n"
        "with open(os.environ['DNS_DGA_SOCKET_BLOCK_MARKER'], 'w', encoding='ascii') as marker:\n"
        "    marker.write('loaded')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(bootstrap_dir), str(repository_root), existing_pythonpath)
        if value
    )
    environment["DNS_DGA_SOCKET_BLOCK_MARKER"] = str(marker_path)
    result = subprocess.run(
        [sys.executable, "-m", "src.gen_log"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert marker_path.is_file(), (
        "socket-block bootstrap did not complete its self-check:\n" + result.stderr
    )
    assert marker_path.read_text(encoding="ascii") == "loaded"
    return result


def test_task5_summary_reports_counts_and_independently_recomputes_csv(
    task5_summary_case,
):
    # 验证摘要的计数、时间范围与 sha256 正确，并能通过 validate_summary 独立复算 CSV
    summary, events, csv_bytes = _task5_build_summary(task5_summary_case)

    gen_log.validate_summary(summary, events, csv_bytes)

    assert summary.seed == 20261101
    assert summary.target_rows == summary.actual_rows == len(events) == 7
    assert summary.split_counts == {"train": 3, "val": 2, "test": 2}
    assert summary.label_counts == {0: 4, 1: 3}
    assert summary.source_counts == {
        "normal_list": 4,
        "public_dga": 2,
        "manual_dga": 1,
    }
    assert summary.qtype_counts == {"A": 3, "AAAA": 2, "TXT": 1, "CNAME": 1}
    assert summary.behavior_counts == {
        "routine": 3,
        "high_frequency": 2,
        "burst": 1,
        "word_digit": 1,
    }
    assert summary.mixed_window_counts == {"train": 1, "val": 0, "test": 1}
    assert summary.unique_qname_count == 6
    assert summary.aggregate_sample_count == 6
    assert summary.synthetic is True
    assert summary.output_sha256 == hashlib.sha256(csv_bytes).hexdigest()
    assert summary.counts_by_split["train"]["label_counts"] == {0: 2, 1: 1}
    assert summary.counts_by_split["train"]["behavior_counts"] == {
        "routine": 1,
        "high_frequency": 1,
        "burst": 1,
    }
    assert summary.counts_by_split["train"]["mixed_window_count"] == 1
    assert summary.counts_by_split["train"]["unique_qname_count"] == 2
    assert summary.counts_by_split["train"]["aggregate_sample_count"] == 2
    assert summary.counts_by_split["train"]["time_range"] == {
        "start": "2026-11-01T00:00:00.000Z",
        "end": "2026-11-01T00:00:02.000Z",
    }


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("output_sha256", "0" * 64),
        ("actual_rows", 8),
        ("split_counts", {}),
        ("label_counts", {}),
        ("source_counts", {}),
        ("qtype_counts", {}),
        ("counts_by_split", {}),
        ("mixed_window_counts", {}),
        ("unique_qname_count", 7),
        ("aggregate_sample_count", 7),
        ("split_time_ranges", {}),
    ],
)
def test_task5_validation_rejects_stale_csv_reconstructible_summary_fields(
    task5_summary_case, field, wrong_value
):
    # 参数化验证哈希、行数或各类计数与 CSV 可复算结果不符时校验失败
    summary, events, csv_bytes = _task5_build_summary(task5_summary_case)

    with pytest.raises(ValueError):
        gen_log.validate_summary(
            replace(summary, **{field: wrong_value}), events, csv_bytes
        )


def test_task5_validation_rejects_csv_bytes_that_disagree_with_summary(
    task5_summary_case,
):
    # 验证 CSV 内容与摘要不一致（改动域名）时校验失败
    summary, events, csv_bytes = _task5_build_summary(task5_summary_case)
    changed_csv = csv_bytes.replace(b"oak-train.com", b"birch-train.com", 1)

    with pytest.raises(ValueError):
        gen_log.validate_summary(summary, events, changed_csv)


def test_task5_validation_rejects_csv_row_changes_even_when_summary_matches(
    task5_summary_case,
):
    # 验证即使更新摘要哈希，CSV 行与内存事件不符仍会失败
    summary, events, csv_bytes = _task5_build_summary(task5_summary_case)
    original_row_fragment = b",normal_list,,train\n"
    assert original_row_fragment in csv_bytes
    changed_csv = csv_bytes.replace(
        original_row_fragment, b",normal_list,altered,train\n", 1
    )
    changed_summary = replace(
        summary,
        output_sha256=hashlib.sha256(changed_csv).hexdigest(),
    )

    with pytest.raises(ValueError, match="event|in-memory|serialized"):
        gen_log.validate_summary(changed_summary, events, changed_csv)


def test_task5_validation_checks_behavior_only_against_in_memory_events(
    task5_summary_case,
):
    # 验证行为字段只与内存事件比对（CSV 无该列），改动内存事件即失败
    summary, events, csv_bytes = _task5_build_summary(task5_summary_case)
    changed_events = [replace(events[0], behavior="low_frequency"), *events[1:]]

    with pytest.raises(ValueError):
        gen_log.validate_summary(summary, changed_events, csv_bytes)


def test_task5_write_log_preserves_target_and_cleans_temp_on_replace_failure(
    tmp_path, monkeypatch
):
    # 验证 write_log 替换失败时保留原目标文件并清理临时文件
    _task5_require_api("write_log")
    target = tmp_path / "data" / "dns_log.csv"
    target.parent.mkdir()
    target.write_bytes(b"existing bytes\n")

    def fail_replace(_path, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        gen_log.write_log(b"new bytes\n", target)

    assert target.read_bytes() == b"existing bytes\n"
    assert list(target.parent.iterdir()) == [target]


def test_task5_write_log_returns_verified_sha256_after_replace(tmp_path):
    # 验证 write_log 成功后返回所写内容的 sha256，且文件内容一致
    _task5_require_api("write_log")
    target = tmp_path / "data" / "dns_log.csv"
    target.parent.mkdir()
    csv_bytes = b"event_id,qname\n1,example.com\n"

    output_sha256 = gen_log.write_log(csv_bytes, target)

    assert output_sha256 == hashlib.sha256(csv_bytes).hexdigest()
    assert target.read_bytes() == csv_bytes


def test_task5_run_generation_is_offline_repeatable_and_hashes_only_inputs(
    tmp_path, monkeypatch
):
    # 验证 run_log_generation 离线且可重复，输入哈希只覆盖 config、splits 与 PSL 三个文件
    _task5_require_api("run_log_generation")
    _task5_block_network(monkeypatch)
    project_root = _task5_write_project(tmp_path)
    target = project_root / "data" / "dns_log.csv"

    first = gen_log.run_log_generation(project_root)
    first_bytes = target.read_bytes()
    second = gen_log.run_log_generation(project_root)
    second_bytes = target.read_bytes()

    expected_input_paths = {
        "config.json": project_root / "config.json",
        "data/splits.csv": project_root / "data" / "splits.csv",
        "data/raw/public_suffix_list.dat": project_root
        / "data"
        / "raw"
        / "public_suffix_list.dat",
    }
    assert first.input_sha256 == {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in expected_input_paths.items()
    }
    assert first.output_sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert first.actual_rows == first.target_rows == 6000
    assert second.output_sha256 == hashlib.sha256(second_bytes).hexdigest()
    assert first_bytes == second_bytes
    assert first == second
    assert sorted(path.name for path in target.parent.iterdir()) == [
        "dns_log.csv",
        "raw",
        "splits.csv",
    ]


def test_task5_run_generation_does_not_replace_target_when_summary_fails(
    tmp_path, monkeypatch
):
    # 验证摘要校验失败时不替换已有 dns_log.csv，且不留临时文件
    _task5_require_api("run_log_generation", "validate_summary")
    _task5_block_network(monkeypatch)
    project_root = _task5_write_project(tmp_path)
    target = project_root / "data" / "dns_log.csv"
    target.write_bytes(b"sentinel\n")

    def fail_validation(*_args, **_kwargs):
        raise ValueError("summary validation failed")

    monkeypatch.setattr(gen_log, "validate_summary", fail_validation)
    with pytest.raises(ValueError, match="summary validation failed"):
        gen_log.run_log_generation(project_root)

    assert target.read_bytes() == b"sentinel\n"
    assert sorted(path.name for path in target.parent.iterdir()) == [
        "dns_log.csv",
        "raw",
        "splits.csv",
    ]


def test_task5_cli_success_writes_project_local_log_and_only_json_stdout(tmp_path):
    # 验证 CLI 成功时写出 data/dns_log.csv，stdout 仅输出 JSON 摘要
    project_root = _task5_write_project(tmp_path)

    result = _task5_run_cli(project_root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "CLI did not emit a JSON summary"
    summary = json.loads(result.stdout)
    assert summary["synthetic"] is True
    assert summary["actual_rows"] == 6000
    output_path = project_root / "data" / "dns_log.csv"
    assert summary["output_sha256"] == hashlib.sha256(
        output_path.read_bytes()
    ).hexdigest()


def test_task5_cli_failure_is_nonzero_quiet_on_stdout_and_preserves_target(
    tmp_path,
):
    # 验证 CLI 失败时退出码非零、stdout 为空、stderr 有错误信息且不泄露域名、不覆盖既有文件
    project_root = _task5_write_project(tmp_path)
    splits_path = project_root / "data" / "splits.csv"
    splits_path.write_text("incorrect header\n", encoding="utf-8")
    target = project_root / "data" / "dns_log.csv"
    target.write_bytes(b"sentinel\n")

    result = _task5_run_cli(project_root)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()
    assert "normal-train.com" not in result.stderr
    assert target.read_bytes() == b"sentinel\n"
