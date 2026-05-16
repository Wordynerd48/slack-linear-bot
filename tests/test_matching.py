import os
from datetime import date, datetime, timedelta

os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("LINEAR_API_KEY", "test-linear")
os.environ.setdefault("LINEAR_TEAM_ID", "test-team")
os.environ.setdefault("OPENAI_API_KEY", "test-openai")

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import main


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    database_path = tmp_path / "test_slack_linear.db"
    monkeypatch.setattr(main, "DATABASE_PATH", str(database_path))
    main.init_database()
    return database_path


def saved_analysis(source_key="slack-thread:C123:111.222"):
    return {
        "summary": "Evan will fix checkout persistence. Sarah will test it.",
        "decisions": [],
        "action_items": [
            {
                "task": "fix checkout page shipping method persistence",
                "assignee_name": "Evan",
                "due_date": date.today().isoformat(),
                "priority": "high",
                "evidence": "Evan: I can fix the checkout page shipping method persistence by tomorrow.",
            },
            {
                "task": "test shipping method persistence after Evan's fix",
                "assignee_name": "Sarah",
                "due_date": "",
                "priority": "none",
                "evidence": "Sarah: I’ll test shipping method persistence after Evan’s fix.",
            },
        ],
        "blockers": [],
        "unresolved_questions": [
            {
                "question": "Should we also test guest checkout separately?",
                "evidence": "Alex: Should we also test guest checkout separately?",
            }
        ],
        "proposed_issues": [
            {
                "title": "fix checkout page shipping method persistence",
                "description": "fix checkout page shipping method persistence",
                "priority": "high",
                "assignee_name": "Evan",
                "due_date": date.today().isoformat(),
                "evidence": "Evan: I can fix the checkout page shipping method persistence by tomorrow.",
            },
            {
                "title": "test shipping method persistence after Evan's fix",
                "description": "test shipping method persistence after Evan's fix",
                "priority": "none",
                "assignee_name": "Sarah",
                "due_date": "",
                "evidence": "Sarah: I’ll test shipping method persistence after Evan’s fix.",
            },
        ],
    }


def get_items_for_source(source_key):
    with main.get_database_connection() as connection:
        analysis = connection.execute(
            "SELECT id FROM thread_analyses WHERE source_key = ?",
            (source_key,),
        ).fetchone()

        assert analysis is not None

        rows = connection.execute(
            """
            SELECT *
            FROM detected_items
            WHERE analysis_id = ?
            ORDER BY id ASC
            """,
            (analysis["id"],),
        ).fetchall()

    return rows


def item_by_type_and_title(source_key, item_type, title_contains):
    rows = get_items_for_source(source_key)

    for row in rows:
        if row["item_type"] == item_type and title_contains.lower() in row["title"].lower():
            return row

    raise AssertionError(f"No {item_type} item containing {title_contains!r} found")


def risk_titles_and_reasons():
    return {
        risk["title"]: risk["reasons"]
        for risk in main.get_dashboard_risks()
    }


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


def test_ignored_action_item_also_ignores_paired_proposed_issue(temp_database):
    source_key = "slack-thread:C123:ignore-pair"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    action_item = item_by_type_and_title(source_key, "action_item", "test shipping")
    main.ignore_related_detected_items(action_item["id"])

    rows = get_items_for_source(source_key)
    test_rows = [
        row for row in rows
        if "test shipping" in row["title"].lower()
    ]

    assert {row["item_type"] for row in test_rows} == {"action_item", "proposed_issue"}
    assert all(row["status"] == "ignored" for row in test_rows)


def test_ignored_proposed_issue_is_not_createable(temp_database):
    source_key = "slack-thread:C123:ignore-createable"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "test shipping")
    main.ignore_related_detected_items(proposed_issue["id"])

    reanalysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    ignored_proposed = [
        item for item in reanalysis["proposed_issues"]
        if "test shipping" in item["title"].lower()
    ][0]

    assert ignored_proposed["status"] == "ignored"
    assert main.proposed_issue_has_existing_match(ignored_proposed) is True
    assert ignored_proposed not in main.createable_proposed_issues(reanalysis["proposed_issues"])


def test_created_status_is_preserved_across_reanalysis(temp_database):
    source_key = "slack-thread:C123:created-preserve"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_detected_item_created(
        proposed_issue["id"],
        {
            "identifier": "FLO-99",
            "url": "https://linear.app/example/issue/FLO-99",
        },
    )

    reanalysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    fix_proposed = [
        item for item in reanalysis["proposed_issues"]
        if "fix checkout" in item["title"].lower()
    ][0]

    assert fix_proposed["status"] == "created"
    assert fix_proposed["linear_identifier"] == "FLO-99"
    assert fix_proposed["existing_issue_url"] == "https://linear.app/example/issue/FLO-99"
    assert main.proposed_issue_has_existing_match(fix_proposed) is True


def test_ignored_status_survives_fuzzy_reanalysis(temp_database):
    source_key = "slack-thread:C123:fuzzy-ignore"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    question = item_by_type_and_title(source_key, "unresolved_question", "guest checkout")
    main.ignore_related_detected_items(question["id"])

    reanalysis = saved_analysis(source_key)
    reanalysis["unresolved_questions"][0]["question"] = "Should guest checkout be tested separately?"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    rows = get_items_for_source(source_key)
    question_rows = [
        row for row in rows
        if row["item_type"] == "unresolved_question"
    ]

    assert len(question_rows) == 1
    assert question_rows[0]["status"] == "ignored"


def test_dashboard_hides_ignored_items(temp_database):
    source_key = "slack-thread:C123:dashboard-hide"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    question = item_by_type_and_title(source_key, "unresolved_question", "guest checkout")
    main.ignore_related_detected_items(question["id"])

    entries = main.get_recent_thread_analyses()
    entry = next(entry for entry in entries if entry["source_key"] == source_key)

    assert entry["unresolved_questions"] == []


def test_risk_engine_flags_due_soon_high_priority_proposed_issue(temp_database):
    source_key = "slack-thread:C123:risk-due-high"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "High-priority item has not been created or matched" in reasons
    assert "Due within 2 days and not yet tracked" in reasons


def test_risk_engine_flags_old_untracked_proposed_issue(temp_database):
    source_key = "slack-thread:C123:risk-old-proposed"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "none"
    analysis["proposed_issues"][0]["due_date"] = ""

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    old_timestamp = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (old_timestamp, old_timestamp, proposed_issue["id"]),
        )
        connection.commit()

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "Untracked action item older than 24 hours" in reasons


def test_risk_engine_flags_old_unresolved_question(temp_database):
    source_key = "slack-thread:C123:risk-old-question"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    question = item_by_type_and_title(source_key, "unresolved_question", "guest checkout")
    old_timestamp = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (old_timestamp, old_timestamp, question["id"]),
        )
        connection.commit()

    risks = risk_titles_and_reasons()
    reasons = risks["Should we also test guest checkout separately?"]

    assert "Unresolved question older than 48 hours" in reasons


def test_risk_engine_ignores_ignored_created_matched_and_possible_duplicate_items(temp_database):
    source_key = "slack-thread:C123:risk-status-filter"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    old_timestamp = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET status = 'ignored', created_at = ?, updated_at = ?
            WHERE item_type = 'unresolved_question'
            """,
            (old_timestamp, old_timestamp),
        )
        connection.execute(
            """
            UPDATE detected_items
            SET status = 'created', created_at = ?, updated_at = ?
            WHERE item_type = 'proposed_issue'
              AND title LIKE '%fix checkout%'
            """,
            (old_timestamp, old_timestamp),
        )
        connection.execute(
            """
            UPDATE detected_items
            SET status = 'matched', created_at = ?, updated_at = ?
            WHERE item_type = 'proposed_issue'
              AND title LIKE '%test shipping%'
            """,
            (old_timestamp, old_timestamp),
        )
        connection.commit()

    assert main.get_dashboard_risks() == []


def test_blocker_creates_risk_until_ignored(temp_database):
    source_key = "slack-thread:C123:risk-blocker"
    analysis = saved_analysis(source_key)
    analysis["blockers"] = [
        {
            "blocker": "Waiting on production API credentials",
            "owner": "Alex",
            "evidence": "Alex: We are blocked until production API credentials arrive.",
        }
    ]

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    risks = risk_titles_and_reasons()
    assert risks["Waiting on production API credentials"] == ["Blocker still needs review"]

    blocker = item_by_type_and_title(source_key, "blocker", "production API credentials")
    main.ignore_related_detected_items(blocker["id"])

    assert "Waiting on production API credentials" not in risk_titles_and_reasons()