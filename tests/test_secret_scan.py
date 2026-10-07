"""Focused tests for warnings about unquoted sensitive assignments."""

from __future__ import annotations

import time

import pytest

from trails.secret_scan import scan_for_secrets


@pytest.mark.parametrize(
    ("line", "detector"),
    [
        ("password = hunter2-Xq9vLm", "KeywordDetector"),
        ("password: hunter2-Xq9vLm", "KeywordDetector"),
        ("Set `password = hunter2-Xq9vLm` in the config.", "KeywordDetector"),
        ("api_key = sk_9f8e7d6c5b4a3210fedcba98", "KeywordDetector"),
        ("DB_PASSWORD=Tr0ub4dor3x", "KeywordDetector"),
        ("export DB_PASSWORD=hunter2-Xq9vLm", "KeywordDetector"),
        ("SECRET_KEY=hunter2-Xq9vLm", "KeywordDetector"),
        ("client_secret = 7f3a9c2e1b8d4f60", "KeywordDetector"),
        ("MY_API_KEY=sk_9f8e7d6c5b4a3210", "KeywordDetector"),
        ("export GITHUB_TOKEN=abc123def456ghi", "KeywordDetector"),
        ("password=Xq9v/Lm2zz", "KeywordDetector"),
        ("DB_PASSWORD=hunter2/backup", "KeywordDetector"),
        ("secret_key=abc/def123", "KeywordDetector"),
        ("password=summer-sale-2024", "KeywordDetector"),
        ("token=ghp-like-v2", "KeywordDetector"),
        ("mysql --password=Tr0ub4dor3x -h db", "KeywordDetector"),
        ("mysql --password Tr0ub4dor3x -h db", "KeywordDetector"),
        ("client --api-key=sk_9f8e7d6c5b4a3210", "KeywordDetector"),
        ("client --api-key sk_9f8e7d6c5b4a3210", "KeywordDetector"),
        (
            "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWSKeyDetector",
        ),
        (
            'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
            "AWSKeyDetector",
        ),
    ],
)
def test_unquoted_sensitive_assignments_are_reported_once(line: str, detector: str) -> None:
    """Catches pasted shell, config and Markdown assignments escaping quote-based plugins,
    or the fallback duplicating a native detector's finding for a quoted value."""
    matching = [item for item in scan_for_secrets({"body": line}) if item.detector == detector]

    assert [(item.field, item.line) for item in matching] == [("body", 1)]


@pytest.mark.parametrize(
    "line",
    [
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "0123456789abcdef0123456789abcdef01234567",
        "rec-7cb6f45ac185f6ea",
        "hyp-k3m9z",
        "note-8a0xrf",
        "res-7cb6f45ac185f6ea",
        "all-MiniLM-L6-v2/model_O4",
        "/Users/ddardzin/projects/agent-trails/src/trails/secret_scan.py",
        "s3://bucket/runs/2026/out.parquet",
        "PWD=/Users/ddardzin/projects/agent-trails",
        "pwd: /Users/ddardzin/projects/agent-trails",
        "token: rec-7cb6f45ac185f6ea",
        "token: 0123456789abcdef0123456789abcdef01234567",
        "secret: s3://bucket/runs/2026/out.parquet",
        "secret = arn:aws:secretsmanager:us-east-1:123456789012:secret:db-1",
        "token=all-MiniLM-L6-v2",
        "token=all-MiniLM-L6-v2/model_O4",
        "next_page_token: AbC123xyz789",
        "page_token: AbC123xyz789",
        "continuation_token: AbC123xyz789",
        "mysql --password-file=/tmp/password.txt -h db",
        "client --api-key-file /tmp/api-key.txt",
        "the password: field is required",
        "password = required",
        "api_key = example",
    ],
)
def test_digest_identifiers_paths_and_prose_do_not_become_secret_warnings(line: str) -> None:
    """Catches a broad entropy or keyword fallback producing warnings agents learn to ignore."""
    assert scan_for_secrets({"body": line}) == []


def test_a_long_hyphenated_non_assignment_is_scanned_in_linear_time() -> None:
    """Catches the sensitive-name prefix starting at every hyphen and backtracking through
    the rest of a long line. The replaced pattern took 30.9 seconds at this length."""
    line = "a-" * 25_000 + "password"

    started = time.perf_counter()
    findings = scan_for_secrets({"body": line})
    elapsed = time.perf_counter() - started

    assert findings == []
    assert elapsed < 2.0
