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

import tldextract

from .gen_dga import ALGORITHMS, DgaRecord
from .gen_dga import generate_dga_domains


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


@dataclass
class ExcludedRow:
    source_id: str
    source_path: Path
    line_number: int
    raw_domain: str
    reason: str


@dataclass
class DomainRecord:
    domain: str
    label: int
    source: str
    algorithm: str
    etld_plus_one: str
    split: str
    sources: tuple[NormalizedSourceRow, ...]


@dataclass
class AlgorithmDiagnostics:
    count: int
    unique_count: int
    collision_retries: int
    example_domains: list[str]


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
    psl_sha256: str
    input_sha256: dict[str, str]


def normalize_domain(value):
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
    root = Path(raw_dir).resolve()
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
                with path.open("r", encoding="utf-8", newline="") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        rows.append(
                            SourceRow(
                                raw_domain=line.rstrip("\r\n"),
                                label=0,
                                source="normal_list",
                                algorithm="",
                                source_id=source_id,
                                source_path=source_path,
                                line_number=line_number,
                            )
                        )
                continue

            with path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.reader(stream, strict=True)
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
    def key(row):
        digest = hashlib.sha256(
            f"{master_seed}|public_dga|{row.family}|{row.domain}".encode(
                "utf-8"
            )
        ).hexdigest()
        return digest, row.domain

    return key


_SPLITS = ("train", "val", "test")
_SPLIT_RATIOS = {"train": 0.6, "val": 0.2, "test": 0.2}


def _require_sequence(value, name):
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def _source_location(row):
    return f"source ID {row.source_id!r} at {row.source_path}:{row.line_number}"


def _validate_normalized_source_row(row):
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
        if len(semantics) != 1 or len(public_families) > 1:
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
        records.append(
            DomainRecord(
                domain=domain,
                label=first.label,
                source=first.source,
                algorithm=first.algorithm,
                etld_plus_one="",
                split="",
                sources=tuple(domain_rows),
            )
        )
    return records


def manual_domain_records(records: Sequence[DgaRecord]) -> list[DomainRecord]:
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
    records = _require_sequence(records, "records")
    if not callable(extractor):
        raise ValueError("extractor must be callable")
    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise ValueError("master_seed must be an integer")

    normalized_records = []
    seen_domains = set()
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

    splits_by_etld = {}
    for record in assigned:
        splits_by_etld.setdefault(record.etld_plus_one, set()).add(record.split)
    if any(len(splits) != 1 for splits in splits_by_etld.values()):
        raise ValueError("an eTLD+1 group was assigned to multiple splits")

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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _csv_bytes(fieldnames, rows):
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
            temporary_path.replace(target)

    except Exception as exc:
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
    root = Path(project_root).resolve()
    configuration_path = Path(config_path)
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
    if type(seed) is not int or seed != 20261101:
        raise ValueError("config seed must be the integer 20261101")
    paths = config.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("configuration paths must be an object")
    psl_value = paths.get("psl_snapshot")
    if not isinstance(psl_value, str) or not psl_value.strip():
        raise ValueError("configuration paths.psl_snapshot must be a non-empty path")

    psl_path = Path(psl_value)
    if not psl_path.is_absolute():
        psl_path = root / psl_path
    psl_path = psl_path.resolve()
    extractor = create_extractor(psl_path)

    raw_dir = root / "data" / "raw"
    source_rows = load_source_rows(raw_dir)
    normalized_rows, excluded_rows = normalize_source_rows(source_rows)

    # Validate every valid source row before applying the public-DGA sample cap.
    merged_records = merge_and_validate(normalized_rows)
    raw_by_location = {
        (row.source_id, row.source_path.resolve(), row.line_number): row.raw_domain
        for row in source_rows
    }
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
    public_rows = [row for row in eligible_rows if row.source == "public_dga"]
    selected_public_rows = select_public_dga(public_rows, limit=300, master_seed=seed)
    selected_public_domains = {row.domain for row in selected_public_rows}
    output_records = [
        record
        for record in merged_records
        if record.domain in eligible_domains
        and (record.source == "normal_list" or record.domain in selected_public_domains)
    ]

    reserved_domains = {
        record.domain for record in merged_records if record.source == "normal_list"
    } | selected_public_domains
    word_roots_path = raw_dir / "manual_word_roots.txt"
    generated_records = generate_dga_domains(
        master_seed=seed,
        word_roots_path=word_roots_path,
        reserved_domains=reserved_domains,
        count_per_algorithm=100,
    )
    manual_records = manual_domain_records(generated_records)
    assigned_records = assign_splits(
        [*output_records, *manual_records], extractor=extractor, master_seed=seed
    )

    data_dir = root / "data"
    normal_domains = sorted(
        record.domain for record in output_records if record.source == "normal_list"
    )
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
        for record in sorted(assigned_records, key=lambda record: record.domain)
    ]
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

    input_paths = {row.source_path for row in source_rows}
    input_paths.update({psl_path, word_roots_path.resolve(), configuration_path})
    input_hashes = {}
    for path in sorted(input_paths, key=lambda item: item.as_posix()):
        try:
            key = path.relative_to(root).as_posix()
        except ValueError:
            key = path.name
        input_hashes[key] = _sha256_file(path)

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
        psl_sha256=_sha256_file(psl_path),
        input_sha256=input_hashes,
    )
    _write_output_bundle(output_bundle)
    return summary


def main() -> int:
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
