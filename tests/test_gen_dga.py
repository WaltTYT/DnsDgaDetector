import csv
import hashlib
import json
import re

import pytest

from src import gen_dga


ALGORITHMS = ("rand_alpha", "word_digit", "md5_date")


@pytest.fixture
def word_roots_path(tmp_path):
    path = tmp_path / "manual_word_roots.txt"
    path.write_text("alpha\nnetwork\npacket\n", encoding="utf-8", newline="")
    return path


def test_generates_one_hundred_unique_domains_per_algorithm(word_roots_path):
    records = gen_dga.generate_dga_domains(20261101, word_roots_path)

    assert len(records) == 300
    assert len({record.domain for record in records}) == 300
    assert [record.algorithm for record in records] == [
        algorithm for algorithm in ALGORITHMS for _ in range(100)
    ]


def test_generation_is_deterministic_for_a_fixed_seed(word_roots_path):
    first = gen_dga.generate_dga_domains(20261101, word_roots_path)
    second = gen_dga.generate_dga_domains(20261101, word_roots_path)
    other_seed = gen_dga.generate_dga_domains(20261102, word_roots_path)

    assert first == second
    assert any(a.domain != b.domain for a, b in zip(first, other_seed))


def test_records_can_be_rebuilt_from_seed_and_params(word_roots_path):
    records = gen_dga.generate_dga_domains(20261101, word_roots_path)

    for record in records:
        assert set(record.__dict__) == {"domain", "algorithm", "seed", "params"}
        assert record.seed
        assert isinstance(json.loads(json.dumps(record.params)), dict)
        assert gen_dga.rebuild_dga_domain(
            record.algorithm, record.seed, record.params, word_roots_path
        ) == record.domain


def test_generated_domains_follow_each_algorithm_contract(word_roots_path):
    records = gen_dga.generate_dga_domains(20261101, word_roots_path)

    for record in records:
        label = record.domain.removesuffix(".com")
        assert record.domain.endswith(".com")
        if record.algorithm == "rand_alpha":
            assert re.fullmatch(r"[a-z]{12,18}", label)
            assert record.params == {"length": len(label), "tld": ".com"}
        elif record.algorithm == "word_digit":
            assert re.fullmatch(r"[a-z][a-z0-9-]*[0-9]{2,4}", label)
            assert record.params["root_count"] in (1, 2)
            assert record.params["digit_count"] in (2, 3, 4)
            assert len(label[-record.params["digit_count"] :]) == record.params["digit_count"]
            assert record.params["tld"] == ".com"
        else:
            assert record.algorithm == "md5_date"
            assert re.fullmatch(r"[0-9a-f]{12,16}", label)
            assert record.params["date"] == "2026-11-01"
            assert 12 <= record.params["truncation_length"] <= 16
            expected = hashlib.md5(
                f"20261101|{record.seed}".encode("utf-8")
            ).hexdigest()[: record.params["truncation_length"]]
            assert label == expected


def test_rebuild_rejects_unknown_algorithm_and_inconsistent_params(word_roots_path):
    record = gen_dga.generate_dga_domains(20261101, word_roots_path, count_per_algorithm=1)[0]
    invalid_params = dict(record.params, length=record.params["length"] + 1)

    with pytest.raises(ValueError, match="algorithm"):
        gen_dga.rebuild_dga_domain("unknown", record.seed, record.params, word_roots_path)
    with pytest.raises(ValueError, match="params"):
        gen_dga.rebuild_dga_domain(
            record.algorithm, record.seed, invalid_params, word_roots_path
        )


def test_rebuild_rejects_a_changed_wordlist(word_roots_path, tmp_path):
    record = gen_dga.generate_dga_domains(20261101, word_roots_path, count_per_algorithm=1)[1]
    changed_roots = tmp_path / "changed_roots.txt"
    changed_roots.write_text("different\nroots\n", encoding="utf-8", newline="")

    with pytest.raises(ValueError, match="wordlist"):
        gen_dga.rebuild_dga_domain(
            record.algorithm, record.seed, record.params, changed_roots
        )


def test_collision_retries_advance_sample_index(word_roots_path):
    initial = gen_dga.generate_dga_domains(
        20261101, word_roots_path, count_per_algorithm=1
    )
    reserved = {record.domain for record in initial}
    retried = gen_dga.generate_dga_domains(
        20261101, word_roots_path, reserved_domains=reserved, count_per_algorithm=1
    )

    assert [record.algorithm for record in retried] == list(ALGORITHMS)
    assert all(record.domain not in reserved for record in retried)
    assert all(record.seed.endswith(":0002") for record in retried)


def test_collision_limit_and_invalid_value_fail_without_partial_records(word_roots_path):
    first_two = gen_dga.generate_dga_domains(
        20261101, word_roots_path, count_per_algorithm=2
    )[:2]
    reserved = {record.domain for record in first_two}

    with pytest.raises(ValueError, match="collision"):
        gen_dga.generate_dga_domains(
            20261101,
            word_roots_path,
            reserved_domains=reserved,
            count_per_algorithm=1,
            collision_retry_limit=2,
        )
    with pytest.raises(ValueError, match="collision_retry_limit"):
        gen_dga.generate_dga_domains(
            20261101, word_roots_path, collision_retry_limit=101
        )


def test_csv_writer_uses_stable_contract_and_preserves_target_on_error(
    word_roots_path, tmp_path
):
    records = gen_dga.generate_dga_domains(
        20261101, word_roots_path, count_per_algorithm=1
    )
    target = tmp_path / "dga_domains.csv"

    gen_dga.write_dga_csv(records, target, word_roots_path=word_roots_path)
    first_bytes = target.read_bytes()
    gen_dga.write_dga_csv(records, target, word_roots_path=word_roots_path)
    assert target.read_bytes() == first_bytes
    assert not first_bytes.startswith(b"\xef\xbb\xbf")
    with target.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["domain", "algorithm", "seed", "params"]
        rows = list(reader)
    assert [row["algorithm"] for row in rows] == list(ALGORITHMS)
    assert all(json.loads(row["params"]) for row in rows)

    target.write_bytes(b"sentinel")
    invalid_record = gen_dga.DgaRecord(
        "invalid.com", "rand_alpha", "seed", {"unsupported": object()}
    )
    with pytest.raises(TypeError):
        gen_dga.write_dga_csv([invalid_record], target)
    assert target.read_bytes() == b"sentinel"
    assert list(tmp_path.glob("*.tmp")) == []


def test_csv_writer_rejects_domain_inconsistent_with_seed_and_params(
    word_roots_path, tmp_path
):
    generated = gen_dga.generate_dga_domains(
        20261101, word_roots_path, count_per_algorithm=1
    )[0]
    mismatched = gen_dga.DgaRecord(
        "different-domain.com",
        generated.algorithm,
        generated.seed,
        generated.params,
    )
    target = tmp_path / "dga_domains.csv"
    target.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="domain"):
        gen_dga.write_dga_csv([mismatched], target)

    assert target.read_bytes() == b"sentinel"
    assert list(tmp_path.glob("*.tmp")) == []
