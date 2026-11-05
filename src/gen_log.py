"""生成模拟 DNS 查询日志（正常 + DGA 混合）。

负责人：组员A（D3）
参考：docs/specs/project-roadmap.md 2.1 日志规模与行为分布、2.2 日志格式

待实现要点：
- 各集合使用独立模拟时段，总量约 50,000 条，主场景约 95% 正常 / 5% DGA
- 行为分布保留重叠：正常含日常访问与合法突发；DGA 含高频随机、低频随机、低熵词根拼接
- 固定 seed、UTC 时间戳与排序规则；相同 seed 可重建相同输入
- 列：event_id,timestamp,client_ip,qname,qtype,label,source,algorithm,split
  （label/source/algorithm/split 为评估元数据，不得进入特征或提示）
- 输出 data/dns_log.csv + 数据统计表（原始行数/唯一域名数/聚合样本数三层比例）
"""

import csv
import hashlib
import ipaddress
import io
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import math
from pathlib import Path
import random
import re
import sys
import tempfile
from types import MappingProxyType

from . import prepare_data
from .gen_dga import ALGORITHMS


_SPLIT_ORDER = ("train", "val", "test")
_SPLIT_HEADER = ("domain", "label", "source", "algorithm", "split")
_LOG_HEADER = (
    "event_id",
    "timestamp",
    "client_ip",
    "qname",
    "qtype",
    "label",
    "source",
    "algorithm",
    "split",
)
_NORMAL_BEHAVIOR_ORDER = ("routine", "burst")
_DGA_BEHAVIOR_ORDER = ("high_frequency", "low_frequency", "word_digit")
_QTYPE_ORDER = ("A", "AAAA", "CNAME", "TXT", "NS")
_WEIGHT_SUM_TOLERANCE = Fraction(1, 1_000_000_000)
_UTC_MILLISECOND_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
_GROUP_SIZE_BOUNDS = MappingProxyType(
    {
        "routine": (1, 2),
        "burst": (3, 12),
        "high_frequency": (5, 12),
        "low_frequency": (1, 2),
        "word_digit": (1, 4),
    }
)
_CLIENT_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def _validate_split_record(record, location):
    if not isinstance(record, prepare_data.DomainRecord):
        raise ValueError(f"{location} must be a DomainRecord")
    if not isinstance(record.domain, str):
        raise ValueError(f"{location} domain must be a string")
    try:
        normalized_domain = prepare_data.normalize_domain(record.domain)
    except ValueError as exc:
        raise ValueError(
            f"invalid domain {record.domain!r} in {location}: {exc}"
        ) from exc
    if normalized_domain != record.domain:
        raise ValueError(
            f"domain {record.domain!r} in {location} is not already normalized"
        )
    if (
        isinstance(record.label, bool)
        or not isinstance(record.label, int)
        or record.label not in (0, 1)
    ):
        raise ValueError(f"invalid label for {record.domain!r} in {location}")
    if not isinstance(record.source, str) or not isinstance(record.algorithm, str):
        raise ValueError(
            f"invalid source or algorithm for {record.domain!r} in {location}"
        )
    if record.source == "normal_list":
        valid_semantics = record.label == 0 and record.algorithm == ""
    elif record.source == "hard_negative":
        valid_semantics = record.label == 0 and record.algorithm == ""
    elif record.source == "public_dga":
        valid_semantics = record.label == 1 and record.algorithm == ""
    elif record.source == "manual_dga":
        valid_semantics = record.label == 1 and record.algorithm in ALGORITHMS
    else:
        valid_semantics = False
    if not valid_semantics:
        raise ValueError(
            f"invalid source/label/algorithm semantics for {record.domain!r} "
            f"in {location}"
        )
    if not isinstance(record.split, str) or record.split not in _SPLIT_ORDER:
        raise ValueError(
            f"unknown split {record.split!r} for {record.domain!r} in {location}"
        )


def read_split_records(splits_path: Path) -> list[prepare_data.DomainRecord]:
    if not isinstance(splits_path, Path):
        raise ValueError("splits_path must be a Path")

    records = []
    seen_domains = set()
    try:
        with splits_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValueError(
                    "splits CSV is empty; expected the contract header"
                ) from exc
            if tuple(header) != _SPLIT_HEADER:
                raise ValueError(
                    "splits CSV header must be exactly " + ",".join(_SPLIT_HEADER)
                )

            for row in reader:
                row_number = reader.line_num
                if len(row) != len(_SPLIT_HEADER):
                    raise ValueError(
                        f"row {row_number} has {len(row)} fields; "
                        f"expected {len(_SPLIT_HEADER)}"
                    )
                if row[1] not in ("0", "1"):
                    raise ValueError(
                        f"invalid label {row[1]!r} in row {row_number}; "
                        "expected exact 0 or 1"
                    )

                record = prepare_data.DomainRecord(
                    domain=row[0],
                    label=int(row[1]),
                    source=row[2],
                    algorithm=row[3],
                    etld_plus_one="",
                    split=row[4],
                    sources=(),
                )
                _validate_split_record(record, f"row {row_number}")
                if record.domain in seen_domains:
                    raise ValueError(
                        f"duplicate domain {record.domain!r} in row {row_number}"
                    )
                seen_domains.add(record.domain)
                records.append(record)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"could not read splits CSV at {splits_path}: {exc}") from exc

    if not records:
        raise ValueError("splits CSV must contain at least one data row")
    return records


def validate_domain_pools(records, psl_path: Path) -> None:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("records must be a sequence of DomainRecord values")

    seen_domains = set()
    labels_by_split = {split: set() for split in _SPLIT_ORDER}
    has_word_digit = set()
    for index, record in enumerate(records, start=1):
        location = f"record {index}"
        _validate_split_record(record, location)
        if record.domain in seen_domains:
            raise ValueError(f"duplicate domain {record.domain!r} in {location}")
        seen_domains.add(record.domain)
        labels_by_split[record.split].add(record.label)
        if (
            record.label == 1
            and record.source == "manual_dga"
            and record.algorithm == "word_digit"
        ):
            has_word_digit.add(record.split)

    for split in _SPLIT_ORDER:
        if labels_by_split[split] != {0, 1}:
            raise ValueError(
                f"split {split!r} must have non-empty label 0 and label 1 pools"
            )
        if split not in has_word_digit:
            raise ValueError(f"split {split!r} has no manual_dga word_digit pool")

    extractor = prepare_data.create_extractor(psl_path)
    split_by_etld_plus_one = {}
    for record in records:
        try:
            extracted = extractor(record.domain)
        except Exception as exc:
            raise ValueError(
                f"could not extract registered domain for {record.domain!r}"
            ) from exc
        domain_part = getattr(extracted, "domain", "")
        suffix_part = getattr(extracted, "suffix", "")
        if not domain_part or not suffix_part:
            raise ValueError(
                f"domain {record.domain!r} has no registered domain (eTLD+1)"
            )
        etld_plus_one = f"{domain_part}.{suffix_part}"
        previous_split = split_by_etld_plus_one.setdefault(
            etld_plus_one, record.split
        )
        if previous_split != record.split:
            raise ValueError(
                f"registered domain (eTLD+1) {etld_plus_one!r} "
                "appears in multiple splits: "
                f"{previous_split!r} and {record.split!r}"
            )


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    seed: int
    window_seconds: int
    psl_snapshot: str
    total_rows_approx: int
    dga_ratio: float
    simulation_start_utc: datetime
    split_period_hours: int
    client_pool_size: int
    active_client_fraction: float
    active_event_fraction: float
    normal_behavior_ratio: Mapping[str, float]
    dga_behavior_ratio: Mapping[str, float]
    mixed_window_fraction: float
    qtype_weights: Mapping[str, float]

    def __post_init__(self):
        for name in (
            "normal_behavior_ratio",
            "dga_behavior_ratio",
            "qtype_weights",
        ):
            object.__setattr__(
                self, name, MappingProxyType(dict(getattr(self, name)))
            )


def _require_integer(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _require_number(value, name, minimum=None, maximum=None, exclusive_min=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None:
        below_minimum = number <= minimum if exclusive_min else number < minimum
        if below_minimum:
            comparison = "greater than" if exclusive_min else "at least"
            raise ValueError(f"{name} must be {comparison} {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return number


def _validate_weight_keys(weights, tie_order):
    if not isinstance(weights, Mapping) or not weights:
        raise ValueError("weights must be a non-empty mapping")
    if any(not isinstance(key, str) for key in weights):
        raise ValueError("weight keys must be strings")
    if not isinstance(tie_order, Sequence) or isinstance(tie_order, (str, bytes)):
        raise ValueError("tie_order must be a sequence of weight keys")
    if any(not isinstance(key, str) for key in tie_order):
        raise ValueError("tie_order keys must be strings")
    ordered_keys = tuple(tie_order)
    if len(ordered_keys) != len(set(ordered_keys)):
        raise ValueError("tie_order must not contain duplicate keys")
    if set(ordered_keys) != set(weights):
        raise ValueError("tie_order must contain every weight key exactly once")
    return ordered_keys


def _fraction_weights(weights, tie_order):
    ordered_keys = _validate_weight_keys(weights, tie_order)
    fraction_weights = {}
    for key, value in weights.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"weight for {key} must be numeric")
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"weight for {key} must be finite")
            weight = Fraction(str(value))
        else:
            weight = Fraction(value)
        if not Fraction(0) <= weight <= Fraction(1):
            raise ValueError(f"weight for {key} must be finite and within [0, 1]")
        fraction_weights[key] = weight

    weight_sum = sum(fraction_weights.values(), Fraction(0))
    if abs(weight_sum - Fraction(1)) > _WEIGHT_SUM_TOLERANCE:
        raise ValueError("weights must sum to 1")
    return fraction_weights


def _exact_largest_remainder(total, weights, tie_order):
    total = _require_integer(total, "total", minimum=0)
    ordered_keys = _validate_weight_keys(weights, tie_order)
    fraction_weights = {}
    for key, value in weights.items():
        if isinstance(value, bool):
            raise ValueError(f"weight for {key} must be a non-negative rational")
        if isinstance(value, Fraction):
            weight = value
        elif isinstance(value, int):
            weight = Fraction(value)
        else:
            raise ValueError(f"weight for {key} must be a non-negative rational")
        if weight < 0:
            raise ValueError(f"weight for {key} must be non-negative")
        fraction_weights[key] = weight

    weight_sum = sum(fraction_weights.values(), Fraction(0))
    if weight_sum <= 0:
        raise ValueError("weights must have a positive sum")
    quotas = {
        key: Fraction(total) * weight / weight_sum
        for key, weight in fraction_weights.items()
    }
    result = {
        key: quota.numerator // quota.denominator for key, quota in quotas.items()
    }
    remainders = {key: quotas[key] - result[key] for key in quotas}
    remaining = total - sum(result.values())
    order_index = {key: index for index, key in enumerate(ordered_keys)}
    remainder_order = sorted(
        fraction_weights,
        key=lambda key: (-remainders[key], order_index[key]),
    )
    for key in remainder_order[:remaining]:
        result[key] += 1
    return result


def largest_remainder(
    total: int, weights: Mapping[str, float], tie_order: Sequence[str]
) -> dict[str, int]:
    total = _require_integer(total, "total", minimum=0)
    fraction_weights = _fraction_weights(weights, tie_order)
    return _exact_largest_remainder(total, fraction_weights, tie_order)


def allocate_event_quotas(
    split_domain_counts: Mapping[str, int], total_rows: int, dga_ratio: float
) -> dict[str, dict[str, int]]:
    total_rows = _require_integer(total_rows, "total_rows", minimum=1)
    if not isinstance(split_domain_counts, Mapping):
        raise ValueError("split_domain_counts must be a mapping")
    if set(split_domain_counts) != set(_SPLIT_ORDER):
        raise ValueError("split_domain_counts must contain train, val, and test")

    counts = {
        split: _require_integer(
            split_domain_counts[split], f"split_domain_counts[{split}]", minimum=0
        )
        for split in _SPLIT_ORDER
    }
    count_total = sum(counts.values())
    if count_total == 0:
        raise ValueError("split_domain_counts must have a positive sum")

    ratio = _require_number(dga_ratio, "dga_ratio", minimum=0, maximum=1)
    split_totals = _exact_largest_remainder(total_rows, counts, _SPLIT_ORDER)
    ratio_fraction = Fraction(str(ratio))
    dga_total = (
        2 * total_rows * ratio_fraction.numerator + ratio_fraction.denominator
    ) // (2 * ratio_fraction.denominator)
    split_dga = _exact_largest_remainder(dga_total, split_totals, _SPLIT_ORDER)

    return {
        split: {
            "total": split_totals[split],
            "normal": split_totals[split] - split_dga[split],
            "dga": split_dga[split],
        }
        for split in _SPLIT_ORDER
    }


def _require_object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _required(mapping, key, name):
    if key not in mapping:
        raise ValueError(f"missing configuration value: {name}")
    return mapping[key]


def _load_weights(value, name, expected_keys):
    value = _require_object(value, name)
    if set(value) != set(expected_keys):
        raise ValueError(f"{name} must contain exactly {', '.join(expected_keys)}")
    weights = {
        key: _require_number(value[key], f"{name}.{key}", minimum=0, maximum=1)
        for key in expected_keys
    }
    if not math.isclose(math.fsum(weights.values()), 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"{name} weights must sum to 1")
    return MappingProxyType(weights)


def _reject_json_constant(_value):
    raise ValueError("config contains a non-standard numeric value")


def _load_simulation_start(value):
    if not isinstance(value, str) or not _UTC_MILLISECOND_TIMESTAMP.fullmatch(value):
        raise ValueError(
            "simulation_start_utc must use canonical UTC millisecond precision"
        )
    try:
        start = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("simulation_start_utc must be a valid UTC timestamp") from exc
    if start.second != 0 or start.microsecond != 0:
        raise ValueError("simulation_start_utc must be aligned to a UTC minute")
    return start.astimezone(timezone.utc)


def load_config(config_path: Path) -> GeneratorConfig:
    if not isinstance(config_path, Path):
        raise ValueError("config_path must be a Path")
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw_config = json.load(stream, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot load valid configuration from {config_path}") from exc

    raw_config = _require_object(raw_config, "config")
    log = _require_object(_required(raw_config, "log", "log"), "log")
    paths = _require_object(_required(raw_config, "paths", "paths"), "paths")

    seed = _require_integer(_required(raw_config, "seed", "seed"), "seed", 0)
    window_seconds = _require_integer(
        _required(raw_config, "window_seconds", "window_seconds"),
        "window_seconds",
        1,
    )
    if window_seconds != 60:
        raise ValueError("window_seconds must be 60 for D3 log generation")

    psl_snapshot = _required(paths, "psl_snapshot", "paths.psl_snapshot")
    if not isinstance(psl_snapshot, str) or not psl_snapshot.strip():
        raise ValueError("paths.psl_snapshot must be a non-empty string")

    total_rows = _require_integer(
        _required(log, "total_rows_approx", "log.total_rows_approx"),
        "log.total_rows_approx",
        1,
    )
    dga_ratio = _require_number(
        _required(log, "dga_ratio", "log.dga_ratio"),
        "log.dga_ratio",
        0,
        1,
    )
    split_period_hours = _require_integer(
        _required(log, "split_period_hours", "log.split_period_hours"),
        "log.split_period_hours",
        1,
    )
    client_pool_size = _require_integer(
        _required(log, "client_pool_size", "log.client_pool_size"),
        "log.client_pool_size",
        1,
    )
    active_client_fraction = _require_number(
        _required(log, "active_client_fraction", "log.active_client_fraction"),
        "log.active_client_fraction",
        0,
        1,
        exclusive_min=True,
    )
    active_event_fraction = _require_number(
        _required(log, "active_event_fraction", "log.active_event_fraction"),
        "log.active_event_fraction",
        0,
        1,
        exclusive_min=True,
    )
    mixed_window_fraction = _require_number(
        _required(log, "mixed_window_fraction", "log.mixed_window_fraction"),
        "log.mixed_window_fraction",
        0,
        1,
        exclusive_min=True,
    )
    normal_behavior_ratio = _load_weights(
        _required(log, "normal_behavior_ratio", "log.normal_behavior_ratio"),
        "log.normal_behavior_ratio",
        _NORMAL_BEHAVIOR_ORDER,
    )
    dga_behavior_ratio = _load_weights(
        _required(log, "dga_behavior_ratio", "log.dga_behavior_ratio"),
        "log.dga_behavior_ratio",
        _DGA_BEHAVIOR_ORDER,
    )
    qtype_weights = _load_weights(
        _required(log, "qtype_weights", "log.qtype_weights"),
        "log.qtype_weights",
        _QTYPE_ORDER,
    )
    simulation_start_utc = _load_simulation_start(
        _required(log, "simulation_start_utc", "log.simulation_start_utc")
    )

    return GeneratorConfig(
        seed=seed,
        window_seconds=window_seconds,
        psl_snapshot=psl_snapshot,
        total_rows_approx=total_rows,
        dga_ratio=dga_ratio,
        simulation_start_utc=simulation_start_utc,
        split_period_hours=split_period_hours,
        client_pool_size=client_pool_size,
        active_client_fraction=active_client_fraction,
        active_event_fraction=active_event_fraction,
        normal_behavior_ratio=normal_behavior_ratio,
        dga_behavior_ratio=dga_behavior_ratio,
        mixed_window_fraction=mixed_window_fraction,
        qtype_weights=qtype_weights,
    )


@dataclass(frozen=True, slots=True)
class EventDraft:
    record: prepare_data.DomainRecord
    split: str
    label: int
    source: str
    algorithm: str
    behavior: str
    qname: str
    group_id: str
    placement_unit_id: str
    client_ip: str
    window_start: datetime
    offset_ms: int
    sequence_index: int


@dataclass(frozen=True, slots=True)
class QueryEvent:
    timestamp: datetime
    client_ip: str
    qname: str
    qtype: str
    label: int
    source: str
    algorithm: str
    split: str
    behavior: str
    stable_order: int


@dataclass(frozen=True, slots=True)
class BehaviorGroup:
    domain: prepare_data.DomainRecord
    split: str
    label: int
    source: str
    algorithm: str
    behavior: str
    qname: str
    group_id: str
    placement_unit_id: str
    paired_group_id: str | None
    client_ip: str
    window_start: datetime
    events: tuple[EventDraft, ...]


@dataclass(slots=True)
class _GroupSpec:
    record: prepare_data.DomainRecord
    behavior: str
    group_id: str
    placement_unit_id: str
    size: int
    paired_group_id: str | None = None
    offsets: tuple[int, ...] = ()
    client_ip: str = ""
    window_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PlacementUnit:
    split: str
    unit_id: str
    groups: tuple[_GroupSpec, ...]

    @property
    def row_count(self):
        return sum(group.size for group in self.groups)


def derive_rng(
    seed: int, split: str, label: int, behavior: str, purpose: str
) -> random.Random:
    seed = _require_integer(seed, "seed", minimum=0)
    if split not in _SPLIT_ORDER:
        raise ValueError(f"unknown split: {split!r}")
    label = _require_integer(label, "label", minimum=0)
    if label not in (0, 1):
        raise ValueError("label must be 0 or 1")
    if not isinstance(behavior, str) or not behavior:
        raise ValueError("behavior must be a non-empty string")
    if not isinstance(purpose, str) or not purpose:
        raise ValueError("purpose must be a non-empty string")
    material = f"{seed}|d3|{split}|{label}|{behavior}|{purpose}".encode("utf-8")
    child_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return random.Random(child_seed)


def partition_event_groups(
    event_count: int,
    min_size: int,
    max_size: int,
    rng: random.Random,
) -> list[int]:
    event_count = _require_integer(event_count, "event_count", minimum=0)
    min_size = _require_integer(min_size, "min_size", minimum=1)
    max_size = _require_integer(max_size, "max_size", minimum=1)
    if max_size < min_size:
        raise ValueError("max_size must be at least min_size")
    if not isinstance(rng, random.Random):
        raise ValueError("rng must be a random.Random instance")
    if event_count == 0:
        return []

    group_count = math.ceil(event_count / max_size)
    if event_count < group_count * min_size:
        raise ValueError(
            f"event count {event_count} is infeasible for group bounds "
            f"[{min_size}, {max_size}]"
        )

    sizes = []
    remaining = event_count
    for index in range(group_count):
        groups_after = group_count - index - 1
        if groups_after == 0:
            size = remaining
        else:
            lower = max(min_size, remaining - groups_after * max_size)
            upper = min(max_size, remaining - groups_after * min_size)
            if lower > upper:
                raise ValueError("event count cannot be partitioned within group bounds")
            size = rng.randint(lower, upper)
        sizes.append(size)
        remaining -= size
    return sizes


def allocate_behavior_quotas(
    event_quotas: Mapping[str, Mapping[str, int]],
    normal_behavior_ratio: Mapping[str, float],
    dga_behavior_ratio: Mapping[str, float],
) -> dict[str, dict[str, dict[str, int]]]:
    if not isinstance(event_quotas, Mapping) or set(event_quotas) != set(_SPLIT_ORDER):
        raise ValueError("event_quotas must contain train, val, and test")
    quotas = {}
    for split in _SPLIT_ORDER:
        split_quota = event_quotas[split]
        if not isinstance(split_quota, Mapping) or set(split_quota) != {
            "total",
            "normal",
            "dga",
        }:
            raise ValueError(f"event quota for {split!r} must contain total, normal, dga")
        total = _require_integer(split_quota["total"], f"{split}.total", minimum=0)
        normal_total = _require_integer(
            split_quota["normal"], f"{split}.normal", minimum=0
        )
        dga_total = _require_integer(split_quota["dga"], f"{split}.dga", minimum=0)
        if total != normal_total + dga_total:
            raise ValueError(f"event quota for {split!r} does not conserve total")
        quotas[split] = {
            "normal": largest_remainder(
                normal_total, normal_behavior_ratio, _NORMAL_BEHAVIOR_ORDER
            ),
            "dga": largest_remainder(
                dga_total, dga_behavior_ratio, _DGA_BEHAVIOR_ORDER
            ),
        }
    return quotas


def allocate_qtype_quotas(
    split_label_counts: Mapping[str, Mapping[int, int]],
    qtype_weights: Mapping[str, float],
) -> dict[str, dict[int, dict[str, int]]]:
    if not isinstance(split_label_counts, Mapping) or set(split_label_counts) != set(
        _SPLIT_ORDER
    ):
        raise ValueError("split_label_counts must contain train, val, and test")

    quotas = {}
    for split in _SPLIT_ORDER:
        label_counts = split_label_counts[split]
        if (
            not isinstance(label_counts, Mapping)
            or any(
                isinstance(label, bool) or not isinstance(label, int)
                for label in label_counts
            )
            or set(label_counts) != {0, 1}
        ):
            raise ValueError(f"label counts for {split!r} must contain labels 0 and 1")
        quotas[split] = {
            label: largest_remainder(
                _require_integer(
                    label_counts[label], f"split_label_counts[{split}][{label}]", 0
                ),
                qtype_weights,
                _QTYPE_ORDER,
            )
            for label in (0, 1)
        }
    return quotas


def assign_qtypes(
    events: Sequence[EventDraft], config: GeneratorConfig
) -> list[str]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("events must be a sequence of EventDraft values")
    if not isinstance(config, GeneratorConfig):
        raise ValueError("config must be a GeneratorConfig")

    buckets = {
        (split, label): []
        for split in _SPLIT_ORDER
        for label in (0, 1)
    }
    for index, event in enumerate(events):
        if not isinstance(event, EventDraft):
            raise ValueError(f"event {index + 1} must be an EventDraft")
        if not isinstance(event.split, str) or event.split not in _SPLIT_ORDER:
            raise ValueError(f"event {index + 1} has unknown split {event.split!r}")
        if (
            isinstance(event.label, bool)
            or not isinstance(event.label, int)
            or event.label not in (0, 1)
        ):
            raise ValueError(f"event {index + 1} has an invalid label")
        buckets[(event.split, event.label)].append(index)

    counts = {
        split: {
            label: len(buckets[(split, label)])
            for label in (0, 1)
        }
        for split in _SPLIT_ORDER
    }
    quotas = allocate_qtype_quotas(counts, config.qtype_weights)
    assignments = [""] * len(events)
    for split in _SPLIT_ORDER:
        for label in (0, 1):
            indexes = buckets[(split, label)]
            qtypes = [
                qtype
                for qtype in _QTYPE_ORDER
                for _ in range(quotas[split][label][qtype])
            ]
            derive_rng(config.seed, split, label, "qtype", "assignment").shuffle(
                qtypes
            )
            stable_indexes = sorted(
                indexes,
                key=lambda index: (
                    events[index].group_id,
                    events[index].sequence_index,
                ),
            )
            for index, qtype in zip(stable_indexes, qtypes):
                assignments[index] = qtype
    return assignments


def _client_pool(pool_size: int) -> tuple[str, ...]:
    pool_size = _require_integer(pool_size, "client_pool_size", minimum=1)
    available = tuple(str(address) for network in _CLIENT_NETWORKS for address in network.hosts())
    if pool_size > len(available):
        raise ValueError(f"client_pool_size cannot exceed {len(available)} TEST-NET addresses")
    return available[:pool_size]


def _fraction_floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _fraction_ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _active_client_count(pool_size: int, active_client_fraction: float) -> int:
    pool_size = _require_integer(pool_size, "client_pool_size", minimum=0)
    fraction = Fraction(
        str(
            _require_number(
                active_client_fraction,
                "active_client_fraction",
                minimum=0,
                maximum=1,
            )
        )
    )
    return _fraction_floor(pool_size * fraction + Fraction(1, 2))


def _active_event_share_within_tolerance(
    active_rows: int, total_rows: int, active_event_fraction: float
) -> bool:
    active_rows = _require_integer(active_rows, "active_rows", minimum=0)
    total_rows = _require_integer(total_rows, "total_rows", minimum=1)
    if active_rows > total_rows:
        raise ValueError("active_rows cannot exceed total_rows")
    target = Fraction(
        str(
            _require_number(
                active_event_fraction,
                "active_event_fraction",
                minimum=0,
                maximum=1,
            )
        )
    )
    return abs(Fraction(active_rows, total_rows) - target) <= Fraction(2, 100)


def _active_unit_ids(
    units: Sequence[_PlacementUnit],
    active_event_fraction: float,
    rng: random.Random,
) -> set[str]:
    row_count = sum(unit.row_count for unit in units)
    fraction = Fraction(str(active_event_fraction))
    tolerance = Fraction(2, 100)
    lower = max(Fraction(0), fraction - tolerance) * row_count
    upper = min(Fraction(1), fraction + tolerance) * row_count
    lower_count = _fraction_ceil(lower)
    upper_count = _fraction_floor(upper)
    target_count = _fraction_floor(fraction * row_count + Fraction(1, 2))

    buckets = {}
    for unit in units:
        buckets.setdefault(unit.row_count, []).append(unit)
    reachable = 1
    bundles = []
    for weight in sorted(buckets):
        bucket = buckets[weight]
        rng.shuffle(bucket)
        remaining = len(bucket)
        bundle_size = 1
        while remaining:
            count = min(bundle_size, remaining)
            bundle_weight = weight * count
            previous = reachable
            reachable |= reachable << bundle_weight
            bundles.append((previous, bundle_weight, weight, count))
            remaining -= count
            bundle_size *= 2

    chosen_count = None
    max_distance = max(target_count - lower_count, upper_count - target_count)
    for distance in range(max_distance + 1):
        below = target_count - distance
        if lower_count <= below <= upper_count and reachable & (1 << below):
            chosen_count = below
            break
        above = target_count + distance
        if above != below and lower_count <= above <= upper_count and reachable & (1 << above):
            chosen_count = above
            break
    if chosen_count is None:
        raise ValueError(
            "active_event_fraction cannot be met within two percentage points "
            "using whole behavior groups"
        )

    remaining_count = chosen_count
    selected_by_weight = {}
    for previous, bundle_weight, weight, count in reversed(bundles):
        if previous & (1 << remaining_count):
            continue
        if remaining_count < bundle_weight or not previous & (1 << (remaining_count - bundle_weight)):
            raise ValueError("could not reconstruct active-client group allocation")
        selected_by_weight[weight] = selected_by_weight.get(weight, 0) + count
        remaining_count -= bundle_weight
    if remaining_count != 0:
        raise ValueError("could not reconstruct active-client row total")

    selected = set()
    for weight, bucket in buckets.items():
        selected.update(unit.unit_id for unit in bucket[: selected_by_weight.get(weight, 0)])
    return selected


def _place_units(
    units: Sequence[_PlacementUnit],
    clients: Sequence[str],
    window_count: int,
    split_index: int,
    config: GeneratorConfig,
    client_rng: random.Random,
    window_rng: random.Random,
) -> None:
    if units and not clients:
        raise ValueError("no clients available for behavior-group placement")
    if len(units) > len(clients) * window_count:
        raise ValueError("client pool and split period cannot hold all behavior groups")

    ordered_units = list(units)
    client_rng.shuffle(ordered_units)
    occupied = {client: set() for client in clients}
    for unit in ordered_units:
        least_used = min(len(windows) for windows in occupied.values())
        candidates = [
            client
            for client, windows in occupied.items()
            if len(windows) == least_used
        ]
        client = client_rng.choice(candidates)
        first_window = window_rng.randrange(window_count)
        for delta in range(window_count):
            window_index = (first_window + delta) % window_count
            if window_index not in occupied[client]:
                break
        else:
            raise ValueError(f"client {client} has no free minute window")
        occupied[client].add(window_index)
        window_start = config.simulation_start_utc + timedelta(
            hours=split_index * config.split_period_hours,
            minutes=window_index,
        )
        for group in unit.groups:
            group.client_ip = client
            group.window_start = window_start


def _draw_adjacent_gaps(rng: random.Random, gap_count: int) -> tuple[int, ...]:
    while True:
        gaps = tuple(rng.randint(1000, 5000) for _ in range(gap_count))
        if sum(gaps) <= 55000:
            return gaps


def _assign_unit_offsets(
    unit: _PlacementUnit, config: GeneratorConfig
) -> None:
    if len(unit.groups) == 2:
        high_group = next(group for group in unit.groups if group.behavior == "high_frequency")
        routine_group = next(group for group in unit.groups if group.behavior == "routine")
        rng = derive_rng(
            config.seed,
            unit.split,
            high_group.record.label,
            high_group.behavior,
            f"offset:{unit.unit_id}",
        )
        gaps = _draw_adjacent_gaps(rng, high_group.size)
        offsets = [rng.randint(0, 59999 - sum(gaps))]
        for gap in gaps:
            offsets.append(offsets[-1] + gap)
        high_group.offsets = tuple(offsets[:-1])
        routine_group.offsets = (offsets[-1],)
        return

    group = unit.groups[0]
    rng = derive_rng(
        config.seed,
        unit.split,
        group.record.label,
        group.behavior,
        f"offset:{group.group_id}",
    )
    if group.behavior in ("burst", "high_frequency"):
        gaps = _draw_adjacent_gaps(rng, group.size - 1)
        offsets = [rng.randint(0, 59999 - sum(gaps))]
        for gap in gaps:
            offsets.append(offsets[-1] + gap)
        group.offsets = tuple(offsets)
    else:
        group.offsets = tuple(sorted(rng.sample(range(60000), group.size)))


def build_behavior_groups(
    records: Sequence[prepare_data.DomainRecord], config: GeneratorConfig
) -> list[BehaviorGroup]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("records must be a sequence of DomainRecord values")
    if not isinstance(config, GeneratorConfig):
        raise ValueError("config must be a GeneratorConfig")
    if config.window_seconds != 60:
        raise ValueError("window_seconds must be 60 for D3 behavior groups")
    if (
        not isinstance(config.simulation_start_utc, datetime)
        or config.simulation_start_utc.tzinfo is None
        or config.simulation_start_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("simulation_start_utc must be timezone-aware UTC")
    if config.simulation_start_utc.second or config.simulation_start_utc.microsecond:
        raise ValueError("simulation_start_utc must be aligned to a UTC minute")
    split_period_hours = _require_integer(
        config.split_period_hours, "split_period_hours", minimum=1
    )
    total_rows = _require_integer(config.total_rows_approx, "total_rows_approx", minimum=1)
    active_client_fraction = _require_number(
        config.active_client_fraction,
        "active_client_fraction",
        minimum=0,
        maximum=1,
        exclusive_min=True,
    )
    active_event_fraction = _require_number(
        config.active_event_fraction,
        "active_event_fraction",
        minimum=0,
        maximum=1,
        exclusive_min=True,
    )
    mixed_window_fraction = _require_number(
        config.mixed_window_fraction,
        "mixed_window_fraction",
        minimum=0,
        maximum=1,
        exclusive_min=True,
    )

    records_by_pool = {
        (split, label): []
        for split in _SPLIT_ORDER
        for label in (0, 1)
    }
    word_digit_pools = {split: [] for split in _SPLIT_ORDER}
    seen_domains = set()
    for index, record in enumerate(records, start=1):
        _validate_split_record(record, f"record {index}")
        if record.domain in seen_domains:
            raise ValueError(f"duplicate domain {record.domain!r} in record {index}")
        seen_domains.add(record.domain)
        records_by_pool[(record.split, record.label)].append(record)
        if (
            record.label == 1
            and record.source == "manual_dga"
            and record.algorithm == "word_digit"
        ):
            word_digit_pools[record.split].append(record)

    for split in _SPLIT_ORDER:
        for label in (0, 1):
            pool = records_by_pool[(split, label)]
            if not pool:
                raise ValueError(f"split {split!r} has no label {label} domain pool")
            pool.sort(key=lambda record: record.domain)
        if not word_digit_pools[split]:
            raise ValueError(f"split {split!r} has no manual_dga word_digit pool")

    split_domain_counts = {
        split: sum(
            len(records_by_pool[(split, label)]) for label in (0, 1)
        )
        for split in _SPLIT_ORDER
    }
    event_quotas = allocate_event_quotas(
        split_domain_counts, total_rows, config.dga_ratio
    )
    for split, quota in event_quotas.items():
        if quota["normal"] == 0 or quota["dga"] == 0:
            raise ValueError(f"split {split!r} must receive both event labels")
    behavior_quotas = allocate_behavior_quotas(
        event_quotas, config.normal_behavior_ratio, config.dga_behavior_ratio
    )
    client_addresses = _client_pool(config.client_pool_size)
    active_client_count = _active_client_count(
        len(client_addresses), active_client_fraction
    )
    if active_client_count > len(client_addresses):
        raise ValueError("active client count exceeds the client pool")
    active_clients = client_addresses[:active_client_count]
    ordinary_clients = client_addresses[active_client_count:]
    window_count = split_period_hours * 60

    all_groups_by_split = {}
    for split in _SPLIT_ORDER:
        quotas = behavior_quotas[split]
        group_specs = []
        high_sizes = partition_event_groups(
            quotas["dga"]["high_frequency"],
            *_GROUP_SIZE_BOUNDS["high_frequency"],
            derive_rng(config.seed, split, 1, "high_frequency", "grouping"),
        )
        if not high_sizes:
            raise ValueError(f"split {split!r} cannot form a mixed-label window")
        mixed_count = _fraction_ceil(
            Fraction(len(high_sizes)) * Fraction(str(mixed_window_fraction))
        )
        if mixed_count < 1:
            mixed_count = 1
        if quotas["normal"]["routine"] < mixed_count:
            raise ValueError(
                f"split {split!r} lacks routine singleton events for mixed windows"
            )
        mixed_indices = sorted(
            derive_rng(
                config.seed,
                split,
                1,
                "high_frequency",
                "grouping:mixed_selection",
            ).sample(range(len(high_sizes)), mixed_count)
        )
        paired_ids_by_high = {
            index: f"{split}:routine:paired:{index:06d}" for index in mixed_indices
        }
        high_domain_rng = derive_rng(
            config.seed, split, 1, "high_frequency", "domains"
        )
        high_specs = []
        for index, size in enumerate(high_sizes):
            group_id = f"{split}:high_frequency:{index:06d}"
            placement_unit_id = (
                f"{split}:mixed:{index}" if index in paired_ids_by_high else group_id
            )
            paired_group_id = paired_ids_by_high.get(index)
            record = high_domain_rng.choice(records_by_pool[(split, 1)])
            high_specs.append(
                _GroupSpec(
                    record=record,
                    behavior="high_frequency",
                    group_id=group_id,
                    placement_unit_id=placement_unit_id,
                    size=size,
                    paired_group_id=paired_group_id,
                )
            )
        group_specs.extend(high_specs)

        routine_domain_rng = derive_rng(config.seed, split, 0, "routine", "domains")
        routine_groups = []
        for index in mixed_indices:
            high_group = high_specs[index]
            routine_id = paired_ids_by_high[index]
            routine_group = _GroupSpec(
                record=routine_domain_rng.choice(records_by_pool[(split, 0)]),
                behavior="routine",
                group_id=routine_id,
                placement_unit_id=high_group.placement_unit_id,
                size=1,
                paired_group_id=high_group.group_id,
            )
            routine_groups.append(routine_group)
        routine_sizes = partition_event_groups(
            quotas["normal"]["routine"] - mixed_count,
            *_GROUP_SIZE_BOUNDS["routine"],
            derive_rng(config.seed, split, 0, "routine", "grouping"),
        )
        for index, size in enumerate(routine_sizes):
            routine_groups.append(
                _GroupSpec(
                    record=routine_domain_rng.choice(records_by_pool[(split, 0)]),
                    behavior="routine",
                    group_id=f"{split}:routine:{index:06d}",
                    placement_unit_id=f"{split}:routine:{index:06d}",
                    size=size,
                )
            )
        group_specs.extend(routine_groups)

        for behavior in ("burst", "low_frequency", "word_digit"):
            label = 0 if behavior == "burst" else 1
            domain_pool = (
                records_by_pool[(split, 0)]
                if behavior == "burst"
                else word_digit_pools[split]
                if behavior == "word_digit"
                else records_by_pool[(split, 1)]
            )
            sizes = partition_event_groups(
                quotas["normal"][behavior]
                if label == 0
                else quotas["dga"][behavior],
                *_GROUP_SIZE_BOUNDS[behavior],
                derive_rng(config.seed, split, label, behavior, "grouping"),
            )
            domain_rng = derive_rng(config.seed, split, label, behavior, "domains")
            for index, size in enumerate(sizes):
                group_id = f"{split}:{behavior}:{index:06d}"
                group_specs.append(
                    _GroupSpec(
                        record=domain_rng.choice(domain_pool),
                        behavior=behavior,
                        group_id=group_id,
                        placement_unit_id=group_id,
                        size=size,
                    )
                )

        group_specs.sort(
            key=lambda group: (
                (_NORMAL_BEHAVIOR_ORDER + _DGA_BEHAVIOR_ORDER).index(group.behavior),
                group.group_id,
            )
        )
        units_by_id = {}
        for group in group_specs:
            units_by_id.setdefault(group.placement_unit_id, []).append(group)
        units = [
            _PlacementUnit(split, unit_id, tuple(unit_groups))
            for unit_id, unit_groups in sorted(units_by_id.items())
        ]
        split_event_count = sum(unit.row_count for unit in units)
        client_rng = derive_rng(config.seed, split, 0, "client_assignment", "client")
        if active_client_count == 0:
            if not _active_event_share_within_tolerance(
                0, split_event_count, active_event_fraction
            ):
                raise ValueError("active client fraction rounds to zero clients")
            selected_active_ids = set()
        elif not ordinary_clients:
            if not _active_event_share_within_tolerance(
                split_event_count, split_event_count, active_event_fraction
            ):
                raise ValueError("client pool has no ordinary clients for requested event share")
            selected_active_ids = {unit.unit_id for unit in units}
        else:
            selected_active_ids = _active_unit_ids(
                units, active_event_fraction, client_rng
            )
        active_units = [unit for unit in units if unit.unit_id in selected_active_ids]
        ordinary_units = [unit for unit in units if unit.unit_id not in selected_active_ids]
        active_rows = sum(unit.row_count for unit in active_units)
        if units and not _active_event_share_within_tolerance(
            active_rows, split_event_count, active_event_fraction
        ):
            raise ValueError(
                f"split {split!r} active-client row share is infeasible within two percentage points"
            )
        window_rng = derive_rng(config.seed, split, 0, "window_assignment", "window")
        _place_units(
            active_units,
            active_clients,
            window_count,
            _SPLIT_ORDER.index(split),
            config,
            client_rng,
            window_rng,
        )
        _place_units(
            ordinary_units,
            ordinary_clients,
            window_count,
            _SPLIT_ORDER.index(split),
            config,
            client_rng,
            window_rng,
        )
        for unit in units:
            _assign_unit_offsets(unit, config)
        all_groups_by_split[split] = group_specs

    result = []
    for split in _SPLIT_ORDER:
        for group in all_groups_by_split[split]:
            if group.window_start is None or not group.client_ip:
                raise ValueError(f"behavior group {group.group_id!r} was not placed")
            events = tuple(
                EventDraft(
                    record=group.record,
                    split=group.record.split,
                    label=group.record.label,
                    source=group.record.source,
                    algorithm=group.record.algorithm,
                    behavior=group.behavior,
                    qname=group.record.domain,
                    group_id=group.group_id,
                    placement_unit_id=group.placement_unit_id,
                    client_ip=group.client_ip,
                    window_start=group.window_start,
                    offset_ms=offset,
                    sequence_index=index,
                )
                for index, offset in enumerate(group.offsets)
            )
            if len(events) != group.size:
                raise ValueError(f"behavior group {group.group_id!r} lost events")
            result.append(
                BehaviorGroup(
                    domain=group.record,
                    split=group.record.split,
                    label=group.record.label,
                    source=group.record.source,
                    algorithm=group.record.algorithm,
                    behavior=group.behavior,
                    qname=group.record.domain,
                    group_id=group.group_id,
                    placement_unit_id=group.placement_unit_id,
                    paired_group_id=group.paired_group_id,
                    client_ip=group.client_ip,
                    window_start=group.window_start,
                    events=events,
                )
            )
    if len(flattened := [event for group in result for event in group.events]) != total_rows:
        raise ValueError("behavior groups do not conserve the target event count")
    return result


def build_events(
    records: Sequence[prepare_data.DomainRecord], config: GeneratorConfig
) -> list[QueryEvent]:
    groups = build_behavior_groups(records, config)
    drafts = [event for group in groups for event in group.events]
    qtypes = assign_qtypes(drafts, config)
    events = [
        QueryEvent(
            timestamp=draft.window_start + timedelta(milliseconds=draft.offset_ms),
            client_ip=draft.client_ip,
            qname=draft.qname,
            qtype=qtype,
            label=draft.label,
            source=draft.source,
            algorithm=draft.algorithm,
            split=draft.split,
            behavior=draft.behavior,
            stable_order=index,
        )
        for index, (draft, qtype) in enumerate(zip(drafts, qtypes))
    ]
    events.sort(key=lambda event: (event.timestamp, event.stable_order))
    return events


def _validate_query_event(event, location):
    if not isinstance(event, QueryEvent):
        raise ValueError(f"{location} must be a QueryEvent")
    if (
        not isinstance(event.timestamp, datetime)
        or event.timestamp.tzinfo is None
        or event.timestamp.utcoffset() != timedelta(0)
        or event.timestamp.microsecond % 1000 != 0
    ):
        raise ValueError(
            f"{location} timestamp must be UTC with millisecond precision"
        )
    if not isinstance(event.stable_order, int) or isinstance(event.stable_order, bool):
        raise ValueError(f"{location} stable_order must be an integer")
    if event.stable_order < 0:
        raise ValueError(f"{location} stable_order must be non-negative")
    if not isinstance(event.client_ip, str):
        raise ValueError(f"{location} client_ip must be an IPv4 string")
    try:
        address = ipaddress.IPv4Address(event.client_ip)
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"{location} client_ip must be an IPv4 string") from exc
    if str(address) != event.client_ip:
        raise ValueError(f"{location} client_ip must use canonical IPv4 notation")
    if not any(address in network for network in _CLIENT_NETWORKS):
        raise ValueError(f"{location} client_ip must belong to an RFC 5737 TEST-NET")
    if not isinstance(event.qname, str):
        raise ValueError(f"{location} qname must be a normalized domain")
    try:
        normalized_qname = prepare_data.normalize_domain(event.qname)
    except ValueError as exc:
        raise ValueError(f"{location} qname is invalid: {exc}") from exc
    if normalized_qname != event.qname:
        raise ValueError(f"{location} qname must already be normalized")
    if not isinstance(event.qtype, str) or event.qtype not in _QTYPE_ORDER:
        raise ValueError(f"{location} qtype must be one of {_QTYPE_ORDER}")
    if (
        isinstance(event.label, bool)
        or not isinstance(event.label, int)
        or event.label not in (0, 1)
    ):
        raise ValueError(f"{location} label must be 0 or 1")
    if not isinstance(event.source, str) or not isinstance(event.algorithm, str):
        raise ValueError(f"{location} source or algorithm must be a string")
    if event.source == "normal_list":
        valid_semantics = event.label == 0 and event.algorithm == ""
    elif event.source == "hard_negative":
        valid_semantics = event.label == 0 and event.algorithm == ""
    elif event.source == "public_dga":
        valid_semantics = event.label == 1 and event.algorithm == ""
    elif event.source == "manual_dga":
        valid_semantics = event.label == 1 and event.algorithm in ALGORITHMS
    else:
        valid_semantics = False
    if not valid_semantics:
        raise ValueError(f"{location} has invalid source/label/algorithm semantics")
    if not isinstance(event.split, str) or event.split not in _SPLIT_ORDER:
        raise ValueError(f"{location} has unknown split {event.split!r}")
    valid_behaviors = (
        _NORMAL_BEHAVIOR_ORDER if event.label == 0 else _DGA_BEHAVIOR_ORDER
    )
    if not isinstance(event.behavior, str) or event.behavior not in valid_behaviors:
        raise ValueError(f"{location} has invalid behavior for its label")


def serialize_log(events: Sequence[QueryEvent]) -> bytes:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("events must be a sequence of QueryEvent values")
    for index, event in enumerate(events, start=1):
        _validate_query_event(event, f"event {index}")

    ordered_events = sorted(
        events, key=lambda event: (event.timestamp, event.stable_order)
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=_LOG_HEADER,
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for event_id, event in enumerate(ordered_events, start=1):
        timestamp = event.timestamp.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        writer.writerow(
            {
                "event_id": event_id,
                "timestamp": timestamp,
                "client_ip": event.client_ip,
                "qname": event.qname,
                "qtype": event.qtype,
                "label": event.label,
                "source": event.source,
                "algorithm": event.algorithm,
                "split": event.split,
            }
        )
    return output.getvalue().encode("utf-8")


@dataclass(frozen=True, slots=True)
class RunSummary:
    seed: int
    input_sha256: Mapping[str, str]
    output_sha256: str
    target_rows: int
    actual_rows: int
    split_counts: Mapping[str, int]
    label_counts: Mapping[int, int]
    source_counts: Mapping[str, int]
    qtype_counts: Mapping[str, int]
    behavior_counts: Mapping[str, int]
    counts_by_split: Mapping[str, Mapping[str, object]]
    mixed_window_counts: Mapping[str, int]
    unique_qname_count: int
    aggregate_sample_count: int
    split_time_ranges: Mapping[str, Mapping[str, str] | None]
    synthetic: bool


def _format_summary_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _counter_dict(counter: Counter, order=None) -> dict:
    keys = order if order is not None else sorted(counter)
    return {key: counter[key] for key in keys if counter[key]}


def _empty_summary_counters():
    return {
        split: {
            "label_counts": Counter(),
            "source_counts": Counter(),
            "qtype_counts": Counter(),
            "behavior_counts": Counter(),
            "times": [],
            "qnames": set(),
            "aggregate_keys": set(),
        }
        for split in _SPLIT_ORDER
    }


def _finish_summary_counts(counts, window_labels):
    split_counts = {}
    mixed_window_counts = {}
    counts_by_split = {}
    split_time_ranges = {}
    all_qnames = set()
    all_aggregate_keys = set()
    for split in _SPLIT_ORDER:
        bucket = counts[split]
        split_counts[split] = len(bucket["times"])
        mixed_window_counts[split] = sum(
            1
            for (window_split, _client, _minute), labels in window_labels.items()
            if window_split == split and labels == {0, 1}
        )
        times = bucket["times"]
        time_range = (
            {
                "start": _format_summary_timestamp(min(times)),
                "end": _format_summary_timestamp(max(times)),
            }
            if times
            else None
        )
        split_time_ranges[split] = time_range
        all_qnames.update(bucket["qnames"])
        all_aggregate_keys.update(bucket["aggregate_keys"])
        counts_by_split[split] = {
            "label_counts": _counter_dict(bucket["label_counts"], (0, 1)),
            "source_counts": _counter_dict(bucket["source_counts"]),
            "qtype_counts": _counter_dict(bucket["qtype_counts"], _QTYPE_ORDER),
            "behavior_counts": _counter_dict(
                bucket["behavior_counts"],
                _NORMAL_BEHAVIOR_ORDER + _DGA_BEHAVIOR_ORDER,
            ),
            "mixed_window_count": mixed_window_counts[split],
            "unique_qname_count": len(bucket["qnames"]),
            "aggregate_sample_count": len(bucket["aggregate_keys"]),
            "time_range": time_range,
        }

    global_counters = {
        name: sum((bucket[name] for bucket in counts.values()), Counter())
        for name in ("label_counts", "source_counts", "qtype_counts", "behavior_counts")
    }
    return {
        "actual_rows": sum(split_counts.values()),
        "split_counts": split_counts,
        "label_counts": _counter_dict(global_counters["label_counts"], (0, 1)),
        "source_counts": _counter_dict(global_counters["source_counts"]),
        "qtype_counts": _counter_dict(global_counters["qtype_counts"], _QTYPE_ORDER),
        "behavior_counts": _counter_dict(
            global_counters["behavior_counts"],
            _NORMAL_BEHAVIOR_ORDER + _DGA_BEHAVIOR_ORDER,
        ),
        "counts_by_split": counts_by_split,
        "mixed_window_counts": mixed_window_counts,
        "unique_qname_count": len(all_qnames),
        "aggregate_sample_count": len(all_aggregate_keys),
        "split_time_ranges": split_time_ranges,
    }


def _summarize_events(events):
    counts = _empty_summary_counters()
    window_labels = {}
    for index, event in enumerate(events, start=1):
        _validate_query_event(event, f"event {index}")
        bucket = counts[event.split]
        minute = event.timestamp.replace(second=0, microsecond=0)
        bucket["label_counts"][event.label] += 1
        bucket["source_counts"][event.source] += 1
        bucket["qtype_counts"][event.qtype] += 1
        bucket["behavior_counts"][event.behavior] += 1
        bucket["times"].append(event.timestamp)
        bucket["qnames"].add(event.qname)
        bucket["aggregate_keys"].add((event.client_ip, minute, event.qname))
        window_labels.setdefault((event.split, event.client_ip, minute), set()).add(
            event.label
        )
    return _finish_summary_counts(counts, window_labels)


def _summarize_csv(csv_bytes):
    if not isinstance(csv_bytes, bytes):
        raise ValueError("csv_bytes must be bytes")
    try:
        text = csv_bytes.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != list(_LOG_HEADER):
            raise ValueError("serialized CSV header does not match the log contract")

        counts = _empty_summary_counters()
        window_labels = {}
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"serialized CSV row {row_number} has invalid fields")
            if row["label"] not in ("0", "1"):
                raise ValueError(f"serialized CSV row {row_number} has invalid label")
            split = row["split"]
            if split not in _SPLIT_ORDER:
                raise ValueError(f"serialized CSV row {row_number} has invalid split")
            if row["qtype"] not in _QTYPE_ORDER:
                raise ValueError(f"serialized CSV row {row_number} has invalid qtype")
            if not _UTC_MILLISECOND_TIMESTAMP.fullmatch(row["timestamp"]):
                raise ValueError(
                    f"serialized CSV row {row_number} has invalid timestamp"
                )
            timestamp = datetime.fromisoformat(row["timestamp"][:-1] + "+00:00")
            label = int(row["label"])
            client_ip = str(ipaddress.IPv4Address(row["client_ip"]))
            if client_ip != row["client_ip"]:
                raise ValueError(f"serialized CSV row {row_number} has invalid client IP")
            qname = prepare_data.normalize_domain(row["qname"])
            if qname != row["qname"]:
                raise ValueError(f"serialized CSV row {row_number} has invalid qname")

            bucket = counts[split]
            minute = timestamp.replace(second=0, microsecond=0)
            bucket["label_counts"][label] += 1
            bucket["source_counts"][row["source"]] += 1
            bucket["qtype_counts"][row["qtype"]] += 1
            bucket["times"].append(timestamp)
            bucket["qnames"].add(qname)
            bucket["aggregate_keys"].add((client_ip, minute, qname))
            window_labels.setdefault((split, client_ip, minute), set()).add(label)
    except (csv.Error, UnicodeError, ipaddress.AddressValueError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("serialized CSV"):
            raise
        raise ValueError(f"could not validate serialized CSV: {exc}") from exc

    result = _finish_summary_counts(counts, window_labels)
    result["output_sha256"] = hashlib.sha256(csv_bytes).hexdigest()
    return result


def _validate_events_sequence(events):
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("events must be a sequence of QueryEvent values")
    for index, event in enumerate(events, start=1):
        _validate_query_event(event, f"event {index}")


def _expected_input_hash_keys(config):
    psl_path = Path(config.psl_snapshot)
    if psl_path.is_absolute() or ".." in psl_path.parts:
        raise ValueError("paths.psl_snapshot must be a project-relative path")
    keys = ("config.json", "data/splits.csv", psl_path.as_posix())
    if len(set(keys)) != len(keys):
        raise ValueError("input hash paths must be distinct")
    return set(keys)


def build_summary(
    events: Sequence[QueryEvent],
    input_sha256: Mapping[str, str],
    output_sha256: str,
    config: GeneratorConfig,
) -> RunSummary:
    if not isinstance(config, GeneratorConfig):
        raise ValueError("config must be a GeneratorConfig")
    _validate_events_sequence(events)
    if not isinstance(input_sha256, Mapping):
        raise ValueError("input_sha256 must be a mapping")
    if set(input_sha256) != _expected_input_hash_keys(config):
        raise ValueError("input_sha256 must contain config, splits, and PSL hashes")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in input_sha256.values()
    ):
        raise ValueError("input_sha256 values must be lowercase SHA-256 digests")
    if not isinstance(output_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", output_sha256
    ):
        raise ValueError("output_sha256 must be a lowercase SHA-256 digest")

    data = _summarize_events(events)
    return RunSummary(
        seed=config.seed,
        input_sha256=dict(input_sha256),
        output_sha256=output_sha256,
        target_rows=config.total_rows_approx,
        actual_rows=data["actual_rows"],
        split_counts=data["split_counts"],
        label_counts=data["label_counts"],
        source_counts=data["source_counts"],
        qtype_counts=data["qtype_counts"],
        behavior_counts=data["behavior_counts"],
        counts_by_split=data["counts_by_split"],
        mixed_window_counts=data["mixed_window_counts"],
        unique_qname_count=data["unique_qname_count"],
        aggregate_sample_count=data["aggregate_sample_count"],
        split_time_ranges=data["split_time_ranges"],
        synthetic=True,
    )


def validate_summary(
    summary: RunSummary, events: Sequence[QueryEvent], csv_bytes: bytes
) -> None:
    if not isinstance(summary, RunSummary):
        raise ValueError("summary must be a RunSummary")
    _validate_events_sequence(events)
    csv_data = _summarize_csv(csv_bytes)
    if csv_bytes != serialize_log(events):
        raise ValueError("serialized CSV bytes do not match in-memory events")
    if len(events) != csv_data["actual_rows"]:
        raise ValueError("CSV row count does not match in-memory events")

    event_data = _summarize_events(events)
    if summary.behavior_counts != event_data["behavior_counts"]:
        raise ValueError("summary behavior_counts do not match in-memory events")
    expected_by_split = csv_data["counts_by_split"]
    for split in _SPLIT_ORDER:
        expected_by_split[split]["behavior_counts"] = event_data["counts_by_split"][
            split
        ]["behavior_counts"]

    fields = (
        "actual_rows",
        "output_sha256",
        "split_counts",
        "label_counts",
        "source_counts",
        "qtype_counts",
        "mixed_window_counts",
        "unique_qname_count",
        "aggregate_sample_count",
        "split_time_ranges",
    )
    for field in fields:
        if getattr(summary, field) != csv_data[field]:
            raise ValueError(f"summary {field} does not match serialized CSV")
    if summary.counts_by_split != expected_by_split:
        raise ValueError("summary counts_by_split does not match CSV and events")
    if summary.target_rows != summary.actual_rows:
        raise ValueError("summary target_rows does not match actual_rows")


def write_log(csv_bytes: bytes, target: Path) -> str:
    if not isinstance(csv_bytes, bytes):
        raise ValueError("csv_bytes must be bytes")
    if not isinstance(target, Path):
        raise ValueError("target must be a Path")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(csv_bytes)
            stream.flush()
            os.fsync(stream.fileno())

        expected_hash = hashlib.sha256(csv_bytes).hexdigest()
        temporary_hash = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        if temporary_hash != expected_hash:
            raise OSError("temporary log SHA-256 verification failed")
        temporary_path.replace(target)
        return temporary_hash
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _project_relative_input(project_root: Path, relative_path: str, name: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a project-relative path")
    resolved = (project_root / path).resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError(f"{name} must remain inside the project root")
    return resolved


def run_log_generation(
    project_root: Path, config_path: Path | None = None
) -> RunSummary:
    root = Path(project_root).resolve()
    if config_path is None:
        config_file = root / "config.json"
    else:
        config_file = Path(config_path)
        if not config_file.is_absolute():
            config_file = root / config_file
        config_file = config_file.resolve()

    config = load_config(config_file)
    splits_path = _project_relative_input(root, "data/splits.csv", "splits path")
    psl_path = _project_relative_input(root, config.psl_snapshot, "PSL path")
    records = read_split_records(splits_path)
    validate_domain_pools(records, psl_path)

    split_domain_counts = {
        split: sum(record.split == split for record in records)
        for split in _SPLIT_ORDER
    }
    event_quotas = allocate_event_quotas(
        split_domain_counts, config.total_rows_approx, config.dga_ratio
    )
    allocate_behavior_quotas(
        event_quotas, config.normal_behavior_ratio, config.dga_behavior_ratio
    )
    expected_label_counts = {
        split: {
            0: event_quotas[split]["normal"],
            1: event_quotas[split]["dga"],
        }
        for split in _SPLIT_ORDER
    }
    expected_qtype_quotas = allocate_qtype_quotas(
        expected_label_counts, config.qtype_weights
    )

    events = build_events(records, config)
    if len(events) != config.total_rows_approx:
        raise ValueError("generated event count does not match configured target")
    for index, event in enumerate(events, start=1):
        _validate_query_event(event, f"event {index}")
    actual_label_counts = {
        split: {
            label: sum(
                event.split == split and event.label == label for event in events
            )
            for label in (0, 1)
        }
        for split in _SPLIT_ORDER
    }
    if actual_label_counts != expected_label_counts:
        raise ValueError("generated label counts do not match allocated quotas")
    for split in _SPLIT_ORDER:
        for label in (0, 1):
            actual_qtypes = Counter(
                event.qtype
                for event in events
                if event.split == split and event.label == label
            )
            if actual_qtypes != expected_qtype_quotas[split][label]:
                raise ValueError("generated qtype counts do not match allocated quotas")

    csv_bytes = serialize_log(events)
    input_bytes = {
        "config.json": config_file.read_bytes(),
        "data/splits.csv": splits_path.read_bytes(),
        Path(config.psl_snapshot).as_posix(): psl_path.read_bytes(),
    }
    input_sha256 = {
        name: hashlib.sha256(data).hexdigest() for name, data in input_bytes.items()
    }
    output_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    summary = build_summary(events, input_sha256, output_sha256, config)
    validate_summary(summary, events, csv_bytes)

    write_log(csv_bytes, root / "data" / "dns_log.csv")
    return summary


def main() -> int:
    try:
        summary = run_log_generation(Path.cwd())
    except Exception as exc:
        print(f"DNS log generation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(summary), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
