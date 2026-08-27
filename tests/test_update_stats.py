import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GITHUB_TOKEN", "test-token")

from scripts import update_stats


class CommitsSinceTests(unittest.TestCase):
    def assert_since(self, repo: dict, report_since: str, expected_since: str) -> None:
        commits = [{"sha": "abc123"}]
        with patch.object(update_stats, "paginate", return_value=commits) as paginate:
            self.assertEqual(
                update_stats.commits_since(repo, report_since),
                commits,
            )

        paginate.assert_called_once_with(
            f"/repos/{update_stats.ORG}/{repo['name']}/commits",
            {"since": expected_since},
            cap=10,
        )

    def test_recent_fork_starts_at_fork_creation(self) -> None:
        self.assert_since(
            {
                "name": "recent-fork",
                "fork": True,
                "created_at": "2026-08-20T12:00:00Z",
            },
            "2026-05-29T12:00:00Z",
            "2026-08-20T12:00:00Z",
        )

    def test_old_fork_keeps_report_window(self) -> None:
        self.assert_since(
            {
                "name": "old-fork",
                "fork": True,
                "created_at": "2025-01-01T00:00:00Z",
            },
            "2026-05-29T12:00:00Z",
            "2026-05-29T12:00:00Z",
        )

    def test_non_fork_keeps_report_window(self) -> None:
        self.assert_since(
            {
                "name": "original-repo",
                "fork": False,
                "created_at": "2026-08-20T12:00:00Z",
            },
            "2026-05-29T12:00:00Z",
            "2026-05-29T12:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
