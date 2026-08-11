import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("packages_ci", REPO_ROOT / "ci.py")
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)


def package_record(root, name, site_count=1):
    path = root / name / "package.json"
    metadata = {
        "name": name,
        "site": [{} for _ in range(site_count)],
    }
    return CI.PackageRecord(path, metadata)


class UrlValidationTests(unittest.TestCase):
    def setUp(self):
        CI.determine_url_valid.cache_clear()

    def test_reachable_url_is_valid(self):
        response = mock.Mock(status_code=200)
        with mock.patch.object(CI.requests, "get", return_value=response) as get:
            self.assertTrue(CI.determine_url_valid("https://github.com/RT-Thread/packages"))

        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], CI.REQUEST_TIMEOUT_SECONDS)
        response.close.assert_called_once()

    def test_http_error_is_retried_and_rejected(self):
        response = mock.Mock(status_code=500)
        with mock.patch.object(CI.requests, "get", return_value=response) as get:
            with mock.patch.object(CI.time, "sleep"):
                self.assertFalse(
                    CI.determine_url_valid("https://github.com/RT-Thread/missing")
                )

        self.assertEqual(get.call_count, CI.URL_RETRIES)

    def test_unsupported_host_is_rejected_without_request(self):
        with mock.patch.object(CI.requests, "get") as get:
            self.assertFalse(CI.determine_url_valid("https://example.com/pkg.zip"))
        get.assert_not_called()

    def test_lookalike_github_host_is_rejected(self):
        with mock.patch.object(CI.requests, "get") as get:
            self.assertFalse(
                CI.determine_url_valid("https://github.com.evil.invalid/pkg.zip")
            )
        get.assert_not_called()


class PackageSelectionTests(unittest.TestCase):
    def test_explicit_package_selection_is_sorted(self):
        root = Path("repo")
        records = [
            package_record(root, "gamma"),
            package_record(root, "alpha"),
            package_record(root, "beta"),
        ]

        selected = CI.select_packages(records, package_names=["gamma", "alpha"])

        self.assertEqual([record.name for record in selected], ["alpha", "gamma"])

    def test_unknown_package_is_rejected(self):
        records = [package_record(Path("repo"), "alpha")]
        with self.assertRaisesRegex(ValueError, "missing"):
            CI.select_packages(records, package_names=["missing"])

    def test_weighted_shards_are_deterministic_and_complete(self):
        root = Path("repo")
        records = [
            package_record(root, "alpha", 8),
            package_record(root, "beta", 5),
            package_record(root, "gamma", 3),
            package_record(root, "delta", 2),
            package_record(root, "epsilon", 1),
            package_record(root, "zeta", 1),
        ]

        shards, loads = CI._build_shards(records, 3)
        repeated, repeated_loads = CI._build_shards(list(reversed(records)), 3)

        self.assertEqual(
            [[record.name for record in shard] for shard in shards],
            [[record.name for record in shard] for shard in repeated],
        )
        self.assertEqual(loads, repeated_loads)
        self.assertEqual(
            sorted(record.name for shard in shards for record in shard),
            sorted(record.name for record in records),
        )
        self.assertLessEqual(max(loads) - min(loads), max(record.weight for record in records))

    def test_weight_ignores_site_entries_without_urls(self):
        record = CI.PackageRecord(
            Path("repo/alpha/package.json"),
            {"name": "alpha", "site": [{"URL": ""}, {}, {"URL": "https://github.com/a/b.git"}]},
        )
        self.assertEqual(record.weight, 2)


class AggregateValidationTests(unittest.TestCase):
    def test_all_selected_packages_are_checked_after_failure(self):
        records = [
            package_record(Path("repo"), "alpha"),
            package_record(Path("repo"), "beta"),
        ]
        with mock.patch.object(
            CI, "json_file_content_check", side_effect=[False, True]
        ) as content_check:
            with mock.patch.object(CI, "file_path_check", return_value=True):
                self.assertFalse(CI.check_package_records(records))

        self.assertEqual(content_check.call_count, 2)

    def test_discovery_keeps_invalid_json_for_its_shard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_dir = root / "alpha"
            invalid_dir = root / "broken"
            valid_dir.mkdir()
            invalid_dir.mkdir()
            (valid_dir / "package.json").write_text(
                '{"name":"alpha","site":[]}', encoding="utf-8"
            )
            (invalid_dir / "package.json").write_text("{", encoding="utf-8")

            records = CI.discover_packages(root)

        self.assertEqual(len(records), 2)
        self.assertEqual(sum(record.error is not None for record in records), 1)


class ArgumentValidationTests(unittest.TestCase):
    def test_shard_arguments_must_be_paired(self):
        with self.assertRaises(SystemExit):
            CI.main(["--shard-count", "8"])

    def test_package_and_shard_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            CI.main(
                [
                    "--packages",
                    "alpha",
                    "--shard-count",
                    "8",
                    "--shard-index",
                    "0",
                ]
            )


if __name__ == "__main__":
    unittest.main()
