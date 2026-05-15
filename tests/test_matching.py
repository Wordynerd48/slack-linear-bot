import os

os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("LINEAR_API_KEY", "test-linear")
os.environ.setdefault("LINEAR_TEAM_ID", "test-team")
os.environ.setdefault("OPENAI_API_KEY", "test-openai")

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import main


def test_task_type_detects_test_task():
    assert main.task_type("test shipping method persistence") == "test"


def test_task_type_detects_implementation_task():
    assert main.task_type("fix checkout page bug") == "implementation"


def test_test_task_and_fix_task_are_incompatible_duplicates():
    proposed = {"title": "test shipping method persistence after Evan's fix"}
    existing = {"title": "fix checkout page issue with shipping method persistence"}

    assert main.incompatible_task_types(proposed, existing) is True


def test_question_only_item_is_not_trackable():
    action_item = {
        "task": "Should we also test guest checkout separately?",
        "assignee_name": "",
        "due_date": "",
        "priority": "none",
        "evidence": "Alex: Should we also test guest checkout separately?",
    }

    assert main.is_trackable_action_item(action_item) is False


def test_action_item_with_owner_becomes_proposed_issue():
    action_items = [
        {
            "task": "test shipping method persistence after Evan's fix",
            "assignee_name": "Sarah",
            "due_date": "",
            "priority": "none",
            "evidence": "Sarah: I’ll test shipping method persistence after Evan’s fix.",
        }
    ]

    proposed_issues = main.build_proposed_issues_from_action_items(action_items)

    assert len(proposed_issues) == 1
    assert proposed_issues[0]["title"] == "test shipping method persistence after Evan's fix"
    assert proposed_issues[0]["assignee_name"] == "Sarah"


def test_same_thread_test_task_does_not_match_same_thread_fix_issue():
    source_key = "slack-thread:C123:111.222"
    source_url = "https://example.slack.com/archives/C123/p111222"

    proposed = {
        "title": "test shipping method persistence after Evan's fix",
        "description": "test shipping method persistence after Evan's fix",
        "evidence": "Sarah: I’ll test shipping method persistence after Evan’s fix.",
    }

    existing = {
        "identifier": "FLO-1",
        "title": "fix checkout page issue with shipping method persistence",
        "description": (
            "Fix checkout page issue with shipping method persistence\n\n"
            f"Source Slack thread:\n{source_url}\n\n"
            f"Slack source key:\n{source_key}\n\n"
            "Evidence:\nEvan: The checkout page loses the selected shipping method after refresh."
        ),
        "url": "https://linear.app/example/issue/FLO-1",
    }

    match = main.find_existing_linear_issue_match(
        proposed,
        [existing],
        source_url=source_url,
        source_key=source_key,
    )

    assert match is None


def test_same_thread_fix_task_matches_same_thread_fix_issue():
    source_key = "slack-thread:C123:111.222"
    source_url = "https://example.slack.com/archives/C123/p111222"

    proposed = {
        "title": "fix checkout page shipping method persistence",
        "description": "fix checkout page shipping method persistence",
        "evidence": "Evan: I can fix it by Wednesday.",
    }

    existing = {
        "identifier": "FLO-2",
        "title": "fix checkout page issue with shipping method persistence",
        "description": (
            "Fix checkout page issue with shipping method persistence\n\n"
            f"Source Slack thread:\n{source_url}\n\n"
            f"Slack source key:\n{source_key}\n\n"
            "Evidence:\nEvan: The checkout page loses the selected shipping method after refresh."
        ),
        "url": "https://linear.app/example/issue/FLO-2",
    }

    match = main.find_existing_linear_issue_match(
        proposed,
        [existing],
        source_url=source_url,
        source_key=source_key,
    )

    assert match is existing
