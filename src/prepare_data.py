"""Prepare the offline D2 domain dataset and deterministic eTLD+1 splits.

The pipeline normalizes and validates local source files, tracks exclusions and
provenance, rejects label conflicts, generates manual samples, and writes the
three D2 outputs without network or DNS access.
"""

import csv
import hashlib
import io
import ipaddress
import json
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import sys

# 公共后缀解析库：只使用外部传入的本地 PSL 快照，不联网更新
import tldextract

# 复用 DGA 生成模块的算法枚举、记录结构与生成入口
from .gen_dga import ALGORITHMS, DgaRecord
from .gen_dga import generate_dga_domains


# 原始来源行：对应 data/raw 中的一行，域名尚未规范化
@dataclass
class SourceRow:
    raw_domain: str
    label: int
    source: str
    algorithm: str
    source_id: str
    source_path: Path
    line_number: int
    family: str | None = None


# 规范化后的来源行：domain 已通过 IDNA、长度与字符集校验
@dataclass
class NormalizedSourceRow:
    domain: str
    label: int
    source: str
    algorithm: str
    source_id: str
    source_path: Path
    line_number: int
    family: str | None = None


# 被剔除的行：保留出处与剔除原因，供运行汇总追溯
@dataclass
class ExcludedRow:
    source_id: str
    source_path: Path
    line_number: int
    raw_domain: str
    reason: str


# 合并后的域名记录：同一域名的所有来源行聚成一条，附带 eTLD+1 与划分结果
@dataclass
class DomainRecord:
    domain: str
    label: int
    source: str
    algorithm: str
    etld_plus_one: str
    split: str
    sources: tuple[NormalizedSourceRow, ...]


# 单个 DGA 算法的诊断信息：样本数、唯一域名数、碰撞重试次数与示例域名
@dataclass
class AlgorithmDiagnostics:
    count: int
    unique_count: int
    collision_retries: int
    example_domains: list[str]


# 运行汇总：各来源计数、剔除明细、划分/标签/算法统计以及输入文件哈希
@dataclass
class RunSummary:
    source_counts: dict[str, dict[str, int]]
    excluded_rows: list[dict[str, str | int]]
    public_dga_family_counts: dict[str, dict[str, int]]
    split_counts: dict[str, int]
    split_ratios: dict[str, float]
    label_counts: dict[str, int]
    algorithm_counts: dict[str, int]
    algorithm_diagnostics: dict[str, AlgorithmDiagnostics]
    collision_retries: int
    hard_negative_promoted: int
    psl_sha256: str
    input_sha256: dict[str, str]


def normalize_domain(value):
    # 域名规范化：去首尾空白与末尾根点后做 IDNA 转 ASCII 并小写，再校验 IP、总长度与逐标签字符集
    if not isinstance(value, str):
        raise ValueError("domain must be a string")

    value = value.strip()
    if value.endswith("."):
        value = value[:-1]
    if not value:
        raise ValueError("domain must not be empty")

    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not valid IDNA") from exc

    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not domains")

    if len(domain) > 253:
        raise ValueError("domain exceeds 253 ASCII characters")

    labels = domain.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63:
            raise ValueError("domain labels must contain 1 to 63 characters")
        if label.startswith("-") or label.endswith("-"):
            raise ValueError("domain labels must not start or end with a hyphen")
        if any(
            not (character.isascii() and (character.isalnum() or character == "-"))
            for character in label
        ):
            raise ValueError(
                "domain labels may contain only ASCII letters, digits, and hyphens"
            )

    return domain


def create_extractor(psl_path):
    # 创建离线 tldextract 解析器：只加载本地 PSL 快照、关闭快照回退、包含私有后缀，并用 example.com 自检 com 规则可用
    path = Path(psl_path)
    if not path.is_file():
        raise ValueError(f"PSL file does not exist: {path}")

    path = path.resolve()
    if path.stat().st_size == 0:
        raise ValueError(f"PSL file is empty: {path}")

    try:
        extractor = tldextract.TLDExtract(
            suffix_list_urls=(path.as_uri(),),
            cache_dir=None,
            fallback_to_snapshot=False,
            include_psl_private_domains=True,
        )
        result = extractor("example.com")
    except Exception as exc:
        raise ValueError(f"Invalid local PSL at {path}: {exc}") from exc

    if result.domain != "example" or result.suffix != "com":
        raise ValueError(f"Invalid local PSL at {path}: a usable com rule is required")
    return extractor


def load_source_rows(raw_dir):
    # 遍历 data/raw 下三类来源文件并解析为 SourceRow：正常榜单与难负样本按行读 txt，公开 DGA 读 domain,family 两列 CSV
    root = Path(raw_dir).resolve()
    # 每项为 (类别, 文件名通配模式, 文件名前缀)，前缀之后的部分用作 source_id
    patterns = (
        ("normal_list", "normal_list__*.txt", "normal_list__"),
        ("hard_negative", "hard_negative__*.txt", "hard_negative__"),
        ("public_dga", "public_dga__*.csv", "public_dga__"),
    )
    files_by_category = {
        category: sorted(
            (path for path in root.glob(pattern) if path.is_file()),
            key=lambda path: path.name,
        )
        for category, pattern, _ in patterns
    }

    for category, _, _ in patterns:
        if not files_by_category[category]:
            raise ValueError(f"raw data directory {root} is missing category {category}")

    rows = []
    for category, _, prefix in patterns:
        for path in files_by_category[category]:
            source_id = path.stem[len(prefix):]
            source_path = path.resolve()
            if category in ("normal_list", "hard_negative"):
                # 正常榜单与难负样本：每行一个域名，label 恒为 0，无算法与 family
                with path.open("r", encoding="utf-8", newline="") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        rows.append(
                            SourceRow(
                                raw_domain=line.rstrip("\r\n"),
                                label=0,
                                source=category,
                                algorithm="",
                                source_id=source_id,
                                source_path=source_path,
                                line_number=line_number,
                            )
                        )
                continue

            with path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.reader(stream, strict=True)
                # 公开 DGA CSV 必须严格是 domain,family 两列，family 不能为空也不能是 legit
                try:
                    header = next(reader)
                except StopIteration:
                    raise ValueError(f"{path.name}:1: missing CSV header")
                except csv.Error as exc:
                    line_number = reader.line_num or 1
                    raise ValueError(
                        f"{path.name}:{line_number}: invalid CSV: {exc}"
                    ) from exc
                if header != ["domain", "family"]:
                    raise ValueError(f"{path.name}:1: expected CSV header domain,family")

                while True:
                    line_number = reader.line_num + 1
                    try:
                        record = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        line_number = reader.line_num or line_number
                        raise ValueError(
                            f"{path.name}:{line_number}: invalid CSV: {exc}"
                        ) from exc

                    if len(record) != 2:
                        raise ValueError(
                            f"{path.name}:{line_number}: expected exactly 2 CSV fields"
                        )
                    family = record[1].strip()
                    if not family:
                        raise ValueError(
                            f"{path.name}:{line_number}: family must not be empty"
                        )
                    if family.casefold() == "legit":
                        raise ValueError(
                            f"{path.name}:{line_number}: public DGA family must not be 'legit'"
                        )
                    rows.append(
                        SourceRow(
                            raw_domain=record[0],
                            label=1,
                            source="public_dga",
                            algorithm="",
                            source_id=source_id,
                            source_path=source_path,
                            line_number=line_number,
                            family=family,
                        )
                    )

    return rows


def normalize_source_rows(rows):
    # 逐行规范化域名，成功的转成 NormalizedSourceRow，失败的转成带原因的 ExcludedRow，返回 (有效行, 剔除行)
    valid_rows = []
    excluded_rows = []
    for row in rows:
        try:
            domain = normalize_domain(row.raw_domain)
        except ValueError as exc:
            excluded_rows.append(
                ExcludedRow(
                    source_id=row.source_id,
                    source_path=row.source_path,
                    line_number=row.line_number,
                    raw_domain=row.raw_domain,
                    reason=str(exc),
                )
            )
        else:
            valid_rows.append(
                NormalizedSourceRow(
                    domain=domain,
                    label=row.label,
                    source=row.source,
                    algorithm=row.algorithm,
                    source_id=row.source_id,
                    source_path=row.source_path,
                    line_number=row.line_number,
                    family=row.family,
                )
            )
    return valid_rows, excluded_rows


def select_public_dga(rows, limit, master_seed):
    # 公开 DGA 抽样：先校验同一域名不跨 family，再按 family 分层等比抽样到 limit 条，并用 master_seed 保证结果可复现
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise ValueError("master_seed must be an integer")

    grouped_by_domain = {}
    for row in rows:
        if not isinstance(row.family, str) or not row.family:
            raise ValueError(f"public DGA row for {row.domain!r} has no family")
        grouped_by_domain.setdefault(row.domain, {}).setdefault(
            row.family, []
        ).append(row)

    # 标签冲突检查：同一域名出现在多个 DGA family 时报错，并列出全部出处
    for domain in sorted(grouped_by_domain):
        families = grouped_by_domain[domain]
        if len(families) > 1:
            provenance = []
            for family in sorted(families):
                for row in sorted(families[family], key=_provenance_key):
                    provenance.append(
                        f"{family} at {row.source_path}:{row.line_number} ({row.source_id})"
                    )
            raise ValueError(
                f"domain {domain!r} appears in multiple DGA families: "
                + "; ".join(provenance)
            )

    # 每个域名在各自 family 内只保留出处排序最小的一行，作为唯一记录
    unique_rows = []
    for families in grouped_by_domain.values():
        for family_rows in families.values():
            unique_rows.append(min(family_rows, key=_provenance_key))

    by_family = {}
    for row in unique_rows:
        by_family.setdefault(row.family, []).append(row)

    if len(unique_rows) <= limit:
        selected = unique_rows
    else:
        total = len(unique_rows)
        # 最大余数法：先按比例取整分配名额，剩余名额按余数从大到小（同余数按 family 名）依次补齐
        quotas = {}
        remainders = {}
        for family, family_rows in by_family.items():
            quotas[family], remainders[family] = divmod(
                len(family_rows) * limit, total
            )
        remaining = limit - sum(quotas.values())
        remainder_order = sorted(
            by_family,
            key=lambda family: (-remainders[family], family),
        )
        for family in remainder_order[:remaining]:
            quotas[family] += 1

        selected = []
        for family in sorted(by_family):
            family_rows = sorted(by_family[family], key=_sampling_key(master_seed))
            selected.extend(family_rows[:quotas[family]])

    return sorted(selected, key=_sampling_key(master_seed))


def _provenance_key(row):
    # 出处排序键：由路径、行号、来源 ID、域名等组成，保证同一批数据下排序结果稳定
    return (
        str(row.source_path),
        row.line_number,
        row.source_id,
        getattr(row, "raw_domain", row.domain),
        row.label,
        row.source,
        row.algorithm,
    )


def _sampling_key(master_seed):
    # 返回抽样排序函数：对 master_seed|public_dga|family|domain 求 SHA-256，固定种子下抽样顺序可复现
    def key(row):
        digest = hashlib.sha256(
            f"{master_seed}|public_dga|{row.family}|{row.domain}".encode(
                "utf-8"
            )
        ).hexdigest()
        return digest, row.domain

    return key


# 数据集划分标签
_SPLITS = ("train", "val", "test")
# 目标划分比例：训练 60%、验证 20%、测试 20%
_SPLIT_RATIOS = {"train": 0.6, "val": 0.2, "test": 0.2}


# 校验入参必须是序列（排除 str/bytes），返回原值便于链式使用
def _require_sequence(value, name):
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def _source_location(row):
    # 生成可读出处串，形如 source ID 'xxx' at 路径:行号，用于报错定位
    return f"source ID {row.source_id!r} at {row.source_path}:{row.line_number}"


def _validate_normalized_source_row(row):
    # 校验单条规范化来源行：类型、source_id、路径、行号、域名已规范化、标签取值，以及各来源的语义一致性
    if not isinstance(row, NormalizedSourceRow):
        raise ValueError("rows must contain NormalizedSourceRow values")
    if not isinstance(row.source_id, str) or not row.source_id.strip():
        raise ValueError("source rows must have a non-empty source ID")
    if not isinstance(row.source_path, Path):
        raise ValueError(f"source row {row.source_id!r} must have a Path source_path")
    if (
        isinstance(row.line_number, bool)
        or not isinstance(row.line_number, int)
        or row.line_number < 1
    ):
        raise ValueError(f"source row {row.source_id!r} has an invalid line number")
    try:
        normalized_domain = normalize_domain(row.domain)
    except ValueError as exc:
        raise ValueError(
            f"invalid domain in {_source_location(row)}: {exc}"
        ) from exc
    if normalized_domain != row.domain:
        raise ValueError(
            f"domain {row.domain!r} in {_source_location(row)} is not normalized"
        )
    if (
        isinstance(row.label, bool)
        or not isinstance(row.label, int)
        or row.label not in (0, 1)
    ):
        raise ValueError(
            f"invalid label for domain {row.domain!r} in {_source_location(row)}"
        )
    if not isinstance(row.algorithm, str):
        raise ValueError(
            f"invalid algorithm for domain {row.domain!r} in {_source_location(row)}"
        )
    if row.source == "normal_list":
        if row.label != 0 or row.algorithm != "" or row.family is not None:
            raise ValueError(
                f"invalid normal-list semantics for {row.domain!r} "
                f"in {_source_location(row)}"
            )
    elif row.source == "hard_negative":
        if row.label != 0 or row.algorithm != "" or row.family is not None:
            raise ValueError(
                f"invalid hard-negative semantics for {row.domain!r} "
                f"in {_source_location(row)}"
            )
    elif row.source == "public_dga":
        if row.label != 1 or row.algorithm != "":
            raise ValueError(
                f"invalid public-DGA semantics for {row.domain!r} "
                f"in {_source_location(row)}"
            )
        if row.family is not None and (
            not isinstance(row.family, str) or not row.family.strip()
        ):
            raise ValueError(
                f"invalid public-DGA family for {row.domain!r} "
                f"in {_source_location(row)}"
            )
    else:
        raise ValueError(
            f"invalid source for domain {row.domain!r}: {row.source!r} "
            f"in {_source_location(row)}"
        )


def _validate_domain_record(record):
    # 校验单条域名记录：域名规范化、标签取值、来源/标签/算法语义、eTLD+1 与 split 字段类型，以及来源行类型
    if not isinstance(record, DomainRecord):
        raise ValueError("records must contain DomainRecord values")
    try:
        domain = normalize_domain(record.domain)
    except ValueError as exc:
        raise ValueError(f"invalid domain {record.domain!r}: {exc}") from exc
    if (
        isinstance(record.label, bool)
        or not isinstance(record.label, int)
        or record.label not in (0, 1)
    ):
        raise ValueError(f"invalid label for domain {record.domain!r}")
    if not isinstance(record.source, str) or not isinstance(record.algorithm, str):
        raise ValueError(f"invalid source or algorithm for domain {record.domain!r}")
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
        raise ValueError(f"invalid source/label/algorithm semantics for {record.domain!r}")
    if not isinstance(record.etld_plus_one, str) or not isinstance(record.split, str):
        raise ValueError(f"invalid split metadata for domain {record.domain!r}")
    if not isinstance(record.sources, tuple) or any(
        not isinstance(source, NormalizedSourceRow) for source in record.sources
    ):
        raise ValueError(f"invalid provenance for domain {record.domain!r}")
    return domain


def merge_and_validate(rows: Sequence[NormalizedSourceRow]) -> list[DomainRecord]:
    # 按域名合并来源行并做冲突检查：同一域名语义必须一致，公开 DGA 只能属于一个 family 且必须有 family
    rows = _require_sequence(rows, "rows")
    grouped = {}
    for row in rows:
        _validate_normalized_source_row(row)
        grouped.setdefault(row.domain, []).append(row)

    records = []
    for domain in sorted(grouped):
        domain_rows = sorted(grouped[domain], key=_provenance_key)
        semantics = {
            (row.label, row.source, row.algorithm) for row in domain_rows
        }
        public_families = {
            row.family for row in domain_rows if row.source == "public_dga"
        }
        benign_semantics = {
            (row.label, row.algorithm)
            for row in domain_rows
            if row.source in ("normal_list", "hard_negative")
        }
        if len(public_families) > 1:
            provenance = "; ".join(_source_location(row) for row in domain_rows)
            raise ValueError(
                f"conflicting records for domain {domain!r}: {provenance}"
            )
        if len(semantics) != 1:
            if (
                any(
                    row.source not in ("normal_list", "hard_negative")
                    for row in domain_rows
                )
                or benign_semantics != {(0, "")}
            ):
                provenance = "; ".join(_source_location(row) for row in domain_rows)
                raise ValueError(
                    f"conflicting records for domain {domain!r}: {provenance}"
                )
        if domain_rows[0].source == "public_dga" and public_families == {None}:
            provenance = "; ".join(_source_location(row) for row in domain_rows)
            raise ValueError(
                f"public DGA family missing for domain {domain!r}: {provenance}"
            )

        first = domain_rows[0]
        # 同一域名同时出现在正常榜单与难负样本时，合并记录的来源标记为 hard_negative
        source = (
            "hard_negative"
            if any(row.source == "hard_negative" for row in domain_rows)
            else first.source
        )
        records.append(
            DomainRecord(
                domain=domain,
                label=first.label,
                source=source,
                algorithm=first.algorithm,
                etld_plus_one="",
                split="",
                sources=tuple(domain_rows),
            )
        )
    return records


def manual_domain_records(records: Sequence[DgaRecord]) -> list[DomainRecord]:
    # 把合成 DGA 记录转换为域名记录：校验算法/种子/参数类型，域名去重，统一标记 label=1、source=manual_dga
    records = _require_sequence(records, "records")
    domain_records = []
    seen_domains = set()
    for record in records:
        if not isinstance(record, DgaRecord):
            raise ValueError("records must contain DgaRecord values")
        if record.algorithm not in ALGORITHMS:
            raise ValueError(f"unknown manual DGA algorithm: {record.algorithm!r}")
        if not isinstance(record.seed, str) or not record.seed:
            raise ValueError("manual DGA records must have a non-empty seed")
        if not isinstance(record.params, dict):
            raise ValueError("manual DGA record params must be a dictionary")
        domain = normalize_domain(record.domain)
        if domain in seen_domains:
            raise ValueError(f"duplicate manual DGA domain: {domain!r}")
        seen_domains.add(domain)
        domain_records.append(
            DomainRecord(
                domain=domain,
                label=1,
                source="manual_dga",
                algorithm=record.algorithm,
                etld_plus_one="",
                split="",
                sources=(),
            )
        )
    return domain_records


def assign_splits(
    records: Sequence[DomainRecord], extractor: tldextract.TLDExtract, master_seed: int
) -> list[DomainRecord]:
    # 以 eTLD+1 为最小分组单位划分 train/val/test：同组不拆开，按目标比例与分层占比贪心分配，并校验同一 eTLD+1 只属于一个划分
    records = _require_sequence(records, "records")
    if not callable(extractor):
        raise ValueError("extractor must be callable")
    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise ValueError("master_seed must be an integer")

    normalized_records = []
    seen_domains = set()
    # 先规范化并去重，再按 eTLD+1 分组，使同一注册域名下的子域绑定到同一划分
    groups = {}
    for record in records:
        domain = _validate_domain_record(record)
        if domain in seen_domains:
            raise ValueError(f"duplicate domain record: {domain!r}")
        seen_domains.add(domain)
        try:
            extracted = extractor(domain)
        except Exception as exc:
            raise ValueError(f"could not extract registered domain for {domain!r}") from exc
        domain_part = getattr(extracted, "domain", "")
        suffix_part = getattr(extracted, "suffix", "")
        if not domain_part or not suffix_part:
            raise ValueError(f"domain {domain!r} has no registered domain (eTLD+1)")
        etld_plus_one = f"{domain_part}.{suffix_part}"
        normalized_record = replace(record, domain=domain)
        normalized_records.append(normalized_record)
        groups.setdefault(etld_plus_one, []).append(normalized_record)

    if not normalized_records:
        return []

    # 分层维度为 (label, source, algorithm)，用于同时逼近各划分内的分层占比
    strata = sorted(
        {
            (record.label, record.source, record.algorithm)
            for record in normalized_records
        }
    )
    total_by_stratum = Counter(
        (record.label, record.source, record.algorithm)
        for record in normalized_records
    )
    split_counts = {split: 0 for split in _SPLITS}
    split_strata = {
        split: {stratum: 0 for stratum in strata} for split in _SPLITS
    }
    group_splits = {}

    # 大组优先处理，同规模组用 种子+eTLD+1 的 SHA-256 打乱，降低划分对输入顺序的依赖
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            -len(item[1]),
            hashlib.sha256(
                f"{master_seed}|{item[0]}".encode("utf-8")
            ).hexdigest(),
            item[0],
        ),
    )
    for etld_plus_one, group in ordered_groups:
        group_by_stratum = Counter(
            (record.label, record.source, record.algorithm) for record in group
        )
        # 对每个候选划分计算损失：总体样本偏差与各分层偏差的相对平方误差之和，取损失最小者（同分按 train/val/test 顺序）
        candidate_losses = []
        for candidate in _SPLITS:
            loss = 0.0
            for split in _SPLITS:
                candidate_total = split_counts[split]
                if split == candidate:
                    candidate_total += len(group)
                target_total = _SPLIT_RATIOS[split] * len(normalized_records)
                loss += ((candidate_total - target_total) / max(1, target_total)) ** 2
                for stratum in strata:
                    candidate_count = split_strata[split][stratum]
                    if split == candidate:
                        candidate_count += group_by_stratum[stratum]
                    target_count = (
                        _SPLIT_RATIOS[split] * total_by_stratum[stratum]
                    )
                    loss += (
                        (candidate_count - target_count) / max(1, target_count)
                    ) ** 2
            candidate_losses.append((loss, _SPLITS.index(candidate), candidate))

        selected_split = min(candidate_losses)[2]
        group_splits[etld_plus_one] = selected_split
        split_counts[selected_split] += len(group)
        for stratum in strata:
            split_strata[selected_split][stratum] += group_by_stratum[stratum]

    assigned = [
        replace(
            record,
            etld_plus_one=etld_plus_one,
            split=group_splits[etld_plus_one],
        )
        for etld_plus_one, group in groups.items()
        for record in group
    ]
    assigned.sort(key=lambda record: (record.etld_plus_one, record.domain))

    # 断言校验：同一 eTLD+1 组不能被拆到多个划分
    splits_by_etld = {}
    for record in assigned:
        splits_by_etld.setdefault(record.etld_plus_one, set()).add(record.split)
    if any(len(splits) != 1 for splits in splits_by_etld.values()):
        raise ValueError("an eTLD+1 group was assigned to multiple splits")

    # 合成 DGA 覆盖检查：每种算法都必须出现在 train/val/test 三个划分中
    if any(record.source == "manual_dga" for record in assigned):
        missing_coverage = {
            algorithm: [
                split
                for split in _SPLITS
                if not any(
                    record.source == "manual_dga"
                    and record.algorithm == algorithm
                    and record.split == split
                    for record in assigned
                )
            ]
            for algorithm in ALGORITHMS
        }
        missing_coverage = {
            algorithm: missing_splits
            for algorithm, missing_splits in missing_coverage.items()
            if missing_splits
        }
        if missing_coverage:
            missing = "; ".join(
                f"{algorithm} missing {', '.join(missing_splits)}"
                for algorithm, missing_splits in missing_coverage.items()
            )
            actual_ratios = ", ".join(
                f"{split}={split_counts[split] / len(normalized_records):.3f}"
                for split in _SPLITS
            )
            raise ValueError(
                f"manual DGA split coverage is incomplete: {missing}; "
                f"actual split ratios: {actual_ratios}"
            )

    return assigned


def _deduplicated_source_counts(rows):
    # 统计各来源文件中被去重的行数：同一 (域名, 标签, 来源, 算法, family) 只保留出处最小的一行，其余计入对应 source_id
    grouped = {}
    for row in rows:
        family = row.family if row.source == "public_dga" else None
        key = (row.domain, row.label, row.source, row.algorithm, family)
        grouped.setdefault(key, []).append(row)

    counts = Counter()
    for duplicates in grouped.values():
        ordered = sorted(duplicates, key=_provenance_key)
        for row in ordered[1:]:
            counts[row.source_id] += 1
    return counts


def _public_dga_family_summary(rows, selected_rows, limit):
    # 汇总公开 DGA 各 family 的可用数、配额与实际选中数，配额算法与 select_public_dga 保持一致
    unique_by_domain = {}
    for row in rows:
        existing = unique_by_domain.get(row.domain)
        if existing is not None and existing.family != row.family:
            raise ValueError(f"domain {row.domain!r} appears in multiple DGA families")
        if existing is None or _provenance_key(row) < _provenance_key(existing):
            unique_by_domain[row.domain] = row

    available = Counter(row.family for row in unique_by_domain.values())
    total = sum(available.values())
    if total <= limit:
        quotas = dict(available)
    else:
        quotas = {}
        remainders = {}
        for family, count in available.items():
            quotas[family], remainders[family] = divmod(count * limit, total)
        remaining = limit - sum(quotas.values())
        for family in sorted(available, key=lambda value: (-remainders[value], value))[
            :remaining
        ]:
            quotas[family] += 1

    selected = Counter(row.family for row in selected_rows)
    return {
        family: {
            "available": available[family],
            "quota": quotas[family],
            "selected": selected[family],
        }
        for family in sorted(available)
    }


def _sha256_file(path):
    # 计算文件内容的 SHA-256，用于运行汇总记录输入文件指纹
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _csv_bytes(fieldnames, rows):
    # 在内存中生成 CSV 字节：统一 LF 换行、多余字段直接报错，供原子写出使用
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fieldnames,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _write_output_bundle(outputs):
    # 原子写出多个输出文件：先把全部内容写入同目录临时文件，再备份旧文件后替换；任一步失败则回滚临时文件与备份
    temporary_paths = {}
    backup_paths = {}
    existing_targets = set()
    backed_up_targets = set()
    try:
        existing_targets = {
            target
            for target in outputs
            if target.exists() or target.is_symlink()
        }
        # 第一步：写入临时文件，此时不触碰任何已有产物
        for target, contents in outputs.items():
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_paths[target] = Path(stream.name)
                stream.write(contents)

        for target in outputs:
            if target not in existing_targets:
                continue
            # 第二步：把已有文件改名为备份，供失败时恢复
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".bak",
                delete=False,
            ) as stream:
                backup_paths[target] = Path(stream.name)
            target.replace(backup_paths[target])
            backed_up_targets.add(target)

        for target, temporary_path in temporary_paths.items():
            # 第三步：用临时文件替换目标文件
            temporary_path.replace(target)

    except Exception as exc:
        # 失败时回滚：恢复已备份的旧文件，删除新建文件与残留临时文件
        rollback_errors = []
        for target in outputs:
            try:
                if target in backed_up_targets:
                    if target.exists():
                        target.unlink()
                    backup_paths[target].replace(target)
                    backed_up_targets.remove(target)
                elif target not in existing_targets:
                    target.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")

        for temporary_path in temporary_paths.values():
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                rollback_errors.append(f"{temporary_path}: {cleanup_exc}")

        for target, backup_path in backup_paths.items():
            if target in backed_up_targets:
                continue
            try:
                backup_path.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                rollback_errors.append(f"{backup_path}: {cleanup_exc}")

        if rollback_errors:
            recoverable_backups = [
                str(backup_paths[target])
                for target in outputs
                if target in backed_up_targets and backup_paths[target].exists()
            ]
            error_message = (
                f"output bundle failed: {exc}; rollback failed: "
                f"{'; '.join(rollback_errors)}"
            )
            if recoverable_backups:
                error_message += (
                    f"; recoverable backups: {', '.join(recoverable_backups)}"
                )
            raise RuntimeError(error_message) from exc
        raise

    cleanup_errors = []
    # 成功替换后清理备份文件；仅清理失败时抛出 OSError，但不影响已写入的产物
    for backup_path in backup_paths.values():
        try:
            backup_path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"{backup_path}: {exc}")
    if cleanup_errors:
        raise OSError(
            "output bundle was written but backup cleanup failed: "
            + "; ".join(cleanup_errors)
        )


def run_data_preparation(project_root: Path, config_path: Path) -> RunSummary:
    # D2 主流程：读配置与 PSL，读取/规范化/合并来源数据，抽样公开 DGA，生成合成样本，划分数据集，汇总统计并原子写出三个产物
    root = Path(project_root).resolve()
    configuration_path = Path(config_path)
    # 相对路径的配置文件以项目根为基准解析
    if not configuration_path.is_absolute():
        configuration_path = root / configuration_path
    configuration_path = configuration_path.resolve()

    try:
        with configuration_path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read configuration {configuration_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    seed = config.get("seed")
    # 种子固定为 20261101，保证抽样、生成与划分结果可复现
    if type(seed) is not int or seed != 20261101:
        raise ValueError("config seed must be the integer 20261101")
    paths = config.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("configuration paths must be an object")
    psl_value = paths.get("psl_snapshot")
    if not isinstance(psl_value, str) or not psl_value.strip():
        raise ValueError("configuration paths.psl_snapshot must be a non-empty path")

    psl_path = Path(psl_value)
    # 相对路径的 PSL 快照同样以项目根为基准
    if not psl_path.is_absolute():
        psl_path = root / psl_path
    psl_path = psl_path.resolve()
    extractor = create_extractor(psl_path)

    # 固定从项目下 data/raw 读取原始数据
    raw_dir = root / "data" / "raw"
    source_rows = load_source_rows(raw_dir)
    normalized_rows, excluded_rows = normalize_source_rows(source_rows)

    # Validate every valid source row before applying the public-DGA sample cap.
    merged_records = merge_and_validate(normalized_rows)
    raw_by_location = {
        (row.source_id, row.source_path.resolve(), row.line_number): row.raw_domain
        for row in source_rows
    }
    # 逐个域名做 PSL 查询：解析不出 eTLD+1 的记为剔除项，其余进入候选集
    eligible_rows = []
    for row in normalized_rows:
        try:
            extracted = extractor(row.domain)
        except Exception as exc:
            raise ValueError(
                f"PSL lookup failed for domain {row.domain!r} in "
                f"{_source_location(row)}: {exc}"
            ) from exc
        if not extracted.domain or not extracted.suffix:
            raw_domain = raw_by_location[
                (row.source_id, row.source_path.resolve(), row.line_number)
            ]
            excluded_rows.append(
                ExcludedRow(
                    source_id=row.source_id,
                    source_path=row.source_path,
                    line_number=row.line_number,
                    raw_domain=raw_domain,
                    reason="no registered domain (eTLD+1) under the configured PSL",
                )
            )
        else:
            eligible_rows.append(row)

    eligible_domains = {row.domain for row in eligible_rows}
    # 公开 DGA 只保留按 family 抽样命中的 300 条，正常榜单与难负样本全部保留
    public_rows = [row for row in eligible_rows if row.source == "public_dga"]
    selected_public_rows = select_public_dga(public_rows, limit=300, master_seed=seed)
    selected_public_domains = {row.domain for row in selected_public_rows}
    output_records = [
        record
        for record in merged_records
        if record.domain in eligible_domains
        and (
            record.source in ("normal_list", "hard_negative")
            or record.domain in selected_public_domains
        )
    ]
    # 统计同时出现在正常榜单中的难负样本数量，这些记录的来源会被提升为 hard_negative
    hard_negative_promoted = sum(
        1
        for record in output_records
        if record.source == "hard_negative"
        and any(row.source == "normal_list" for row in record.sources)
    )

    # 合成 DGA 需避开全部真实域名（榜单、难负样本、抽样命中的公开 DGA），避免标签冲突
    reserved_domains = {
        record.domain
        for record in merged_records
        if record.source in ("normal_list", "hard_negative")
    } | selected_public_domains
    word_roots_path = raw_dir / "manual_word_roots.txt"
    generated_records = generate_dga_domains(
        master_seed=seed,
        word_roots_path=word_roots_path,
        reserved_domains=reserved_domains,
        count_per_algorithm=100,
    )
    manual_records = manual_domain_records(generated_records)
    # 真实来源记录与合成记录一起划分，保证同一 eTLD+1 不跨划分
    assigned_records = assign_splits(
        [*output_records, *manual_records], extractor=extractor, master_seed=seed
    )

    data_dir = root / "data"
    # 正常域名列表：只取真实来源中的正常榜单与难负样本
    normal_domains = sorted(
        record.domain
        for record in output_records
        if record.source in ("normal_list", "hard_negative")
    )
    # 合成 DGA 明细按 算法顺序、种子序号 排序，params 序列化为紧凑 JSON 便于复现
    dga_rows = [
        {
            "domain": record.domain,
            "algorithm": record.algorithm,
            "seed": record.seed,
            "params": json.dumps(
                record.params,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        }
        for record in sorted(
            generated_records,
            key=lambda record: (
                ALGORITHMS.index(record.algorithm),
                int(record.seed.rsplit(":", 1)[1]),
            ),
        )
    ]
    split_rows = [
        {
            "domain": record.domain,
            "label": record.label,
            "source": record.source,
            "algorithm": record.algorithm,
            "split": record.split,
        }
        # 划分表按域名排序，便于人工核对与版本比对
        for record in sorted(assigned_records, key=lambda record: record.domain)
    ]
    # 三个产物先在内存组装，随后由 _write_output_bundle 原子落盘
    output_bundle = {
        data_dir / "normal_domains.txt": "".join(
            f"{domain}\n" for domain in normal_domains
        ).encode("utf-8"),
        data_dir / "dga_domains.csv": _csv_bytes(
            ["domain", "algorithm", "seed", "params"], dga_rows
        ),
        data_dir / "splits.csv": _csv_bytes(
            ["domain", "label", "source", "algorithm", "split"], split_rows
        ),
    }

    # 汇总各来源文件的行数：原始、有效、剔除与去重
    raw_counts = Counter(row.source_id for row in source_rows)
    valid_counts = Counter(row.source_id for row in eligible_rows)
    excluded_counts = Counter(row.source_id for row in excluded_rows)
    duplicate_counts = _deduplicated_source_counts(eligible_rows)
    source_counts = {
        source_id: {
            "raw": raw_counts[source_id],
            "valid": valid_counts[source_id],
            "excluded": excluded_counts[source_id],
            "deduplicated": duplicate_counts[source_id],
        }
        for source_id in sorted(raw_counts)
    }

    # 统计各划分数量与实际比例，空集时比例记 0
    split_counts = Counter(record.split for record in assigned_records)
    split_counts = {split: split_counts[split] for split in _SPLITS}
    total_records = len(assigned_records)
    split_ratios = {
        split: (split_counts[split] / total_records if total_records else 0.0)
        for split in _SPLITS
    }
    label_counts = {
        str(label): sum(record.label == label for record in assigned_records)
        for label in (0, 1)
    }
    algorithm_diagnostics = {}
    collision_retries = 0
    # 逐算法统计：重试次数 = 最大种子序号 - 实际产出条数（碰撞时序号会跳号）
    for algorithm in ALGORITHMS:
        algorithm_records = sorted(
            (record for record in generated_records if record.algorithm == algorithm),
            key=lambda record: int(record.seed.rsplit(":", 1)[1]),
        )
        algorithm_retry_count = 0
        if algorithm_records:
            final_index = max(
                int(record.seed.rsplit(":", 1)[1]) for record in algorithm_records
            )
            algorithm_retry_count = final_index - len(algorithm_records)
        collision_retries += algorithm_retry_count
        algorithm_diagnostics[algorithm] = AlgorithmDiagnostics(
            count=len(algorithm_records),
            unique_count=len({record.domain for record in algorithm_records}),
            collision_retries=algorithm_retry_count,
            example_domains=[record.domain for record in algorithm_records[:3]],
        )
    algorithm_counts = {
        algorithm: diagnostics.count
        for algorithm, diagnostics in algorithm_diagnostics.items()
    }

    # 记录全部输入文件（来源文件、PSL、词根表、配置）的 SHA-256；路径尽量转为相对项目根的 POSIX 形式
    input_paths = {row.source_path for row in source_rows}
    input_paths.update({psl_path, word_roots_path.resolve(), configuration_path})
    input_hashes = {}
    for path in sorted(input_paths, key=lambda item: item.as_posix()):
        try:
            key = path.relative_to(root).as_posix()
        except ValueError:
            key = path.name
        input_hashes[key] = _sha256_file(path)

    # 剔除明细转为可 JSON 序列化的字典，并按 来源 ID、路径、行号 排序
    exclusion_details = []
    for row in excluded_rows:
        try:
            source_path = row.source_path.resolve().relative_to(root).as_posix()
        except ValueError:
            source_path = row.source_path.name
        exclusion_details.append(
            {
                "source_id": row.source_id,
                "source_path": source_path,
                "line_number": row.line_number,
                "raw_domain": row.raw_domain,
                "reason": row.reason,
            }
        )
    exclusion_details.sort(
        key=lambda row: (row["source_id"], row["source_path"], row["line_number"])
    )

    # 先原子写出产物再返回汇总，保证调用方看到的统计与磁盘内容一致
    summary = RunSummary(
        source_counts=source_counts,
        excluded_rows=exclusion_details,
        public_dga_family_counts=_public_dga_family_summary(
            public_rows, selected_public_rows, limit=300
        ),
        split_counts=split_counts,
        split_ratios=split_ratios,
        label_counts=label_counts,
        algorithm_counts=algorithm_counts,
        algorithm_diagnostics=algorithm_diagnostics,
        collision_retries=collision_retries,
        hard_negative_promoted=hard_negative_promoted,
        psl_sha256=_sha256_file(psl_path),
        input_sha256=input_hashes,
    )
    _write_output_bundle(output_bundle)
    return summary


def main() -> int:
    # 命令行入口：以当前工作目录为项目根、根目录 config.json 为配置执行 D2 流程，成功时打印汇总 JSON 并返回 0，失败返回 1
    project_root = Path.cwd().resolve()
    config_path = project_root / "config.json"
    try:
        summary = run_data_preparation(project_root, config_path)
    except (OSError, ValueError) as exc:
        print(f"D2 data preparation failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            asdict(summary),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
