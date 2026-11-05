import csv
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from src import gen_dga, prepare_data
from src.gen_dga import DgaRecord


def write_psl(path):
    path.write_text(
        "com\nuk\nco.uk\n// ===BEGIN PRIVATE DOMAINS===\nblogspot.com\n",
        encoding="utf-8",
        newline="",
    )
    return path


def make_source_row(
    raw_domain,
    source_id="source-a",
    source="public_dga",
    label=1,
    algorithm="",
    line_number=2,
    family="family-a",
):
    return prepare_data.SourceRow(
        raw_domain=raw_domain,
        label=label,
        source=source,
        algorithm=algorithm,
        source_id=source_id,
        source_path=Path(f"{source_id}.csv"),
        line_number=line_number,
        family=family,
    )


def make_normalized_row(
    domain,
    source_id="source-a",
    source="public_dga",
    label=1,
    algorithm="",
    line_number=2,
    family="family-a",
):
    return prepare_data.NormalizedSourceRow(
        domain=domain,
        label=label,
        source=source,
        algorithm=algorithm,
        source_id=source_id,
        source_path=Path(f"{source_id}.csv"),
        line_number=line_number,
        family=family,
    )


def make_domain_record(
    domain,
    label=0,
    source="normal_list",
    algorithm="",
    etld_plus_one="",
    split="",
):
    return prepare_data.DomainRecord(
        domain=domain,
        label=label,
        source=source,
        algorithm=algorithm,
        etld_plus_one=etld_plus_one,
        split=split,
        sources=(),
    )


@pytest.fixture
def local_extractor(tmp_path):
    return prepare_data.create_extractor(write_psl(tmp_path / "public_suffix_list.dat"))


def write_source_files(raw_dir, normal="normal.example\n", hard="hard.example\n", public=None):
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "normal_list__popular.txt").write_text(
        normal, encoding="utf-8", newline=""
    )
    (raw_dir / "hard_negative__numeric.txt").write_text(
        hard, encoding="utf-8", newline=""
    )
    public_path = raw_dir / "public_dga__families.csv"
    with public_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["domain", "family"])
        writer.writerows(public or [("dga.example", "family-a")])
    return public_path


def make_preparation_project(
    project_root, normal_domains, hard_negative_domains, public_domains
):
    raw_dir = project_root / "data" / "raw"
    normal_text = "".join(f"{domain}\n" for domain in normal_domains)
    hard_text = "".join(f"{domain}\n" for domain in hard_negative_domains)
    write_source_files(
        raw_dir,
        normal=normal_text,
        hard=hard_text,
        public=public_domains,
    )
    write_psl(raw_dir / "public_suffix_list.dat")
    (raw_dir / "manual_word_roots.txt").write_text(
        "alpha\nbeta\n", encoding="utf-8", newline=""
    )
    config_path = project_root / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "seed": 20261101,
                "paths": {"psl_snapshot": "data/raw/public_suffix_list.dat"},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="",
    )
    return config_path


def read_csv_output(path):
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def test_normalize_domain_trims_case_root_dot_and_encodes_idna():
    assert prepare_data.normalize_domain("  BÜCHER.Example.  ") == (
        "xn--bcher-kva.example"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "192.0.2.1",
        "2001:db8::1",
        "*.example.com",
        "bad..example",
        "-bad.example",
        "bad-.example",
        "bad_name.example",
        f"{'a' * 64}.example",
        ".".join(["a" * 63] * 4),
    ],
)
def test_normalize_domain_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        prepare_data.normalize_domain(value)


def test_normalize_source_rows_separates_invalid_domains_with_provenance():
    rows = [
        make_source_row("Normal.Example.", source="normal_list", label=0),
        make_source_row("bad..example", source="normal_list", label=0, line_number=3),
    ]

    valid, excluded = prepare_data.normalize_source_rows(rows)

    assert [row.domain for row in valid] == ["normal.example"]
    assert len(excluded) == 1
    assert excluded[0].source_id == "source-a"
    assert excluded[0].line_number == 3
    assert excluded[0].raw_domain == "bad..example"
    assert excluded[0].reason


def test_load_source_rows_reads_only_contract_files_and_maps_semantics(tmp_path):
    raw_dir = tmp_path / "raw"
    public_path = write_source_files(
        raw_dir,
        normal="Normal.Example.\nsecond.example\n",
        hard="hashed123.example\n",
        public=[("dga.example", " family-a ")],
    )
    snapshots = raw_dir / "snapshots"
    snapshots.mkdir()
    (snapshots / "normal_list__ignored.txt").write_text(
        "ignored.example\n", encoding="utf-8"
    )

    rows = prepare_data.load_source_rows(raw_dir)

    assert [(row.source_id, row.line_number) for row in rows] == [
        ("popular", 1),
        ("popular", 2),
        ("numeric", 1),
        ("families", 2),
    ]
    assert [(row.label, row.source, row.algorithm) for row in rows] == [
        (0, "normal_list", ""),
        (0, "normal_list", ""),
        (0, "hard_negative", ""),
        (1, "public_dga", ""),
    ]
    assert rows[-1].family == "family-a"
    assert rows[-1].source_path == public_path


def test_load_source_rows_rejects_public_dga_family_named_legit(tmp_path):
    raw_dir = tmp_path / "raw"
    write_source_files(raw_dir, public=[("dga.example", " LeGiT ")])

    with pytest.raises(
        ValueError,
        match=r"public_dga__families\.csv:2:.*legit",
    ):
        prepare_data.load_source_rows(raw_dir)


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("public_dga__bad.csv", "domain,family\ndga.example,\n", r"bad.csv:2"),
        ("public_dga__bad.csv", "domain\ndga.example\n", r"bad.csv:1"),
        (
            "public_dga__bad.csv",
            "domain,family\ndga.example,family-a,extra\n",
            r"bad.csv:2",
        ),
    ],
)
def test_load_source_rows_reports_public_csv_structure_errors(
    tmp_path, filename, content, message
):
    raw_dir = tmp_path / "raw"
    write_source_files(raw_dir)
    (raw_dir / filename).write_text(content, encoding="utf-8", newline="")

    with pytest.raises(ValueError, match=message):
        prepare_data.load_source_rows(raw_dir)


def test_load_source_rows_requires_each_input_category(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    with pytest.raises(ValueError, match="normal_list"):
        prepare_data.load_source_rows(raw_dir)


def test_public_dga_sampling_uses_largest_remainder_and_hash_order():
    family_sizes = {"family-a": 101, "family-b": 90, "family-c": 79, "family-d": 60}
    rows = [
        make_normalized_row(
            f"{family[-1]}{index:03d}.example",
            source_id=family,
            family=family,
            line_number=index + 2,
        )
        for family, size in family_sizes.items()
        for index in range(size)
    ]

    selected = prepare_data.select_public_dga(rows, limit=300, master_seed=20261101)
    counts = {}
    for row in selected:
        counts[row.family] = counts.get(row.family, 0) + 1

    assert counts == {"family-a": 92, "family-b": 82, "family-c": 72, "family-d": 54}
    for family, quota in counts.items():
        expected = sorted(
            (row for row in rows if row.family == family),
            key=lambda row: (
                hashlib.sha256(
                    f"20261101|public_dga|{family}|{row.domain}".encode("utf-8")
                ).hexdigest(),
                row.domain,
            ),
        )[:quota]
        assert {row.domain for row in selected if row.family == family} == {
            row.domain for row in expected
        }


def test_public_dga_sampling_is_input_order_independent_and_deduplicates():
    rows = [
        make_normalized_row(f"dga{index:03d}.example", family="family-a", line_number=index)
        for index in range(12)
    ]
    duplicated = rows + [rows[0]]

    first = prepare_data.select_public_dga(duplicated, limit=300, master_seed=1)
    second = prepare_data.select_public_dga(
        list(reversed(duplicated)), limit=300, master_seed=1
    )

    assert [row.domain for row in first] == [row.domain for row in second]
    assert len(first) == len(rows)


def test_public_dga_sampling_rejects_family_conflicts_and_rejects_bad_limit():
    rows = [
        make_normalized_row("same.example", source_id="source-a", family="family-a"),
        make_normalized_row("same.example", source_id="source-b", family="family-b"),
    ]

    with pytest.raises(ValueError, match="same.example"):
        prepare_data.select_public_dga(rows, limit=300, master_seed=1)
    with pytest.raises(ValueError, match="limit"):
        prepare_data.select_public_dga([], limit=0, master_seed=1)


def test_create_extractor_uses_local_psl_with_private_domains_and_no_network(
    tmp_path, monkeypatch
):
    psl_path = write_psl(tmp_path / "public_suffix_list.dat")

    def fail_network(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)
    extractor = prepare_data.create_extractor(psl_path)

    co_uk = extractor("www.example.co.uk")
    private = extractor("a.account.blogspot.com")
    assert f"{co_uk.domain}.{co_uk.suffix}" == "example.co.uk"
    assert f"{private.domain}.{private.suffix}" == "account.blogspot.com"


@pytest.mark.parametrize("contents", ["", "not-a-suffix-rule\n"])
def test_create_extractor_rejects_missing_or_invalid_local_psl(tmp_path, contents):
    psl_path = tmp_path / "bad.dat"
    psl_path.write_text(contents, encoding="utf-8", newline="")

    with pytest.raises(ValueError, match="PSL"):
        prepare_data.create_extractor(psl_path)


def test_merge_and_validate_folds_duplicate_semantics_and_retains_provenance(tmp_path):
    rows = [
        make_normalized_row(
            "duplicate.example",
            source_id="families-a",
            line_number=4,
            family="family-a",
        ),
        make_normalized_row(
            "duplicate.example",
            source_id="families-b",
            line_number=9,
            family="family-a",
        ),
    ]
    rows[0].source_path = tmp_path / "public_dga__a.csv"
    rows[1].source_path = tmp_path / "public_dga__b.csv"

    records = prepare_data.merge_and_validate(rows)

    assert len(records) == 1
    assert (records[0].domain, records[0].label, records[0].source) == (
        "duplicate.example",
        1,
        "public_dga",
    )
    assert records[0].algorithm == ""
    assert {
        (row.source_id, row.source_path, row.line_number, row.family)
        for row in records[0].sources
    } == {
        ("families-a", tmp_path / "public_dga__a.csv", 4, "family-a"),
        ("families-b", tmp_path / "public_dga__b.csv", 9, "family-a"),
    }


def test_merge_and_validate_promotes_hard_negative_and_retains_provenance(tmp_path):
    rows = [
        make_normalized_row(
            "shared.example",
            source_id="popular",
            source="normal_list",
            label=0,
            algorithm="",
            line_number=2,
            family=None,
        ),
        make_normalized_row(
            "shared.example",
            source_id="numeric",
            source="hard_negative",
            label=0,
            algorithm="",
            line_number=1,
            family=None,
        ),
    ]
    rows[0].source_path = tmp_path / "normal_list__popular.txt"
    rows[1].source_path = tmp_path / "hard_negative__numeric.txt"

    records = prepare_data.merge_and_validate(rows)

    assert len(records) == 1
    assert (records[0].domain, records[0].label, records[0].source) == (
        "shared.example",
        0,
        "hard_negative",
    )
    assert {
        (row.source_id, row.source, row.line_number) for row in records[0].sources
    } == {
        ("popular", "normal_list", 2),
        ("numeric", "hard_negative", 1),
    }


def test_merge_and_validate_rejects_cross_label_conflict_with_provenance(tmp_path):
    rows = [
        make_normalized_row(
            "collision.example",
            source_id="normal-source",
            source="normal_list",
            label=0,
            algorithm="",
            line_number=3,
            family=None,
        ),
        make_normalized_row(
            "collision.example",
            source_id="dga-source",
            source="public_dga",
            label=1,
            algorithm="",
            line_number=7,
            family="family-a",
        ),
    ]
    rows[0].source_path = tmp_path / "normal.txt"
    rows[1].source_path = tmp_path / "public_dga.csv"

    with pytest.raises(ValueError) as error:
        prepare_data.merge_and_validate(rows)

    message = str(error.value)
    assert "collision.example" in message
    for detail in (
        "normal-source",
        "normal.txt",
        ":3",
        "dga-source",
        "public_dga.csv",
        ":7",
    ):
        assert detail in message


def test_merge_and_validate_rejects_public_family_conflict_with_provenance(tmp_path):
    rows = [
        make_normalized_row(
            "family-collision.example",
            source_id="family-source-a",
            line_number=5,
            family="family-a",
        ),
        make_normalized_row(
            "family-collision.example",
            source_id="family-source-b",
            line_number=11,
            family="family-b",
        ),
    ]
    rows[0].source_path = tmp_path / "public-a.csv"
    rows[1].source_path = tmp_path / "public-b.csv"

    with pytest.raises(ValueError) as error:
        prepare_data.merge_and_validate(rows)

    message = str(error.value)
    assert "family-collision.example" in message
    for detail in (
        "family-source-a",
        "public-a.csv",
        ":5",
        "family-source-b",
        "public-b.csv",
        ":11",
    ):
        assert detail in message


def test_manual_domain_records_maps_dga_records_to_shared_schema():
    dga_records = [
        DgaRecord("random.example.com", "rand_alpha", "7:rand_alpha:0001", {}),
        DgaRecord("roots.example.com", "word_digit", "7:word_digit:0001", {}),
        DgaRecord("digest.example.com", "md5_date", "7:md5_date:0001", {}),
    ]

    records = prepare_data.manual_domain_records(dga_records)

    assert [
        (record.domain, record.label, record.source, record.algorithm)
        for record in records
    ] == [
        (record.domain, 1, "manual_dga", record.algorithm)
        for record in dga_records
    ]
    assert all(record.sources == () for record in records)


def test_assign_splits_keeps_etld_plus_one_groups_together_and_distinguishes_suffixes(
    local_extractor,
):
    records = [
        make_domain_record("one.example.com"),
        make_domain_record("two.example.com"),
        make_domain_record("one.example.co.uk"),
        make_domain_record("two.example.co.uk"),
    ]

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)
    by_domain = {record.domain: record for record in assigned}

    assert by_domain["one.example.com"].etld_plus_one == "example.com"
    assert by_domain["two.example.com"].etld_plus_one == "example.com"
    assert by_domain["one.example.co.uk"].etld_plus_one == "example.co.uk"
    assert by_domain["two.example.co.uk"].etld_plus_one == "example.co.uk"
    assert by_domain["one.example.com"].split == by_domain["two.example.com"].split
    assert by_domain["one.example.co.uk"].split == by_domain["two.example.co.uk"].split
    assert by_domain["one.example.com"].etld_plus_one != by_domain[
        "one.example.co.uk"
    ].etld_plus_one


def test_assign_splits_uses_seeded_hash_ties_and_d2_loss_independent_of_input_order(
    local_extractor,
):
    records = [
        make_domain_record(f"host.{group}.com")
        for group in ("a", "b", "c", "d", "e")
    ]
    expected = {
        "a.com": "test",
        "b.com": "train",
        "c.com": "train",
        "d.com": "train",
        "e.com": "val",
    }

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)
    reversed_assigned = prepare_data.assign_splits(
        list(reversed(records)), local_extractor, master_seed=17
    )

    assert {record.etld_plus_one: record.split for record in assigned} == expected
    assert {
        record.etld_plus_one: record.split for record in reversed_assigned
    } == expected


def test_assign_splits_uses_d2_loss_for_uneven_group_sizes_and_strata(local_extractor):
    group_rows = {
        "a": [("normal_list", 0)] * 4,
        "b": [("normal_list", 0)],
        "c": [("normal_list", 0)],
        "d": [("public_dga", 1)] * 2,
        "e": [("public_dga", 1)],
        "f": [("public_dga", 1)],
    }
    records = [
        make_domain_record(
            f"host{index}.{group}.com",
            label=label,
            source=source,
        )
        for group, rows in group_rows.items()
        for index, (source, label) in enumerate(rows)
    ]

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)

    assert {record.etld_plus_one: record.split for record in assigned} == {
        "a.com": "train",
        "b.com": "val",
        "c.com": "test",
        "d.com": "train",
        "e.com": "test",
        "f.com": "val",
    }


def test_assign_splits_keeps_large_groups_intact_without_ratio_threshold(
    local_extractor,
):
    records = [
        make_domain_record(f"host{index}.large.com") for index in range(8)
    ] + [
        make_domain_record("host.a.com"),
        make_domain_record("host.b.com"),
        make_domain_record("host.c.com"),
        make_domain_record("host.d.com"),
    ]

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)

    large_group_splits = {
        record.split for record in assigned if record.etld_plus_one == "large.com"
    }
    split_counts = {
        split: sum(record.split == split for record in assigned)
        for split in ("train", "val", "test")
    }
    actual_ratios = {
        split: count / len(assigned) for split, count in split_counts.items()
    }
    assert len(assigned) == len(records)
    assert len(large_group_splits) == 1
    assert sum(split_counts.values()) == len(records)
    assert actual_ratios != {"train": 0.6, "val": 0.2, "test": 0.2}


def test_assign_splits_covers_each_manual_algorithm_in_every_split(local_extractor):
    algorithms = ("rand_alpha", "word_digit", "md5_date")
    records = [
        make_domain_record(
            f"{algorithm.replace('_', '-')}.{group}.com",
            label=1,
            source="manual_dga",
            algorithm=algorithm,
        )
        for group in ("a", "b", "c")
        for algorithm in algorithms
    ]

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)

    assert {
        algorithm: {
            record.split for record in assigned if record.algorithm == algorithm
        }
        for algorithm in algorithms
    } == {algorithm: {"train", "val", "test"} for algorithm in algorithms}


def test_assign_splits_rejects_manual_coverage_impossible_with_one_etld_group(
    local_extractor,
):
    records = [
        make_domain_record(
            f"{algorithm.replace('_', '-')}.shared.com",
            label=1,
            source="manual_dga",
            algorithm=algorithm,
        )
        for algorithm in ("rand_alpha", "word_digit", "md5_date")
    ]

    with pytest.raises(ValueError) as error:
        prepare_data.assign_splits(records, local_extractor, master_seed=17)

    message = str(error.value)
    for detail in ("rand_alpha", "word_digit", "md5_date", "train", "val", "test"):
        assert detail in message


def test_assign_splits_rejects_domain_without_registered_domain(local_extractor):
    records = [make_domain_record("localhost")]

    with pytest.raises(ValueError, match="localhost"):
        prepare_data.assign_splits(records, local_extractor, master_seed=17)


def test_assign_splits_normalizes_domain_records_before_grouping(local_extractor):
    records = [make_domain_record("EXAMPLE.COM"), make_domain_record("example.com")]

    with pytest.raises(ValueError, match="duplicate domain record: 'example.com'"):
        prepare_data.assign_splits(records, local_extractor, master_seed=17)


def test_assign_splits_normalizes_single_record_before_grouping(local_extractor):
    records = [make_domain_record("EXAMPLE.COM.")]

    assigned = prepare_data.assign_splits(records, local_extractor, master_seed=17)

    assert assigned[0].domain == "example.com"
    assert assigned[0].etld_plus_one == "example.com"


def test_assign_splits_rejects_malformed_domain_records(local_extractor):
    records = [make_domain_record("bad..example.com")]

    with pytest.raises(ValueError, match="invalid domain"):
        prepare_data.assign_splits(records, local_extractor, master_seed=17)


def test_run_data_preparation_is_offline_deterministic_and_preserves_dns_log(
    tmp_path, monkeypatch
):
    word_roots = tmp_path / "roots.txt"
    word_roots.write_text("alpha\nbeta\n", encoding="utf-8", newline="")
    collision_candidate = gen_dga.generate_dga_domains(
        master_seed=20261101,
        word_roots_path=word_roots,
        count_per_algorithm=1,
    )[0].domain

    project_root = tmp_path / "project"
    config_path = make_preparation_project(
        project_root,
        normal_domains=[
            collision_candidate,
            "www.normal.com",
            "shop.example.co.uk",
            "blogspot.com",
            "missing-root.unknownzone",
        ],
        hard_negative_domains=[
            "www.normal.com",
            "account.blogspot.com",
            "co.uk",
        ],
        public_domains=[
            ("dga-one.com", "family-a"),
            ("dga-two.co.uk", "family-b"),
            ("dga-three.blogspot.com", "family-a"),
            ("dga-four.bazar", "family-b"),
        ],
    )
    data_dir = project_root / "data"
    dns_log_path = data_dir / "dns_log.csv"
    dns_log_path.write_bytes(b"dns-log sentinel\n")

    def fail_network(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)

    summary = prepare_data.run_data_preparation(project_root, config_path)
    output_paths = (
        data_dir / "normal_domains.txt",
        data_dir / "dga_domains.csv",
        data_dir / "splits.csv",
    )
    first_output_bytes = [path.read_bytes() for path in output_paths]

    normal_domains = (data_dir / "normal_domains.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert normal_domains == sorted(set(normal_domains))
    assert set(normal_domains) == {
        collision_candidate,
        "www.normal.com",
        "shop.example.co.uk",
        "account.blogspot.com",
    }
    assert {
        source_id: (counts["raw"], counts["valid"], counts["excluded"])
        for source_id, counts in summary.source_counts.items()
    } == {
        "families": (4, 3, 1),
        "numeric": (3, 2, 1),
        "popular": (5, 3, 2),
    }
    assert sum(
        counts["deduplicated"] for counts in summary.source_counts.values()
    ) == 0
    assert summary.hard_negative_promoted == 1
    assert {
        (row["source_id"], row["line_number"], row["raw_domain"])
        for row in summary.excluded_rows
    } == {
        ("popular", 4, "blogspot.com"),
        ("popular", 5, "missing-root.unknownzone"),
        ("numeric", 3, "co.uk"),
        ("families", 5, "dga-four.bazar"),
    }
    assert all("eTLD+1" in row["reason"] for row in summary.excluded_rows)
    assert summary.public_dga_family_counts == {
        "family-a": {"available": 2, "quota": 2, "selected": 2},
        "family-b": {"available": 1, "quota": 1, "selected": 1},
    }

    dga_header, dga_rows = read_csv_output(data_dir / "dga_domains.csv")
    assert dga_header == ["domain", "algorithm", "seed", "params"]
    assert len(dga_rows) == 300
    assert len({row["domain"] for row in dga_rows}) == 300
    assert {
        algorithm: sum(row["algorithm"] == algorithm for row in dga_rows)
        for algorithm in gen_dga.ALGORITHMS
    } == {algorithm: 100 for algorithm in gen_dga.ALGORITHMS}
    assert all(row["seed"] for row in dga_rows)
    assert all(isinstance(json.loads(row["params"]), dict) for row in dga_rows)
    first_rand_alpha = next(
        row for row in dga_rows if row["algorithm"] == "rand_alpha"
    )
    assert first_rand_alpha["seed"].endswith(":0002")

    split_header, split_rows = read_csv_output(data_dir / "splits.csv")
    assert split_header == ["domain", "label", "source", "algorithm", "split"]
    assert len({row["domain"] for row in split_rows}) == len(split_rows)
    assert {row["source"] for row in split_rows} == {
        "normal_list",
        "hard_negative",
        "public_dga",
        "manual_dga",
    }
    assert all(
        row["algorithm"] == ""
        for row in split_rows
        if row["source"] != "manual_dga"
    )

    extractor = prepare_data.create_extractor(
        data_dir / "raw" / "public_suffix_list.dat"
    )
    splits_by_group = {}
    for row in split_rows:
        extracted = extractor(row["domain"])
        group = f"{extracted.domain}.{extracted.suffix}"
        splits_by_group.setdefault(group, set()).add(row["split"])
    assert all(len(splits) == 1 for splits in splits_by_group.values())

    expected_collision_retries = sum(
        max(
            int(row["seed"].rsplit(":", 1)[1])
            for row in dga_rows
            if row["algorithm"] == algorithm
        )
        - 100
        for algorithm in gen_dga.ALGORITHMS
    )
    assert summary.collision_retries == expected_collision_retries
    assert summary.collision_retries >= 1
    assert summary.algorithm_counts == {
        algorithm: 100 for algorithm in gen_dga.ALGORITHMS
    }
    for algorithm in gen_dga.ALGORITHMS:
        algorithm_rows = [
            row for row in dga_rows if row["algorithm"] == algorithm
        ]
        expected_retries = max(
            int(row["seed"].rsplit(":", 1)[1]) for row in algorithm_rows
        ) - len(algorithm_rows)
        diagnostics = summary.algorithm_diagnostics[algorithm]
        assert diagnostics.count == 100
        assert diagnostics.unique_count == 100
        assert diagnostics.collision_retries == expected_retries
        assert diagnostics.example_domains == [
            row["domain"] for row in algorithm_rows[:3]
        ]
    assert dns_log_path.read_bytes() == b"dns-log sentinel\n"

    prepare_data.run_data_preparation(project_root, config_path)
    assert [path.read_bytes() for path in output_paths] == first_output_bytes
    assert dns_log_path.read_bytes() == b"dns-log sentinel\n"


def test_run_data_preparation_conflict_preserves_all_existing_outputs(tmp_path):
    project_root = tmp_path / "project"
    config_path = make_preparation_project(
        project_root,
        normal_domains=["conflict.example.com"],
        hard_negative_domains=["safe.example.co.uk"],
        public_domains=[("conflict.example.com", "family-a")],
    )
    data_dir = project_root / "data"
    output_paths = (
        data_dir / "normal_domains.txt",
        data_dir / "dga_domains.csv",
        data_dir / "splits.csv",
    )
    for index, path in enumerate(output_paths):
        path.write_bytes(f"sentinel-{index}\n".encode("ascii"))
    dns_log_path = data_dir / "dns_log.csv"
    dns_log_path.write_bytes(b"dns-log sentinel\n")
    before = [path.read_bytes() for path in (*output_paths, dns_log_path)]

    with pytest.raises(ValueError, match="conflict.example.com"):
        prepare_data.run_data_preparation(project_root, config_path)

    assert [path.read_bytes() for path in (*output_paths, dns_log_path)] == before


def test_write_output_bundle_restores_all_outputs_when_second_promotion_fails(
    tmp_path, monkeypatch
):
    outputs = {
        tmp_path / f"output-{index}.csv": f"new-{index}\n".encode("ascii")
        for index in range(3)
    }
    original_contents = {
        path: f"sentinel-{index}\n".encode("ascii")
        for index, path in enumerate(outputs)
    }
    for path, contents in original_contents.items():
        path.write_bytes(contents)

    original_replace = Path.replace
    promotion_count = 0
    failure_injected = False

    def fail_second_promotion_once(source, destination):
        nonlocal promotion_count, failure_injected
        destination = Path(destination)
        if source.suffix == ".tmp" and destination in outputs:
            promotion_count += 1
            if promotion_count == 2 and not failure_injected:
                failure_injected = True
                raise OSError("injected second-promotion failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_second_promotion_once)

    with pytest.raises(OSError, match="injected second-promotion failure"):
        prepare_data._write_output_bundle(outputs)

    assert failure_injected
    assert promotion_count == 2
    assert {path: path.read_bytes() for path in outputs} == original_contents
    assert all(not list(tmp_path.glob(f".{path.name}.*")) for path in outputs)


def test_write_output_bundle_removes_new_outputs_when_second_promotion_fails(
    tmp_path, monkeypatch
):
    outputs = {
        tmp_path / f"new-output-{index}.csv": f"new-{index}\n".encode("ascii")
        for index in range(3)
    }
    assert all(not path.exists() for path in outputs)

    original_replace = Path.replace
    promotion_count = 0
    failure_injected = False

    def fail_second_promotion_once(source, destination):
        nonlocal promotion_count, failure_injected
        destination = Path(destination)
        if source.suffix == ".tmp" and destination in outputs:
            promotion_count += 1
            if promotion_count == 2 and not failure_injected:
                failure_injected = True
                raise OSError("injected second-promotion failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_second_promotion_once)

    with pytest.raises(OSError, match="injected second-promotion failure"):
        prepare_data._write_output_bundle(outputs)

    assert failure_injected
    assert promotion_count == 2
    assert all(not path.exists() for path in outputs)
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak"))


def test_write_output_bundle_restores_dangling_symlink_on_promotion_failure(
    tmp_path, monkeypatch
):
    link_path = tmp_path / "dangling-link.csv"
    expected_link_target = "missing-target.csv"
    try:
        os.symlink(expected_link_target, link_path)
    except OSError:
        link_path.write_bytes(b"original dangling symlink entry")
        symlink_targets = {link_path: expected_link_target}
    else:
        symlink_targets = {}

    existing_path = tmp_path / "existing.csv"
    existing_path.write_bytes(b"existing sentinel")
    new_path = tmp_path / "new.csv"
    outputs = {
        link_path: b"replacement link bytes",
        existing_path: b"replacement existing bytes",
        new_path: b"new output bytes",
    }

    original_exists = Path.exists
    original_is_symlink = Path.is_symlink

    def exists_following_symlinks(path):
        if path in symlink_targets:
            return False
        return original_exists(path)

    def is_symlink(path):
        if path in symlink_targets:
            return True
        return original_is_symlink(path)

    def replace_entry(source, destination):
        destination = Path(destination)
        result = original_replace(source, destination)
        symlink_target = symlink_targets.pop(source, None)
        symlink_targets.pop(destination, None)
        if symlink_target is not None:
            symlink_targets[destination] = symlink_target
        return result

    monkeypatch.setattr(Path, "exists", exists_following_symlinks)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    original_replace = Path.replace
    promotion_count = 0

    def fail_second_promotion_once(source, destination):
        nonlocal promotion_count
        destination = Path(destination)
        if source.suffix == ".tmp" and destination in outputs:
            promotion_count += 1
            if promotion_count == 2:
                raise OSError("injected second-promotion failure")
        return replace_entry(source, destination)

    monkeypatch.setattr(Path, "replace", fail_second_promotion_once)

    with pytest.raises(OSError, match="injected second-promotion failure"):
        prepare_data._write_output_bundle(outputs)

    assert link_path.is_symlink()
    if symlink_targets:
        assert symlink_targets == {link_path: expected_link_target}
        assert link_path.read_bytes() == b"original dangling symlink entry"
    else:
        assert os.readlink(link_path) == expected_link_target
    assert existing_path.read_bytes() == b"existing sentinel"
    assert not new_path.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak"))


def test_write_output_bundle_reports_recoverable_backup_when_rollback_fails(
    tmp_path, monkeypatch
):
    outputs = {
        tmp_path / f"output-{index}.csv": f"new-{index}\n".encode("ascii")
        for index in range(3)
    }
    original_contents = {
        path: f"sentinel-{index}\n".encode("ascii")
        for index, path in enumerate(outputs)
    }
    for path, contents in original_contents.items():
        path.write_bytes(contents)

    first_target = next(iter(outputs))
    original_replace = Path.replace
    promotion_count = 0
    promotion_failure_injected = False
    rollback_failure_injected = False

    def fail_promotion_and_one_restore(source, destination):
        nonlocal promotion_count
        nonlocal promotion_failure_injected, rollback_failure_injected
        destination = Path(destination)
        if source.suffix == ".tmp" and destination in outputs:
            promotion_count += 1
            if promotion_count == 2 and not promotion_failure_injected:
                promotion_failure_injected = True
                raise OSError("injected second-promotion failure")
        if (
            source.suffix == ".bak"
            and destination == first_target
            and not rollback_failure_injected
        ):
            rollback_failure_injected = True
            raise OSError("injected rollback failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_promotion_and_one_restore)

    with pytest.raises(RuntimeError, match=r"recoverable backups: .*\.bak") as exc_info:
        prepare_data._write_output_bundle(outputs)

    assert promotion_failure_injected
    assert rollback_failure_injected
    recoverable_backups = list(tmp_path.glob(f".{first_target.name}.*.bak"))
    assert len(recoverable_backups) == 1
    assert str(recoverable_backups[0]) in str(exc_info.value)
    assert recoverable_backups[0].read_bytes() == original_contents[first_target]


def test_main_runs_from_current_project_root_and_prints_json_summary(
    tmp_path, monkeypatch, capsys
):
    summary = prepare_data.RunSummary(
        source_counts={"normal": {"raw": 1, "valid": 1, "excluded": 0, "deduplicated": 0}},
        excluded_rows=[],
        public_dga_family_counts={},
        split_counts={"train": 1, "val": 0, "test": 0},
        split_ratios={"train": 1.0, "val": 0.0, "test": 0.0},
        label_counts={"0": 1, "1": 0},
        algorithm_counts={algorithm: 0 for algorithm in gen_dga.ALGORITHMS},
        algorithm_diagnostics={
            algorithm: prepare_data.AlgorithmDiagnostics(0, 0, 0, [])
            for algorithm in gen_dga.ALGORITHMS
        },
        collision_retries=0,
        hard_negative_promoted=0,
        psl_sha256="psl-hash",
        input_sha256={},
    )
    calls = []

    def fake_run(project_root, config_path):
        calls.append((project_root, config_path))
        return summary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prepare_data, "run_data_preparation", fake_run)

    assert prepare_data.main() == 0
    assert calls == [(tmp_path.resolve(), tmp_path.resolve() / "config.json")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["split_counts"] == {
        "train": 1,
        "val": 0,
        "test": 0,
    }
    assert payload["algorithm_diagnostics"] == {
        algorithm: {
            "count": 0,
            "unique_count": 0,
            "collision_retries": 0,
            "example_domains": [],
        }
        for algorithm in gen_dga.ALGORITHMS
    }


def test_main_reports_pipeline_error_and_returns_nonzero(
    tmp_path, monkeypatch, capsys
):
    def fail_run(project_root, config_path):
        raise ValueError("source conflict")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prepare_data, "run_data_preparation", fail_run)

    assert prepare_data.main() != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "source conflict" in captured.err
