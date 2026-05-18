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

def call_mark_tracked(item_id):
    """Support the current helper name, with a fallback if we rename it later."""
    if hasattr(main, "mark_related_detected_items_tracked"):
        return main.mark_related_detected_items_tracked(item_id)

    if hasattr(main, "mark_detected_item_tracked"):
        return main.mark_detected_item_tracked(item_id)

    raise AssertionError("No mark-tracked helper found in main.py")


def call_snooze(item_id, hours=24):
    """Support the current helper name, with a fallback if we rename it later."""
    if hasattr(main, "snooze_related_detected_items"):
        return main.snooze_related_detected_items(item_id, hours=hours)

    if hasattr(main, "snooze_detected_item"):
        return main.snooze_detected_item(item_id, hours=hours)

    raise AssertionError("No snooze helper found in main.py")


def test_mark_tracked_action_item_also_marks_paired_proposed_issue(temp_database):
    source_key = "slack-thread:C123:mark-tracked-pair"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    action_item = item_by_type_and_title(source_key, "action_item", "fix checkout")
    call_mark_tracked(action_item["id"])

    rows = get_items_for_source(source_key)
    fix_rows = [
        row for row in rows
        if "fix checkout" in row["title"].lower()
    ]

    assert {row["item_type"] for row in fix_rows} == {"action_item", "proposed_issue"}
    assert all(row["status"] == "matched" for row in fix_rows)


def test_mark_tracked_proposed_issue_is_not_createable(temp_database):
    source_key = "slack-thread:C123:mark-tracked-createable"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_mark_tracked(proposed_issue["id"])

    reanalysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    fix_proposed = [
        item for item in reanalysis["proposed_issues"]
        if "fix checkout" in item["title"].lower()
    ][0]

    assert fix_proposed["status"] == "matched"
    assert main.proposed_issue_has_existing_match(fix_proposed) is True
    assert fix_proposed not in main.createable_proposed_issues(reanalysis["proposed_issues"])


def test_mark_tracked_removes_item_from_risks(temp_database):
    source_key = "slack-thread:C123:mark-tracked-risk"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    assert "fix checkout page shipping method persistence" in risk_titles_and_reasons()

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_mark_tracked(proposed_issue["id"])

    assert "fix checkout page shipping method persistence" not in risk_titles_and_reasons()


def test_mark_tracked_status_survives_reanalysis(temp_database):
    source_key = "slack-thread:C123:mark-tracked-preserve"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_mark_tracked(proposed_issue["id"])

    reanalysis = saved_analysis(source_key)
    reanalysis["proposed_issues"][0]["title"] = "fix checkout shipping method persistence issue"
    reanalysis["action_items"][0]["task"] = "fix checkout shipping method persistence issue"

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    rows = get_items_for_source(source_key)
    fix_rows = [
        row for row in rows
        if row["item_type"] in {"action_item", "proposed_issue"}
        and "fix checkout" in row["title"].lower()
    ]

    assert fix_rows
    assert all(row["status"] == "matched" for row in fix_rows)


def test_snooze_sets_snoozed_until_on_item_and_pair(temp_database):
    source_key = "slack-thread:C123:snooze-pair"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    action_item = item_by_type_and_title(source_key, "action_item", "fix checkout")
    call_snooze(action_item["id"], hours=24)

    rows = get_items_for_source(source_key)
    fix_rows = [
        row for row in rows
        if "fix checkout" in row["title"].lower()
    ]

    assert {row["item_type"] for row in fix_rows} == {"action_item", "proposed_issue"}
    assert all(row["snoozed_until"] for row in fix_rows)


def test_snoozed_proposed_issue_is_temporarily_not_createable(temp_database):
    source_key = "slack-thread:C123:snooze-createable"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_snooze(proposed_issue["id"], hours=24)

    reanalysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    fix_proposed = [
        item for item in reanalysis["proposed_issues"]
        if "fix checkout" in item["title"].lower()
    ][0]

    assert fix_proposed.get("snoozed_until")
    assert main.proposed_issue_has_existing_match(fix_proposed) is True
    assert fix_proposed not in main.createable_proposed_issues(reanalysis["proposed_issues"])


def test_snoozed_item_does_not_show_in_risks_until_expired(temp_database):
    source_key = "slack-thread:C123:snooze-risk"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    assert "fix checkout page shipping method persistence" in risk_titles_and_reasons()

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_snooze(proposed_issue["id"], hours=24)

    assert "fix checkout page shipping method persistence" not in risk_titles_and_reasons()


def test_expired_snooze_allows_risk_to_return(temp_database):
    source_key = "slack-thread:C123:snooze-expired"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_snooze(proposed_issue["id"], hours=24)

    expired_time = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

    with main.get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET snoozed_until = ?
            WHERE id = ?
            """,
            (expired_time, proposed_issue["id"]),
        )
        connection.commit()

    risks = risk_titles_and_reasons()
    assert "fix checkout page shipping method persistence" in risks


def test_snooze_status_survives_reanalysis(temp_database):
    source_key = "slack-thread:C123:snooze-preserve"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_snooze(proposed_issue["id"], hours=24)

    reanalysis = saved_analysis(source_key)
    reanalysis["proposed_issues"][0]["title"] = "fix checkout shipping method persistence issue"
    reanalysis["action_items"][0]["task"] = "fix checkout shipping method persistence issue"

    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    rows = get_items_for_source(source_key)
    fix_rows = [
        row for row in rows
        if row["item_type"] in {"action_item", "proposed_issue"}
        and "fix checkout" in row["title"].lower()
    ]

    assert fix_rows
    assert all(row["snoozed_until"] for row in fix_rows)

def test_channel_thread_parent_messages_keeps_only_real_thread_parents():
    messages = [
        {
            "ts": "111.000",
            "text": "Real parent thread",
            "reply_count": 2,
        },
        {
            "ts": "222.000",
            "text": "No replies",
            "reply_count": 0,
        },
        {
            "ts": "333.000",
            "text": "Bot message",
            "reply_count": 2,
            "subtype": "bot_message",
        },
        {
            "ts": "444.000",
            "text": "Join message",
            "reply_count": 2,
            "subtype": "channel_join",
        },
        {
            "ts": "111.000",
            "thread_ts": "111.000",
            "text": "Duplicate same thread",
            "reply_count": 3,
        },
    ]

    thread_parents = main.channel_thread_parent_messages(messages)

    assert len(thread_parents) == 1
    assert thread_parents[0]["ts"] == "111.000"


def test_channel_thread_parent_messages_uses_thread_ts_when_present():
    messages = [
        {
            "ts": "111.001",
            "thread_ts": "111.000",
            "text": "Thread parent style message",
            "reply_count": 2,
        }
    ]

    thread_parents = main.channel_thread_parent_messages(messages)

    assert len(thread_parents) == 1
    assert thread_parents[0]["thread_ts"] == "111.000"


def test_scan_channel_analyzes_new_thread_and_saves_result(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)

    def fake_fetch_slack_channel_messages(channel_id_arg, lookback_hours=24):
        assert channel_id_arg == channel_id
        assert lookback_hours == 24
        return [
            {
                "ts": thread_ts,
                "text": "Parent",
                "reply_count": 2,
            }
        ]

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        assert channel_id_arg == channel_id
        assert thread_ts_arg == thread_ts
        analysis = saved_analysis(source_key)
        main.save_thread_analysis(
            source_key,
            "https://example.slack.com/thread",
            analysis,
        )
        return analysis

    monkeypatch.setattr(main, "fetch_slack_channel_messages", fake_fetch_slack_channel_messages)
    monkeypatch.setattr(main, "analyze_and_save_thread_from_channel_scan", fake_analyze_and_save_thread_from_channel_scan)

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=24)

    assert result == {
        "threads_found": 1,
        "analyzed": 1,
        "skipped_existing": 0,
        "failed": 0,
    }

    rows = get_items_for_source(source_key)
    assert any(row["item_type"] == "proposed_issue" for row in rows)


def test_scan_channel_skips_existing_thread(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata={"last_seen_reply_ts": thread_ts, "reply_count": 2},
    )

    analyze_calls = []

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": thread_ts,
                "text": "Parent",
                "reply_count": 2,
            }
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        analyze_calls.append((channel_id_arg, thread_ts_arg))

    monkeypatch.setattr(main, "analyze_and_save_thread_from_channel_scan", fake_analyze_and_save_thread_from_channel_scan)

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=24)

    assert result == {
        "threads_found": 1,
        "analyzed": 0,
        "skipped_existing": 1,
        "failed": 0,
    }
    assert analyze_calls == []


def test_scan_channel_counts_failed_thread_without_stopping(temp_database, monkeypatch):
    channel_id = "C123"

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": "111.000",
                "text": "First parent",
                "reply_count": 2,
            },
            {
                "ts": "222.000",
                "text": "Second parent",
                "reply_count": 2,
            },
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        if thread_ts_arg == "111.000":
            raise RuntimeError("fake scan failure")

        source_key = main.build_slack_source_key(channel_id_arg, thread_ts_arg)
        main.save_thread_analysis(
            source_key,
            "https://example.slack.com/thread",
            saved_analysis(source_key),
        )
        return True

    monkeypatch.setattr(main, "analyze_and_save_thread_from_channel_scan", fake_analyze_and_save_thread_from_channel_scan)

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=24)

    assert result == {
        "threads_found": 2,
        "analyzed": 1,
        "skipped_existing": 0,
        "failed": 1,
    }

    successful_source_key = main.build_slack_source_key(channel_id, "222.000")
    rows = get_items_for_source(successful_source_key)
    assert rows


def test_scan_channel_clamps_invalid_lookback_hours(temp_database, monkeypatch):
    channel_id = "C123"
    captured_oldest_values = []

    def fake_fetch_slack_channel_messages(channel_id_arg, lookback_hours=24):
        captured_oldest_values.append(lookback_hours)
        return []

    monkeypatch.setattr(main, "fetch_slack_channel_messages", fake_fetch_slack_channel_messages)

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours="not-a-number")

    assert result == {
        "threads_found": 0,
        "analyzed": 0,
        "skipped_existing": 0,
        "failed": 0,
    }
    assert len(captured_oldest_values) == 1
    assert captured_oldest_values[0] == 24


def test_fetch_slack_channel_messages_calls_slack_history(monkeypatch):
    calls = []

    def fake_slack_api_get(endpoint, params=None):
        calls.append((endpoint, params))
        return {
            "messages": [
                {
                    "ts": "111.000",
                    "text": "Parent",
                    "reply_count": 2,
                }
            ]
        }

    monkeypatch.setattr(main, "slack_api_get", fake_slack_api_get)

    messages = main.fetch_slack_channel_messages("C123", lookback_hours=24)

    assert messages == [
        {
            "ts": "111.000",
            "text": "Parent",
            "reply_count": 2,
        }
    ]
    assert calls
    assert calls[0][0] == "conversations.history"
    assert calls[0][1]["channel"] == "C123"
    assert calls[0][1]["limit"] == 100
    assert "oldest" in calls[0][1]


def test_dashboard_scan_form_is_rendered():
    html = main.render_dashboard_html()

    assert 'action="/dashboard/scan-channel"' in html
    assert 'name="channel_id"' in html
    assert 'name="lookback_hours"' in html
    assert "Scan channel" in html

def test_scan_history_is_saved_after_scan(temp_database, monkeypatch):
    channel_id = "C123"

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [],
    )

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=12)

    assert result == {
        "threads_found": 0,
        "analyzed": 0,
        "skipped_existing": 0,
        "failed": 0,
    }

    history = main.get_recent_channel_scan_history()
    assert len(history) == 1
    assert history[0]["channel_id"] == channel_id
    assert history[0]["lookback_hours"] == 12
    assert history[0]["force_rescan"] == 0


def test_dashboard_renders_scan_history(temp_database, monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [],
    )

    main.scan_slack_channel_for_threads("C123", lookback_hours=24)
    html = main.render_dashboard_html()

    assert "Recent scans" in html
    assert "C123" in html
    assert "found 0" in html


def test_force_rescan_existing_thread_updates_without_duplicate_card(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
    )

    calls = []

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": thread_ts,
                "text": "Parent",
                "reply_count": 2,
            }
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        calls.append((channel_id_arg, thread_ts_arg))
        updated = saved_analysis(source_key)
        updated["summary"] = "Updated checkout summary after force rescan."
        main.save_thread_analysis(
            source_key,
            "https://example.slack.com/thread",
            updated,
        )
        return True

    monkeypatch.setattr(
        main,
        "analyze_and_save_thread_from_channel_scan",
        fake_analyze_and_save_thread_from_channel_scan,
    )

    result = main.scan_slack_channel_for_threads(
        channel_id,
        lookback_hours=24,
        force_rescan=True,
    )

    assert result == {
        "threads_found": 1,
        "analyzed": 1,
        "skipped_existing": 0,
        "failed": 0,
    }
    assert calls == [(channel_id, thread_ts)]

    with main.get_database_connection() as connection:
        rows = connection.execute(
            "SELECT source_key, summary FROM thread_analyses WHERE source_key = ?",
            (source_key,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["summary"] == "Updated checkout summary after force rescan."


def test_force_rescan_is_recorded_in_scan_history(temp_database, monkeypatch):
    channel_id = "C123"

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [],
    )

    main.scan_slack_channel_for_threads(channel_id, lookback_hours=24, force_rescan=True)

    history = main.get_recent_channel_scan_history()
    assert history[0]["force_rescan"] == 1


def test_dashboard_filter_tabs_are_rendered(temp_database):
    html = main.render_dashboard_html(item_filter="tracked")

    assert 'href="/dashboard?filter=all"' in html
    assert 'href="/dashboard?filter=risks"' in html
    assert 'href="/dashboard?filter=new"' in html
    assert 'href="/dashboard?filter=tracked"' in html
    assert 'href="/dashboard?filter=snoozed"' in html
    assert 'href="/dashboard?filter=ignored"' in html
    assert "active-filter" in html


def test_dashboard_scan_form_includes_force_rescan_checkbox(temp_database):
    html = main.render_dashboard_html()

    assert 'name="force_rescan"' in html
    assert "Force rescan existing threads" in html


def test_recent_thread_analyses_new_filter_excludes_tracked_ignored_and_snoozed(temp_database):
    source_key = "slack-thread:C123:filter-new"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    fix_proposed = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    test_proposed = item_by_type_and_title(source_key, "proposed_issue", "test shipping")
    question = item_by_type_and_title(source_key, "unresolved_question", "guest checkout")

    call_mark_tracked(fix_proposed["id"])
    call_snooze(test_proposed["id"], hours=24)
    main.ignore_related_detected_items(question["id"])

    entries = main.get_recent_thread_analyses(item_filter="new")

    assert all(entry["source_key"] != source_key for entry in entries)


def test_recent_thread_analyses_tracked_filter_includes_matched_items(temp_database):
    source_key = "slack-thread:C123:filter-tracked"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    fix_proposed = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    call_mark_tracked(fix_proposed["id"])

    entries = main.get_recent_thread_analyses(item_filter="tracked")
    entry = next(entry for entry in entries if entry["source_key"] == source_key)

    titles = [item.get("title", "") for item in entry["proposed_issues"]]
    assert "fix checkout page shipping method persistence" in titles


def test_recent_thread_analyses_snoozed_filter_includes_snoozed_items(temp_database):
    source_key = "slack-thread:C123:filter-snoozed"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    test_proposed = item_by_type_and_title(source_key, "proposed_issue", "test shipping")
    call_snooze(test_proposed["id"], hours=24)

    entries = main.get_recent_thread_analyses(item_filter="snoozed")
    entry = next(entry for entry in entries if entry["source_key"] == source_key)

    titles = [item.get("title", "") for item in entry["proposed_issues"]]
    assert "test shipping method persistence after Evan's fix" in titles


def test_recent_thread_analyses_ignored_filter_includes_ignored_items(temp_database):
    source_key = "slack-thread:C123:filter-ignored"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    question = item_by_type_and_title(source_key, "unresolved_question", "guest checkout")
    main.ignore_related_detected_items(question["id"])

    entries = main.get_recent_thread_analyses(item_filter="ignored")
    entry = next(entry for entry in entries if entry["source_key"] == source_key)

    questions = [item.get("question", "") for item in entry["unresolved_questions"]]
    assert "Should we also test guest checkout separately?" in questions



def test_save_thread_analysis_stores_scan_metadata(temp_database):
    source_key = "slack-thread:C123:freshness-store"
    metadata = {
        "last_seen_reply_ts": "111.999",
        "reply_count": 4,
        "last_scanned_at": "2026-05-16 12:00:00",
    }

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata=metadata,
    )

    stored = main.get_thread_scan_metadata(source_key)

    assert stored["last_seen_reply_ts"] == "111.999"
    assert stored["reply_count"] == 4
    assert stored["last_scanned_at"] == "2026-05-16 12:00:00"


def test_normal_scan_skips_unchanged_existing_thread_with_metadata(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)
    metadata = {"last_seen_reply_ts": "111.500", "reply_count": 2}

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata=metadata,
    )

    analyze_calls = []

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": thread_ts,
                "latest_reply": "111.500",
                "reply_count": 2,
                "text": "Existing unchanged parent",
            }
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        analyze_calls.append((channel_id_arg, thread_ts_arg, scan_metadata))
        return True

    monkeypatch.setattr(
        main,
        "analyze_and_save_thread_from_channel_scan",
        fake_analyze_and_save_thread_from_channel_scan,
    )

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=24)

    assert result == {
        "threads_found": 1,
        "analyzed": 0,
        "skipped_existing": 1,
        "failed": 0,
    }
    assert analyze_calls == []


def test_normal_scan_rescans_existing_thread_when_reply_metadata_changes(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata={"last_seen_reply_ts": "111.100", "reply_count": 1},
    )

    captured_metadata = []

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": thread_ts,
                "latest_reply": "111.900",
                "reply_count": 2,
                "text": "Changed parent",
            }
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        captured_metadata.append(scan_metadata)
        updated = saved_analysis(source_key)
        updated["summary"] = "Updated because Slack reply metadata changed."
        main.save_thread_analysis(
            source_key,
            "https://example.slack.com/thread",
            updated,
            scan_metadata=scan_metadata,
        )
        return True

    monkeypatch.setattr(
        main,
        "analyze_and_save_thread_from_channel_scan",
        fake_analyze_and_save_thread_from_channel_scan,
    )

    result = main.scan_slack_channel_for_threads(channel_id, lookback_hours=24)

    assert result == {
        "threads_found": 1,
        "analyzed": 1,
        "skipped_existing": 0,
        "failed": 0,
    }
    assert captured_metadata[0]["last_seen_reply_ts"] == "111.900"
    assert captured_metadata[0]["reply_count"] == 2

    stored = main.get_thread_scan_metadata(source_key)
    assert stored["last_seen_reply_ts"] == "111.900"
    assert stored["reply_count"] == 2


def test_force_rescan_rescans_unchanged_existing_thread(temp_database, monkeypatch):
    channel_id = "C123"
    thread_ts = "111.000"
    source_key = main.build_slack_source_key(channel_id, thread_ts)

    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata={"last_seen_reply_ts": "111.500", "reply_count": 2},
    )

    analyze_calls = []

    monkeypatch.setattr(
        main,
        "fetch_slack_channel_messages",
        lambda channel_id_arg, lookback_hours=24: [
            {
                "ts": thread_ts,
                "latest_reply": "111.500",
                "reply_count": 2,
                "text": "Existing unchanged parent",
            }
        ],
    )

    def fake_analyze_and_save_thread_from_channel_scan(channel_id_arg, thread_ts_arg, scan_metadata=None):
        analyze_calls.append((channel_id_arg, thread_ts_arg, scan_metadata))
        main.save_thread_analysis(
            source_key,
            "https://example.slack.com/thread",
            saved_analysis(source_key),
            scan_metadata=scan_metadata,
        )
        return True

    monkeypatch.setattr(
        main,
        "analyze_and_save_thread_from_channel_scan",
        fake_analyze_and_save_thread_from_channel_scan,
    )

    result = main.scan_slack_channel_for_threads(
        channel_id,
        lookback_hours=24,
        force_rescan=True,
    )

    assert result == {
        "threads_found": 1,
        "analyzed": 1,
        "skipped_existing": 0,
        "failed": 0,
    }
    assert len(analyze_calls) == 1


def test_linear_reference_parts_extracts_identifier_and_url():
    identifier, url = main.linear_reference_parts(
        "https://linear.app/example/issue/FLO-123/fix-checkout"
    )

    assert identifier == "FLO-123"
    assert url == "https://linear.app/example/issue/FLO-123/fix-checkout"

    identifier, url = main.linear_reference_parts("flo-456")

    assert identifier == "FLO-456"
    assert url == ""


def test_mark_tracked_with_linear_id_saves_reference_on_item_and_pair(temp_database):
    source_key = "slack-thread:C123:mark-tracked-linear-id"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    action_item = item_by_type_and_title(source_key, "action_item", "fix checkout")
    main.mark_related_detected_items_tracked(action_item["id"], linear_reference="FLO-123")

    rows = get_items_for_source(source_key)
    fix_rows = [row for row in rows if "fix checkout" in row["title"].lower()]

    assert {row["item_type"] for row in fix_rows} == {"action_item", "proposed_issue"}
    assert all(row["status"] == "matched" for row in fix_rows)
    assert all(row["linear_identifier"] == "FLO-123" for row in fix_rows)
    assert all(row["existing_issue_match"] == "FLO-123" for row in fix_rows)
    assert all(row["existing_issue_match_type"] == "manually_tracked" for row in fix_rows)


def test_mark_tracked_with_linear_url_saves_url_and_survives_reanalysis(temp_database):
    source_key = "slack-thread:C123:mark-tracked-linear-url"
    linear_url = "https://linear.app/example/issue/FLO-789/fix-checkout"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference=linear_url)

    reanalysis = saved_analysis(source_key)
    reanalysis["proposed_issues"][0]["title"] = "fix checkout shipping method persistence issue"
    reanalysis["action_items"][0]["task"] = "fix checkout shipping method persistence issue"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    rows = get_items_for_source(source_key)
    fix_rows = [
        row for row in rows
        if row["item_type"] in {"action_item", "proposed_issue"}
        and "fix checkout" in row["title"].lower()
    ]

    assert fix_rows
    assert all(row["status"] == "matched" for row in fix_rows)
    assert all(row["linear_identifier"] == "FLO-789" for row in fix_rows)
    assert all(row["linear_url"] == linear_url for row in fix_rows)


def test_dashboard_renders_mark_tracked_reference_form(temp_database):
    source_key = "slack-thread:C123:render-reference-form"
    analysis = saved_analysis(source_key)
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    html = main.render_dashboard_html()

    assert 'name="linear_reference"' in html
    assert "FLO-123 or Linear URL" in html
    assert "Mark tracked" in html


def test_dashboard_renders_clean_layout_containers(temp_database):
    html = main.render_dashboard_html()

    assert "dashboard-layout" in html
    assert "dashboard-sidebar" in html
    assert "dashboard-content" in html


def test_dashboard_renders_scan_freshness_metadata(temp_database):
    source_key = "slack-thread:C123:freshness-render"
    main.save_thread_analysis(
        source_key,
        "https://example.slack.com/thread",
        saved_analysis(source_key),
        scan_metadata={"last_seen_reply_ts": "111.555", "reply_count": 3},
    )

    entry = next(
        entry for entry in main.get_recent_thread_analyses()
        if entry["source_key"] == source_key
    )

    assert entry["last_seen_reply_ts"] == "111.555"
    assert entry["reply_count"] == 3
    assert entry["last_scanned_at"]


def test_evidence_asked_if_is_unanswered_question():
    assert main.evidence_is_unanswered_question(
        "Alex asked if they should also test backup code settings."
    ) is True


def test_action_item_from_asked_if_evidence_is_not_trackable():
    action_item = {
        "task": "Test backup code settings.",
        "assignee_name": "Alex",
        "due_date": "",
        "priority": "none",
        "evidence": "Alex asked if they should also test backup code settings.",
    }

    assert main.is_trackable_action_item(action_item) is False


def test_clean_thread_analysis_moves_question_evidence_action_to_unresolved_question():
    analysis = {
        "summary": "Alex raised a question about backup code settings.",
        "decisions": [],
        "action_items": [
            {
                "task": "Test backup code settings.",
                "assignee_name": "Alex",
                "due_date": "",
                "priority": "none",
                "evidence": "Alex asked if they should also test backup code settings.",
            }
        ],
        "blockers": [],
        "unresolved_questions": [],
        "proposed_issues": [],
    }

    cleaned = main.clean_thread_analysis(analysis, date.today())
    cleaned["proposed_issues"] = main.build_proposed_issues_from_action_items(
        cleaned["action_items"]
    )

    assert cleaned["action_items"] == []
    assert cleaned["proposed_issues"] == []
    assert cleaned["unresolved_questions"] == [
        {
            "question": "Should we also test backup code settings?",
            "evidence": "Alex asked if they should also test backup code settings.",
        }
    ]


def test_question_evidence_action_does_not_create_proposed_issue_when_saved(temp_database):
    source_key = "slack-thread:C123:asked-if-question"
    analysis = {
        "summary": "Alex asked if backup code settings should be tested.",
        "decisions": [],
        "action_items": [
            {
                "task": "Test backup code settings.",
                "assignee_name": "Alex",
                "due_date": "",
                "priority": "none",
                "evidence": "Alex asked if they should also test backup code settings.",
            }
        ],
        "blockers": [],
        "unresolved_questions": [],
        "proposed_issues": [],
    }

    cleaned = main.clean_thread_analysis(analysis, date.today())
    cleaned["proposed_issues"] = main.build_proposed_issues_from_action_items(
        cleaned["action_items"]
    )
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", cleaned)

    rows = get_items_for_source(source_key)

    assert [row["item_type"] for row in rows] == ["unresolved_question"]
    assert rows[0]["title"] == "Should we also test backup code settings?"


def test_dashboard_renders_collapsible_thread_cards(temp_database):
    source_key = "slack-thread:C123:collapsible-card"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    html = main.render_dashboard_html()

    assert '<details class="card thread-card"' in html
    assert '<summary class="thread-summary">' in html
    assert "Recent thread history" in html
    assert "Cards are collapsed by default" in html


def test_dashboard_renders_compact_item_rows_and_details(temp_database):
    source_key = "slack-thread:C123:compact-rows"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    html = main.render_dashboard_html()

    assert "compact-item-list" in html
    assert "item-row" in html
    assert "status-pill" in html
    assert "Details" in html
    assert "Evidence:" in html


def test_dashboard_search_filters_thread_history(temp_database):
    first_key = "slack-thread:C123:search-checkout"
    second_key = "slack-thread:C123:search-billing"

    checkout = saved_analysis(first_key)
    billing = saved_analysis(second_key)
    billing["summary"] = "Evan will fix billing preferences."
    billing["action_items"][0]["task"] = "fix billing preferences persistence"
    billing["proposed_issues"][0]["title"] = "fix billing preferences persistence"

    main.save_thread_analysis(first_key, "https://example.slack.com/checkout", checkout)
    main.save_thread_analysis(second_key, "https://example.slack.com/billing", billing)

    html = main.render_dashboard_html(search_query="billing")

    assert "billing preferences" in html
    assert "checkout persistence" not in html


def test_dashboard_search_form_preserves_active_filter():
    html = main.render_dashboard_html(item_filter="tracked", search_query="FLO-123")

    assert 'class="dashboard-search"' in html
    assert 'name="q"' in html
    assert 'value="FLO-123"' in html
    assert 'name="filter" value="tracked"' in html
    assert '/dashboard?filter=tracked&amp;q=FLO-123' in html


def test_dashboard_separates_review_queue_from_history(temp_database):
    source_key = "slack-thread:C123:review-history"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    html = main.render_dashboard_html()

    assert "Needs review" in html
    assert "Recent thread history" in html
    assert "risk-section" in html
    assert "history-section" in html


def test_github_columns_are_created(temp_database):
    with main.get_database_connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(detected_items)").fetchall()
        }

    assert "github_status" in columns
    assert "github_url" in columns
    assert "github_checked_at" in columns


def test_github_pull_request_search_query_uses_repo_and_identifier(monkeypatch):
    monkeypatch.setattr(main, "GITHUB_OWNER", "flow0178")
    monkeypatch.setattr(main, "GITHUB_REPO", "slack-linear-bot")

    query = main.github_pull_request_search_query("flo-123")

    assert query == "repo:flow0178/slack-linear-bot is:pr FLO-123 in:title,body"


def test_search_github_pull_request_for_linear_identifier_returns_first_pr(monkeypatch):
    monkeypatch.setattr(main, "GITHUB_OWNER", "flow0178")
    monkeypatch.setattr(main, "GITHUB_REPO", "slack-linear-bot")
    calls = []

    def fake_github_api_get(endpoint, params=None):
        calls.append((endpoint, params))
        return {
            "items": [
                {
                    "title": "FLO-123 Fix checkout persistence",
                    "html_url": "https://github.com/flow0178/slack-linear-bot/pull/7",
                    "state": "open",
                    "number": 7,
                }
            ]
        }

    monkeypatch.setattr(main, "github_api_get", fake_github_api_get)

    result = main.search_github_pull_request_for_linear_identifier("FLO-123")

    assert result["url"] == "https://github.com/flow0178/slack-linear-bot/pull/7"
    assert calls[0][0] == "search/issues"
    assert calls[0][1]["q"] == "repo:flow0178/slack-linear-bot is:pr FLO-123 in:title,body"
    assert calls[0][1]["per_page"] == 1




def test_check_github_refreshes_github_config_from_env_before_lookup(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-refresh-config"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-123")

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OWNER", "updated-owner")
    monkeypatch.setenv("GITHUB_REPO", "updated-repo")

    observed = {}

    def fake_search(linear_identifier):
        observed["owner"] = main.GITHUB_OWNER
        observed["repo"] = main.GITHUB_REPO
        observed["identifier"] = linear_identifier
        return None

    monkeypatch.setattr(main, "search_github_pull_request_for_linear_identifier", fake_search)

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "not_found"
    assert observed == {
        "owner": "updated-owner",
        "repo": "updated-repo",
        "identifier": "FLO-123",
    }


def test_require_github_config_reports_missing_refreshed_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setattr(main, "GITHUB_TOKEN", "old-token")
    monkeypatch.setattr(main, "GITHUB_OWNER", "old-owner")
    monkeypatch.setattr(main, "GITHUB_REPO", "old-repo")

    with pytest.raises(RuntimeError) as error:
        main.require_github_config()

    assert "GITHUB_TOKEN" in str(error.value)
    assert "GITHUB_OWNER" in str(error.value)
    assert "GITHUB_REPO" in str(error.value)

def test_check_github_for_detected_item_found_updates_database(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-found"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-123")

    monkeypatch.setattr(
        main,
        "search_github_pull_request_for_linear_identifier",
        lambda linear_identifier: {
            "title": "FLO-123 Fix checkout persistence",
            "url": "https://github.com/flow0178/slack-linear-bot/pull/7",
            "state": "open",
            "number": 7,
        },
    )

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "found"

    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "found"
    assert updated["github_url"] == "https://github.com/flow0178/slack-linear-bot/pull/7"
    assert updated["github_checked_at"]


def test_check_github_for_detected_item_not_found_updates_database(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-not-found"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-124")

    monkeypatch.setattr(
        main,
        "search_github_pull_request_for_linear_identifier",
        lambda linear_identifier: None,
    )

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "not_found"

    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "not_found"
    assert updated["github_url"] == ""
    assert updated["github_checked_at"]


def test_check_github_for_detected_item_without_linear_identifier_records_error(temp_database):
    source_key = "slack-thread:C123:github-missing-linear"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "error"

    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "error"
    assert updated["github_checked_at"]


def test_dashboard_renders_check_github_button_for_tracked_linear_item(temp_database):
    source_key = "slack-thread:C123:github-button"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-123")

    html = main.render_dashboard_html(item_filter="tracked")

    assert f'/dashboard/items/{proposed_issue["id"]}/check-github' in html
    assert "Check GitHub" in html


def test_github_result_survives_reanalysis(temp_database):
    source_key = "slack-thread:C123:github-preserve"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-123")
    main.update_detected_item_github_result(
        proposed_issue["id"],
        "found",
        "https://github.com/flow0178/slack-linear-bot/pull/7",
    )

    reanalysis = saved_analysis(source_key)
    reanalysis["proposed_issues"][0]["title"] = "fix checkout shipping method persistence issue"
    reanalysis["action_items"][0]["task"] = "fix checkout shipping method persistence issue"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", reanalysis)

    rows = get_items_for_source(source_key)
    fix_proposed = [
        row for row in rows
        if row["item_type"] == "proposed_issue" and "fix checkout" in row["title"].lower()
    ][0]

    assert fix_proposed["github_status"] == "found"
    assert fix_proposed["github_url"] == "https://github.com/flow0178/slack-linear-bot/pull/7"


def test_dashboard_hides_action_items_but_keeps_proposed_issues(temp_database):
    source_key = "slack-thread:C123:hide-actions"
    analysis = saved_analysis(source_key)
    analysis["action_items"].append(
        {
            "task": "backend only action item that should not render",
            "assignee_name": "Evan",
            "due_date": "",
            "priority": "none",
            "evidence": "Evan: I will handle backend only action item.",
        }
    )
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    html = main.render_dashboard_html()

    assert "Action items" not in html
    assert "backend only action item that should not render" not in html
    assert "Proposed Linear issues" in html
    assert "fix checkout page shipping method persistence" in html
    assert " actions</span>" not in html


def test_github_not_found_overwrites_stale_found_result(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-stale-not-found"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-125")
    main.update_detected_item_github_result(
        proposed_issue["id"],
        "found",
        "https://github.com/flow0178/slack-linear-bot/pull/7",
    )

    monkeypatch.setattr(
        main,
        "search_github_pull_request_for_linear_identifier",
        lambda linear_identifier: None,
    )

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "not_found"
    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "not_found"
    assert updated["github_url"] == ""
    assert updated["github_checked_at"]


def test_github_error_overwrites_stale_found_result(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-stale-error"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-126")
    main.update_detected_item_github_result(
        proposed_issue["id"],
        "found",
        "https://github.com/flow0178/slack-linear-bot/pull/7",
    )

    def fake_search(linear_identifier):
        raise RuntimeError("fake repo failure")

    monkeypatch.setattr(main, "search_github_pull_request_for_linear_identifier", fake_search)

    result = main.check_github_for_detected_item(proposed_issue["id"])

    assert result["status"] == "error"
    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "error"
    assert updated["github_url"] == ""
    assert updated["github_checked_at"]


def test_github_result_is_mirrored_to_paired_action_item(temp_database, monkeypatch):
    source_key = "slack-thread:C123:github-paired"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-127")

    monkeypatch.setattr(
        main,
        "search_github_pull_request_for_linear_identifier",
        lambda linear_identifier: {
            "title": "FLO-127 Fix checkout persistence",
            "url": "https://github.com/flow0178/slack-linear-bot/pull/8",
            "state": "open",
            "number": 8,
        },
    )

    main.check_github_for_detected_item(proposed_issue["id"])

    rows = get_items_for_source(source_key)
    paired_rows = [
        row for row in rows
        if row["item_type"] in {"action_item", "proposed_issue"}
        and "fix checkout" in row["title"].lower()
    ]
    assert {row["item_type"] for row in paired_rows} == {"action_item", "proposed_issue"}
    assert all(row["github_status"] == "found" for row in paired_rows)
    assert all(row["github_url"] == "https://github.com/flow0178/slack-linear-bot/pull/8" for row in paired_rows)


def test_dashboard_github_notice_renders_not_found_and_found():
    not_found_html = main.render_dashboard_html(
        github_status="not_found",
        github_identifier="FLO-998",
    )
    found_html = main.render_dashboard_html(
        github_status="found",
        github_identifier="FLO-999",
        github_url="https://github.com/flow0178/slack-linear-bot/pull/9",
    )

    assert "No GitHub pull request found for FLO-998." in not_found_html
    assert "GitHub pull request found for FLO-999." in found_html
    assert "Open pull request" in found_html


def test_dashboard_redirect_location_preserves_tracked_filter():
    class FakeURL:
        scheme = "http"
        netloc = "testserver"

    class FakeRequest:
        url = FakeURL()
        headers = {"referer": "http://testserver/dashboard?filter=tracked&q=FLO-999"}

    location = main.dashboard_redirect_location(
        FakeRequest(),
        {"github_status": "not_found", "github_identifier": "FLO-999"},
    )

    assert location.startswith("/dashboard?filter=tracked&q=FLO-999")
    assert "github_status=not_found" in location
    assert "github_identifier=FLO-999" in location


def test_github_check_candidate_items_returns_tracked_proposed_issues_with_linear_ids(temp_database):
    source_key = "slack-thread:C123:bulk-candidates"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    fix_proposed = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(fix_proposed["id"], linear_reference="FLO-200")

    candidates = main.github_check_candidate_items()

    assert len(candidates) == 1
    assert candidates[0]["id"] == fix_proposed["id"]
    assert candidates[0]["linear_identifier"] == "FLO-200"


def test_check_github_for_tracked_items_checks_all_tracked_candidates(temp_database, monkeypatch):
    source_key = "slack-thread:C123:bulk-check"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    fix_proposed = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    test_proposed = item_by_type_and_title(source_key, "proposed_issue", "test shipping")
    main.mark_related_detected_items_tracked(fix_proposed["id"], linear_reference="FLO-200")
    main.mark_related_detected_items_tracked(test_proposed["id"], linear_reference="FLO-201")

    def fake_search(linear_identifier):
        if linear_identifier == "FLO-200":
            return {
                "title": "FLO-200 Fix checkout persistence",
                "url": "https://github.com/flow0178/slack-linear-bot/pull/200",
                "state": "open",
                "number": 200,
            }
        return None

    monkeypatch.setattr(main, "search_github_pull_request_for_linear_identifier", fake_search)

    result = main.check_github_for_tracked_items()

    assert result == {
        "checked": 2,
        "found": 1,
        "not_found": 1,
        "error": 0,
    }

    updated_fix = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    updated_test = item_by_type_and_title(source_key, "proposed_issue", "test shipping")
    assert updated_fix["github_status"] == "found"
    assert updated_fix["github_url"] == "https://github.com/flow0178/slack-linear-bot/pull/200"
    assert updated_test["github_status"] == "not_found"
    assert updated_test["github_url"] == ""


def test_check_github_for_tracked_items_counts_errors(temp_database, monkeypatch):
    source_key = "slack-thread:C123:bulk-error"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    fix_proposed = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(fix_proposed["id"], linear_reference="FLO-202")

    def fake_search(linear_identifier):
        raise RuntimeError("fake github failure")

    monkeypatch.setattr(main, "search_github_pull_request_for_linear_identifier", fake_search)

    result = main.check_github_for_tracked_items()

    assert result == {
        "checked": 1,
        "found": 0,
        "not_found": 0,
        "error": 1,
    }

    updated = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    assert updated["github_status"] == "error"
    assert updated["github_url"] == ""


def test_check_github_for_tracked_items_respects_dashboard_search(temp_database, monkeypatch):
    first_source_key = "slack-thread:C123:bulk-search-checkout"
    second_source_key = "slack-thread:C123:bulk-search-profile"
    first_analysis = saved_analysis(first_source_key)
    second_analysis = saved_analysis(second_source_key)
    second_analysis["summary"] = "Evan will fix profile persistence."
    second_analysis["action_items"][0]["task"] = "fix profile page persistence"
    second_analysis["proposed_issues"][0]["title"] = "fix profile page persistence"
    second_analysis["proposed_issues"][0]["description"] = "fix profile page persistence"

    main.save_thread_analysis(first_source_key, "https://example.slack.com/thread-1", first_analysis)
    main.save_thread_analysis(second_source_key, "https://example.slack.com/thread-2", second_analysis)

    first_proposed = item_by_type_and_title(first_source_key, "proposed_issue", "fix checkout")
    second_proposed = item_by_type_and_title(second_source_key, "proposed_issue", "fix profile")
    main.mark_related_detected_items_tracked(first_proposed["id"], linear_reference="FLO-203")
    main.mark_related_detected_items_tracked(second_proposed["id"], linear_reference="FLO-204")

    checked = []

    def fake_check(item_id):
        checked.append(item_id)
        return {"status": "not_found", "url": "", "message": "No GitHub pull request found."}

    monkeypatch.setattr(main, "check_github_for_detected_item", fake_check)

    result = main.check_github_for_tracked_items(search_query="profile")

    assert result["checked"] == 1
    assert checked == [second_proposed["id"]]


def test_format_bulk_github_result_and_notice_render():
    message = main.format_bulk_github_result({
        "checked": 3,
        "found": 1,
        "not_found": 1,
        "error": 1,
    })
    html = main.render_dashboard_html(github_bulk_result=message)

    assert message == "Checked GitHub for 3 tracked item(s): 1 found, 1 not found, 1 error."
    assert message in html
    assert "dashboard-notice" in html


def test_dashboard_renders_bulk_github_check_form(temp_database):
    source_key = "slack-thread:C123:bulk-form"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))
    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-205")

    html = main.render_dashboard_html(item_filter="tracked", search_query="checkout")

    assert 'action="/dashboard/check-github-tracked"' in html
    assert "Check GitHub for tracked items" in html
    assert 'name="filter" value="tracked"' in html
    assert 'name="q" value="checkout"' in html


def test_risk_engine_flags_tracked_item_not_checked_on_github(temp_database):
    source_key = "slack-thread:C123:risk-github-unchecked"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-300")

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "Tracked Linear item has not been checked on GitHub" in reasons


def test_risk_engine_flags_tracked_item_with_no_matching_github_pr(temp_database):
    source_key = "slack-thread:C123:risk-github-not-found"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-301")
    main.update_detected_item_github_result(proposed_issue["id"], "not_found", "")

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "Tracked Linear item has no matching GitHub PR" in reasons


def test_risk_engine_flags_tracked_item_with_github_error(temp_database):
    source_key = "slack-thread:C123:risk-github-error"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-302")
    main.update_detected_item_github_result(proposed_issue["id"], "error", "")

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "GitHub lookup failed" in reasons


def test_risk_engine_flags_high_priority_tracked_item_without_github_pr(temp_database):
    source_key = "slack-thread:C123:risk-github-high-priority"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = ""
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-303")

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "High-priority tracked item has no GitHub PR" in reasons


def test_risk_engine_flags_due_soon_tracked_item_without_github_pr(temp_database):
    source_key = "slack-thread:C123:risk-github-due-soon"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "none"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-304")

    risks = risk_titles_and_reasons()
    reasons = risks["fix checkout page shipping method persistence"]

    assert "Due soon, but no GitHub PR found" in reasons


def test_risk_engine_does_not_flag_tracked_item_with_github_pr_found(temp_database):
    source_key = "slack-thread:C123:risk-github-found-clear"
    analysis = saved_analysis(source_key)
    analysis["proposed_issues"][0]["priority"] = "urgent"
    analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", analysis)

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-305")
    main.update_detected_item_github_result(
        proposed_issue["id"],
        "found",
        "https://github.com/flow0178/slack-linear-bot/pull/305",
    )

    assert "fix checkout page shipping method persistence" not in risk_titles_and_reasons()


def test_risk_engine_ignores_snoozed_tracked_github_risk(temp_database):
    source_key = "slack-thread:C123:risk-github-snoozed"
    main.save_thread_analysis(source_key, "https://example.slack.com/thread", saved_analysis(source_key))

    proposed_issue = item_by_type_and_title(source_key, "proposed_issue", "fix checkout")
    main.mark_related_detected_items_tracked(proposed_issue["id"], linear_reference="FLO-306")
    main.update_detected_item_github_result(proposed_issue["id"], "not_found", "")
    main.snooze_related_detected_items(proposed_issue["id"], hours=24)

    assert "fix checkout page shipping method persistence" not in risk_titles_and_reasons()


def test_recent_thread_analyses_filters_by_channel_id(temp_database):
    old_source_key = "slack-thread:COLD123:111.000"
    new_source_key = "slack-thread:GNEW456:222.000"

    old_analysis = saved_analysis(old_source_key)
    old_analysis["summary"] = "Old public channel work"
    new_analysis = saved_analysis(new_source_key)
    new_analysis["summary"] = "New private channel work"

    main.save_thread_analysis(old_source_key, "https://example.slack.com/old", old_analysis)
    main.save_thread_analysis(new_source_key, "https://example.slack.com/new", new_analysis)

    entries = main.get_recent_thread_analyses(channel_id="GNEW456")
    source_keys = {entry["source_key"] for entry in entries}

    assert new_source_key in source_keys
    assert old_source_key not in source_keys


def test_dashboard_html_channel_filter_hides_other_channel_cards(temp_database):
    old_source_key = "slack-thread:COLD123:333.000"
    new_source_key = "slack-thread:GNEW456:444.000"

    old_analysis = saved_analysis(old_source_key)
    old_analysis["summary"] = "Old channel checkout work"
    new_analysis = saved_analysis(new_source_key)
    new_analysis["summary"] = "Private channel timezone work"

    main.save_thread_analysis(old_source_key, "https://example.slack.com/old", old_analysis)
    main.save_thread_analysis(new_source_key, "https://example.slack.com/new", new_analysis)

    html = main.render_dashboard_html(channel_id="GNEW456")

    assert "Showing saved dashboard cards for channel" in html
    assert "GNEW456" in html
    assert "Private channel timezone work" in html
    assert "Old channel checkout work" not in html
    assert "channel_id" in html


def test_dashboard_risks_filters_by_channel_id(temp_database):
    old_source_key = "slack-thread:COLD123:555.000"
    new_source_key = "slack-thread:GNEW456:666.000"

    old_analysis = saved_analysis(old_source_key)
    old_analysis["summary"] = "Old channel risk summary"
    old_analysis["proposed_issues"][0]["title"] = "old channel risky issue"
    old_analysis["proposed_issues"][0]["priority"] = "urgent"
    old_analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    new_analysis = saved_analysis(new_source_key)
    new_analysis["summary"] = "New private channel risk summary"
    new_analysis["proposed_issues"][0]["title"] = "new private channel risky issue"
    new_analysis["proposed_issues"][0]["priority"] = "urgent"
    new_analysis["proposed_issues"][0]["due_date"] = date.today().isoformat()

    main.save_thread_analysis(old_source_key, "https://example.slack.com/old", old_analysis)
    main.save_thread_analysis(new_source_key, "https://example.slack.com/new", new_analysis)

    risks = main.get_dashboard_risks(channel_id="GNEW456")
    titles = {risk["title"] for risk in risks}

    assert "new private channel risky issue" in titles
    assert "old channel risky issue" not in titles


def test_github_bulk_candidates_filter_by_channel_id(temp_database):
    old_source_key = "slack-thread:COLD123:777.000"
    new_source_key = "slack-thread:GNEW456:888.000"

    main.save_thread_analysis(old_source_key, "https://example.slack.com/old", saved_analysis(old_source_key))
    main.save_thread_analysis(new_source_key, "https://example.slack.com/new", saved_analysis(new_source_key))

    old_issue = item_by_type_and_title(old_source_key, "proposed_issue", "fix checkout")
    new_issue = item_by_type_and_title(new_source_key, "proposed_issue", "fix checkout")

    main.mark_related_detected_items_tracked(old_issue["id"], linear_reference="FLO-111")
    main.mark_related_detected_items_tracked(new_issue["id"], linear_reference="FLO-222")

    candidates = main.github_check_candidate_items(channel_id="GNEW456")

    assert [candidate["linear_identifier"] for candidate in candidates] == ["FLO-222"]
