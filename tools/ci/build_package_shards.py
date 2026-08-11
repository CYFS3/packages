#!/usr/bin/env python3
"""Build a deterministic weighted package-test matrix for CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SAFE_PACKAGE_NAME = re.compile(r"[A-Za-z0-9_.+-]+")
SAFE_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class PackageMetadataError(ValueError):
    """Raised when a package.json cannot be used to build a matrix."""


@dataclass(frozen=True)
class Package:
    """The package fields used by the shard planner."""

    name: str
    weight: int
    path: Path


@dataclass
class Shard:
    """A weighted package shard."""

    shard: int
    weight: int
    packages: list[str]


def _read_metadata(package_json: Path) -> Mapping[str, object]:
    try:
        with package_json.open("r", encoding="utf-8-sig") as stream:
            metadata = json.load(stream)
    except (OSError, UnicodeError) as exc:
        raise PackageMetadataError(
            f"Unable to read {package_json}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PackageMetadataError(
            f"Invalid JSON in {package_json}: {exc}"
        ) from exc

    if not isinstance(metadata, dict):
        raise PackageMetadataError(f"Expected an object in {package_json}")
    return metadata


def _package_from_file(package_json: Path) -> Package:
    metadata = _read_metadata(package_json)
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise PackageMetadataError(f"Missing package name in {package_json}")
    if SAFE_PACKAGE_NAME.fullmatch(name) is None:
        raise PackageMetadataError(
            f"Invalid package name {name!r} in {package_json}"
        )

    site = metadata.get("site", [])
    if not isinstance(site, list):
        raise PackageMetadataError(
            f"Expected site to be a list in {package_json}"
        )
    return Package(name=name, weight=max(1, len(site)), path=package_json)


def scan_packages(repo_root: Path) -> list[Package]:
    """Read and validate every current package.json below repo_root."""

    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        raise PackageMetadataError(f"Repository root is not a directory: {repo_root}")

    package_files = sorted(
        (path for path in repo_root.rglob("package.json") if path.is_file()),
        key=lambda path: path.relative_to(repo_root).as_posix(),
    )
    packages = [_package_from_file(path) for path in package_files]

    by_name: dict[str, Path] = {}
    for package in packages:
        previous = by_name.get(package.name)
        if previous is not None:
            raise PackageMetadataError(
                f"Duplicate package name {package.name!r} in "
                f"{previous} and {package.path}"
            )
        by_name[package.name] = package.path
    return packages


def plan_shards(packages: Iterable[Package], shard_count: int) -> list[Shard]:
    """Greedily assign packages to the least-loaded deterministic shard."""

    if shard_count <= 0:
        raise ValueError("shard count must be positive")

    shards = [
        Shard(shard=index, weight=0, packages=[])
        for index in range(1, shard_count + 1)
    ]
    ordered_packages = sorted(
        packages, key=lambda package: (-package.weight, package.name)
    )
    for package in ordered_packages:
        target = min(
            shards,
            key=lambda shard: (shard.weight, len(shard.packages), shard.shard),
        )
        target.weight += package.weight
        target.packages.append(package.name)

    for shard in shards:
        shard.packages.sort()
    return shards


def _split_values(groups: Sequence[Sequence[str]], option: str) -> list[str]:
    values = [
        value
        for group in groups
        for item in group
        for value in item.split()
    ]
    if not values:
        raise ValueError(f"{option} must contain at least one value")
    return values


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return slug or "value"


def _artifact_name(
    shard: int,
    rt_thread_version: str,
    bsp: str,
    used: set[str],
) -> str:
    base = f"pkg-shard-{shard}-{_slug(rt_thread_version)}-{_slug(bsp)}"
    candidate = base
    if candidate in used:
        digest = hashlib.sha256(
            f"{rt_thread_version}\0{bsp}".encode("utf-8")
        ).hexdigest()[:10]
        candidate = f"{base}-{digest}"
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{digest}-{suffix}"
            suffix += 1
    if SAFE_ARTIFACT_NAME.fullmatch(candidate) is None:
        raise ValueError(f"Unable to create safe artifact name from {candidate!r}")
    used.add(candidate)
    return candidate


def build_matrix(
    packages: Iterable[Package],
    shard_count: int,
    rt_thread_versions: Sequence[str],
    bsps: Sequence[str],
) -> dict[str, list[dict[str, object]]]:
    """Build a GitHub matrix include object for all requested combinations."""

    if not rt_thread_versions:
        raise ValueError("at least one RT-Thread version is required")
    if not bsps:
        raise ValueError("at least one BSP is required")

    shards = plan_shards(packages, shard_count)
    include: list[dict[str, object]] = []
    used_artifacts: set[str] = set()
    for rt_thread_version in rt_thread_versions:
        for bsp in bsps:
            for shard in shards:
                include.append(
                    {
                        "shard": shard.shard,
                        "rt_thread_version": rt_thread_version,
                        "bsp": bsp,
                        "packages": " ".join(shard.packages),
                        "artifact_name": _artifact_name(
                            shard.shard,
                            rt_thread_version,
                            bsp,
                            used_artifacts,
                        ),
                    }
                )
    return {"include": include}


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root (defaults to the current directory)",
    )
    parser.add_argument(
        "--shards",
        type=_positive_int,
        required=True,
        help="number of package shards",
    )
    parser.add_argument(
        "--rt-thread-versions",
        action="append",
        nargs="+",
        required=True,
        metavar="VERSION",
        help="RT-Thread selectors; repeat the option or separate values with spaces",
    )
    parser.add_argument(
        "--bsps",
        action="append",
        nargs="+",
        required=True,
        metavar="BSP",
        help="BSP selectors; repeat the option or separate values with spaces",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        versions = _split_values(args.rt_thread_versions, "--rt-thread-versions")
        bsps = _split_values(args.bsps, "--bsps")
        packages = scan_packages(args.repo_root)
        matrix = build_matrix(packages, args.shards, versions, bsps)
    except (OSError, PackageMetadataError, ValueError) as exc:
        print(f"build_package_shards.py: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(matrix, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
