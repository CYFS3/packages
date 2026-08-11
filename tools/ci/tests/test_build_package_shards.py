from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "build_package_shards.py"
REPO_ROOT = SCRIPT.parents[2]
SAFE_ARTIFACT = re.compile(r"[A-Za-z0-9_.-]+")


class BuildPackageShardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_package(
        self,
        relative_path: str,
        name: str,
        site_count: int = 1,
    ) -> None:
        path = self.repo / relative_path / "package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"name": name, "site": [{} for _ in range(site_count)]}
        path.write_text(json.dumps(metadata), encoding="utf-8")

    def run_planner(
        self,
        *extra_args: str,
        check: bool = True,
        repo: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo or self.repo),
            *extra_args,
        ]
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

    def make_packages(self) -> None:
        for index, weight in enumerate((8, 7, 6, 5, 4, 3, 2, 1)):
            self.write_package(f"group{index}/package", f"pkg{index}", weight)

    def test_output_is_deterministic_and_has_complete_assignment(self) -> None:
        self.make_packages()
        args = (
            "--shards",
            "3",
            "--rt-thread-versions",
            "branch:master tag:v4.1.1",
            "--bsps",
            "qemu-vexpress-a9:sourcery-arm",
        )
        first = self.run_planner(*args)
        second = self.run_planner(*args)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(len(first.stdout.splitlines()), 1)

        entries = json.loads(first.stdout)["include"]
        assigned = [name for entry in entries[:3] for name in entry["packages"].split()]
        self.assertCountEqual(assigned, [f"pkg{index}" for index in range(8)])
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_weighted_greedy_assignment_is_balanced(self) -> None:
        self.make_packages()
        result = self.run_planner(
            "--shards",
            "3",
            "--rt-thread-versions",
            "v1",
            "--bsps",
            "bsp1",
        )
        entries = json.loads(result.stdout)["include"]
        weights = {f"pkg{index}": weight for index, weight in enumerate((8, 7, 6, 5, 4, 3, 2, 1))}
        shard_weights = [
            sum(weights[name] for name in entry["packages"].split())
            for entry in entries
        ]
        self.assertEqual(len(shard_weights), 3)
        self.assertLessEqual(max(shard_weights) - min(shard_weights), 2)
        self.assertEqual(shard_weights, [13, 12, 11])

    def test_matrix_cartesian_count_and_safe_unique_artifacts(self) -> None:
        self.write_package("one", "one")
        self.write_package("two", "two")
        result = self.run_planner(
            "--shards",
            "2",
            "--rt-thread-versions",
            "branch:main branch-main",
            "--bsps",
            "bsp/a bsp:a",
        )
        entries = json.loads(result.stdout)["include"]
        artifacts = [entry["artifact_name"] for entry in entries]
        self.assertEqual(len(entries), 2 * 2 * 2)
        self.assertEqual(len(artifacts), len(set(artifacts)))
        self.assertTrue(all(SAFE_ARTIFACT.fullmatch(name) for name in artifacts))
        self.assertTrue(all(set(entry) == {"shard", "rt_thread_version", "bsp", "packages", "artifact_name"} for entry in entries))

    def test_invalid_metadata_fails(self) -> None:
        path = self.repo / "broken" / "package.json"
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        result = self.run_planner(
            "--shards",
            "1",
            "--rt-thread-versions",
            "v1",
            "--bsps",
            "bsp1",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid JSON", result.stderr)

    def test_duplicate_and_unsafe_names_fail(self) -> None:
        self.write_package("one", "same")
        self.write_package("two", "same")
        result = self.run_planner(
            "--shards",
            "1",
            "--rt-thread-versions",
            "v1",
            "--bsps",
            "bsp1",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate package name", result.stderr)

        self.repo = Path(self.temp_dir.name) / "unsafe"
        self.write_package("one", "unsafe name")
        result = self.run_planner(
            "--shards",
            "1",
            "--rt-thread-versions",
            "v1",
            "--bsps",
            "bsp1",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid package name", result.stderr)

    def test_real_repository_dry_run_is_compact(self) -> None:
        result = self.run_planner(
            "--shards",
            "12",
            "--rt-thread-versions",
            "branch:master tag:v4.1.1",
            "--bsps",
            "stm32/stm32h750-artpi:sourcery-arm k210:sourcery-riscv-none-embed qemu-vexpress-a9:sourcery-arm",
            repo=REPO_ROOT,
        )
        self.assertLess(len(result.stdout.encode("utf-8")), 1_000_000)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        matrix = json.loads(result.stdout)
        self.assertEqual(len(matrix["include"]), 12 * 2 * 3)
        self.assertEqual(
            len({entry["artifact_name"] for entry in matrix["include"]}),
            12 * 2 * 3,
        )


if __name__ == "__main__":
    unittest.main()
