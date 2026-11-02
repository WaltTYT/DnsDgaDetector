"""Deterministically generate teaching-only DGA-like domain samples.

The module implements three reproducible algorithms, records the seed and
parameters needed to rebuild each sample, and writes the contract CSV safely.
It does not model a real malware family or perform network operations.
"""

import csv
import hashlib
import json
import random
import re
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


ALGORITHMS = ("rand_alpha", "word_digit", "md5_date")
TLD = ".com"
DATE = "2026-11-01"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"
_WORD_ROOT_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,19}")
_SEED_PATTERN = re.compile(
    r"(?P<master_seed>-?(?:0|[1-9][0-9]*)):(?P<algorithm>[^:]+):(?P<index>[0-9]{4})"
)


@dataclass(frozen=True)
class DgaRecord:
    domain: str
    algorithm: str
    seed: str
    params: dict[str, int | str]


def _validate_algorithm(algorithm: str) -> None:
    if not isinstance(algorithm, str) or algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm: {algorithm!r}")


def _parse_seed(seed: str, algorithm: str) -> int:
    if not isinstance(seed, str):
        raise ValueError("seed must be a string")
    match = _SEED_PATTERN.fullmatch(seed)
    if match is None:
        raise ValueError("seed has an invalid format or index")
    if match.group("algorithm") != algorithm:
        raise ValueError("seed algorithm does not match algorithm")
    if str(int(match.group("master_seed"))) != match.group("master_seed"):
        raise ValueError("seed master_seed is not canonical")
    index = int(match.group("index"))
    if not 1 <= index <= 9999:
        raise ValueError("seed index must be between 0001 and 9999")
    return index


def _load_word_roots(word_roots_path: Path) -> tuple[list[str], str]:
    try:
        raw_bytes = Path(word_roots_path).read_bytes()
        contents = raw_bytes.decode("utf-8")
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"wordlist could not be read as UTF-8: {exc}") from exc

    roots = []
    for line in contents.splitlines():
        root = line.strip()
        if not root:
            raise ValueError("wordlist must not contain blank lines")
        if not _WORD_ROOT_PATTERN.fullmatch(root) or root.endswith("-"):
            raise ValueError(f"wordlist contains an invalid root: {root!r}")
        roots.append(root)

    if len(set(roots)) < 2:
        raise ValueError("wordlist must contain at least two unique valid roots")
    return roots, hashlib.sha256(raw_bytes).hexdigest()


def _derive_domain(
    algorithm: str,
    seed: str,
    wordlist: tuple[list[str], str] | None = None,
) -> tuple[str, dict[str, int | str]]:
    rng = random.Random(seed)

    if algorithm == "rand_alpha":
        length = rng.randint(12, 18)
        label = "".join(rng.choice(_ALPHABET) for _ in range(length))
        params = {"length": length, "tld": TLD}
    elif algorithm == "word_digit":
        if wordlist is None:
            raise ValueError("wordlist is required for word_digit")
        roots, wordlist_sha256 = wordlist
        root_count = rng.randint(1, 2)
        selected_roots = rng.sample(roots, root_count)
        digit_count = rng.randint(2, 4)
        digits = "".join(rng.choice(_DIGITS) for _ in range(digit_count))
        label = "".join(selected_roots) + digits
        params = {
            "wordlist_sha256": wordlist_sha256,
            "root_count": root_count,
            "digit_count": digit_count,
            "tld": TLD,
        }
    elif algorithm == "md5_date":
        truncation_length = rng.randint(12, 16)
        digest = hashlib.md5(f"20261101|{seed}".encode("utf-8")).hexdigest()
        label = digest[:truncation_length]
        params = {
            "date": DATE,
            "truncation_length": truncation_length,
            "tld": TLD,
        }
    else:
        raise ValueError(f"unknown algorithm: {algorithm!r}")

    return f"{label}{TLD}", params


def _validate_params(
    params: Mapping[str, object], expected: Mapping[str, int | str]
) -> None:
    if not isinstance(params, Mapping) or set(params) != set(expected):
        raise ValueError("params do not match the algorithm contract")
    for key, expected_value in expected.items():
        value = params[key]
        if type(value) is not type(expected_value) or value != expected_value:
            raise ValueError("params do not match the values derived from seed")


def generate_dga_domains(
    master_seed: int,
    word_roots_path: Path,
    reserved_domains: Collection[str] = (),
    count_per_algorithm: int = 100,
    collision_retry_limit: int = 100,
) -> list[DgaRecord]:
    if type(master_seed) is not int:
        raise ValueError("master_seed must be an integer")
    if type(count_per_algorithm) is not int or count_per_algorithm < 0:
        raise ValueError("count_per_algorithm must be a non-negative integer")
    if (
        type(collision_retry_limit) is not int
        or not 1 <= collision_retry_limit <= 100
    ):
        raise ValueError("collision_retry_limit must be between 1 and 100")
    if isinstance(reserved_domains, (str, bytes)):
        raise ValueError("reserved_domains must be a collection of domain strings")
    try:
        used_domains = set(reserved_domains)
    except TypeError as exc:
        raise ValueError(
            "reserved_domains must be a collection of domain strings"
        ) from exc
    if any(not isinstance(domain, str) for domain in used_domains):
        raise ValueError("reserved_domains must contain only strings")

    wordlist = _load_word_roots(word_roots_path)
    records = []
    for algorithm in ALGORITHMS:
        index = 1
        for _ in range(count_per_algorithm):
            collisions = 0
            while True:
                if index > 9999:
                    raise ValueError("sample index exceeds 9999")
                seed = f"{master_seed}:{algorithm}:{index:04d}"
                domain, params = _derive_domain(algorithm, seed, wordlist)
                index += 1
                if domain not in used_domains:
                    used_domains.add(domain)
                    records.append(DgaRecord(domain, algorithm, seed, params))
                    break
                collisions += 1
                if collisions >= collision_retry_limit:
                    raise ValueError(
                        f"collision retry limit reached for {algorithm}"
                    )
    return records


def rebuild_dga_domain(
    algorithm: str,
    seed: str,
    params: Mapping[str, int | str],
    word_roots_path: Path | None = None,
) -> str:
    _validate_algorithm(algorithm)
    _parse_seed(seed, algorithm)
    wordlist = (
        _load_word_roots(word_roots_path) if algorithm == "word_digit" else None
    )
    domain, expected_params = _derive_domain(algorithm, seed, wordlist)

    if algorithm == "word_digit" and isinstance(params, Mapping):
        supplied_hash = params.get("wordlist_sha256")
        if (
            type(supplied_hash) is not str
            or supplied_hash != expected_params["wordlist_sha256"]
        ):
            raise ValueError("wordlist hash does not match params")
    _validate_params(params, expected_params)
    return domain


def write_dga_csv(
    records: Sequence[DgaRecord],
    target: Path,
    word_roots_path: Path | None = None,
) -> None:
    target_path = Path(target)
    prepared = []
    for record in records:
        params_json = json.dumps(
            record.params,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        _validate_algorithm(record.algorithm)
        index = _parse_seed(record.seed, record.algorithm)
        expected_domain = rebuild_dga_domain(
            record.algorithm,
            record.seed,
            record.params,
            word_roots_path,
        )
        if record.domain != expected_domain:
            raise ValueError(
                "record domain does not match algorithm, seed, and params"
            )
        prepared.append(
            (
                (ALGORITHMS.index(record.algorithm), index),
                {
                    "domain": record.domain,
                    "algorithm": record.algorithm,
                    "seed": record.seed,
                    "params": params_json,
                },
            )
        )
    rows = [row for _, row in sorted(prepared, key=lambda item: item[0])]

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            writer = csv.DictWriter(
                stream,
                fieldnames=["domain", "algorithm", "seed", "params"],
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        temp_path.replace(target_path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
