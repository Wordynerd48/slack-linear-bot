import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode
from difflib import SequenceMatcher

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")
LINEAR_API_URL = "https://api.linear.app/graphql"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_API_URL = "https://api.github.com"
DATABASE_PATH = os.getenv("DATABASE_PATH", "slack_linear.db")
PENDING_THREAD_PREVIEWS = {}
CREATED_THREAD_ISSUES_BY_SOURCE_KEY = {}
MAX_ANALYSIS_HISTORY = 20


@app.on_event("startup")
def startup_check():
    require_env_vars()
    init_database()
    logger.info("Slack Linear bot started")


def get_database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_database():
    with get_database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT UNIQUE NOT NULL,
                source_url TEXT,
                summary TEXT,
                last_seen_reply_ts TEXT,
                reply_count INTEGER,
                last_scanned_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_thread_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(thread_analyses)").fetchall()
        }

        if "last_seen_reply_ts" not in existing_thread_columns:
            connection.execute("ALTER TABLE thread_analyses ADD COLUMN last_seen_reply_ts TEXT")

        if "reply_count" not in existing_thread_columns:
            connection.execute("ALTER TABLE thread_analyses ADD COLUMN reply_count INTEGER")

        if "last_scanned_at" not in existing_thread_columns:
            connection.execute("ALTER TABLE thread_analyses ADD COLUMN last_scanned_at TEXT")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS detected_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                assignee_name TEXT,
                due_date TEXT,
                priority TEXT,
                evidence TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                linear_identifier TEXT,
                linear_url TEXT,
                existing_issue_match TEXT,
                existing_issue_match_type TEXT,
                snoozed_until TEXT,
                github_status TEXT,
                github_url TEXT,
                github_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES thread_analyses(id) ON DELETE CASCADE
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(detected_items)").fetchall()
        }

        if "snoozed_until" not in existing_columns:
            connection.execute("ALTER TABLE detected_items ADD COLUMN snoozed_until TEXT")

        if "github_status" not in existing_columns:
            connection.execute("ALTER TABLE detected_items ADD COLUMN github_status TEXT")

        if "github_url" not in existing_columns:
            connection.execute("ALTER TABLE detected_items ADD COLUMN github_url TEXT")

        if "github_checked_at" not in existing_columns:
            connection.execute("ALTER TABLE detected_items ADD COLUMN github_checked_at TEXT")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                lookback_hours INTEGER NOT NULL,
                force_rescan INTEGER NOT NULL DEFAULT 0,
                threads_found INTEGER NOT NULL DEFAULT 0,
                analyzed INTEGER NOT NULL DEFAULT 0,
                skipped_existing INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_analyses_updated_at ON thread_analyses(updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_thread_analyses_source_key ON thread_analyses(source_key)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_detected_items_analysis_id ON detected_items(analysis_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_scan_history_created_at ON channel_scan_history(created_at DESC)"
        )
        connection.commit()


def database_source_key(source_key):
    source_key = clean_text(source_key)
    return source_key or f"manual-analysis:{uuid.uuid4()}"


def item_title_for_type(item_type, item):
    if item_type == "action_item":
        return clean_text(item.get("task", ""))

    if item_type == "unresolved_question":
        return clean_text(item.get("question", ""))

    if item_type == "blocker":
        return clean_text(item.get("blocker", ""))

    return clean_text(item.get("title", ""))


def item_signature(item_type, title):
    return (clean_text(item_type), normalize_name(title))


def previous_item_state_map(connection, analysis_id):
    rows = connection.execute(
        """
        SELECT item_type, title, status, linear_identifier, linear_url,
               existing_issue_match, existing_issue_match_type, snoozed_until,
               github_status, github_url, github_checked_at
        FROM detected_items
        WHERE analysis_id = ?
        """,
        (analysis_id,),
    ).fetchall()

    state = {}

    for row in rows:
        state[item_signature(row["item_type"], row["title"])] = {
            "item_type": row["item_type"] or "",
            "title": row["title"] or "",
            "status": row["status"] or "new",
            "linear_identifier": row["linear_identifier"] or "",
            "linear_url": row["linear_url"] or "",
            "existing_issue_match": row["existing_issue_match"] or "",
            "existing_issue_match_type": row["existing_issue_match_type"] or "",
            "snoozed_until": row["snoozed_until"] or "",
            "github_status": row["github_status"] or "",
            "github_url": row["github_url"] or "",
            "github_checked_at": row["github_checked_at"] or "",
        }

    return state


def previous_item_state_for(previous_state, item_type, title):
    """Find prior status for a re-analyzed item.

    The model may slightly rewrite the same action item/question across runs.
    Preserve user decisions when the title is exact or strongly similar, while
    avoiding the old bug where test tasks matched implementation tasks.
    """
    exact_state = previous_state.get(item_signature(item_type, title))

    if exact_state:
        return exact_state

    best_state = None
    best_score = 0
    normalized_item_type = clean_text(item_type)

    for (old_item_type, _old_normalized_title), old_state in previous_state.items():
        if old_item_type != normalized_item_type:
            continue

        old_title = old_state.get("title", "")

        if item_type == "proposed_issue":
            old_issue = {"title": old_title}
            new_issue = {"title": title}

            if incompatible_task_types(new_issue, old_issue):
                continue

        score = max(
            similarity(title, old_title),
            token_overlap_score(title, old_title),
        )

        normalized_title = normalize_name(title)
        normalized_old_title = normalize_name(old_title)

        if normalized_title and normalized_old_title:
            if normalized_title in normalized_old_title or normalized_old_title in normalized_title:
                score = max(score, 0.9)

        if score > best_score:
            best_score = score
            best_state = old_state

    threshold = 0.72 if item_type == "unresolved_question" else 0.82
    if best_state and best_score >= threshold:
        logger.info(
            "Preserved prior status for re-analyzed item: type=%s title='%s' old_title='%s' score=%.2f",
            item_type,
            title,
            best_state.get("title", ""),
            best_score,
        )
        return best_state

    return None




def previous_related_item_state_for(previous_state, item_type, title):
    """Preserve status across related action/proposed issue rows.

    Action items and proposed issues represent the same underlying work, but
    they are stored as separate dashboard rows. If a user ignores one, the
    matching paired row should also stay ignored on re-analysis so Slack does
    not offer to create a Linear issue for dismissed work.
    """
    if item_type == "proposed_issue":
        candidate_types = ["action_item"]
    elif item_type == "action_item":
        candidate_types = ["proposed_issue"]
    else:
        candidate_types = []

    best_state = None
    best_score = 0
    new_issue = {"title": title}

    for (old_item_type, _old_normalized_title), old_state in previous_state.items():
        if old_item_type not in candidate_types:
            continue

        old_title = old_state.get("title", "")
        old_issue = {"title": old_title}

        if incompatible_task_types(new_issue, old_issue):
            continue

        score = max(
            similarity(title, old_title),
            token_overlap_score(title, old_title),
        )

        normalized_title = normalize_name(title)
        normalized_old_title = normalize_name(old_title)

        if normalized_title and normalized_old_title:
            if normalized_title in normalized_old_title or normalized_old_title in normalized_title:
                score = max(score, 0.9)

        if score > best_score:
            best_score = score
            best_state = old_state

    if best_state and best_score >= 0.82:
        logger.info(
            "Preserved prior related status for re-analyzed item: type=%s title='%s' old_type=%s old_title='%s' score=%.2f",
            item_type,
            title,
            best_state.get("item_type", ""),
            best_state.get("title", ""),
            best_score,
        )
        return best_state

    return None

def save_thread_analysis(source_key, source_url, analysis, scan_metadata=None):
    source_key = database_source_key(source_key)
    now = current_timestamp()
    summary = clean_text(analysis.get("summary", ""))
    scan_metadata = scan_metadata or {}
    last_seen_reply_ts = clean_text(scan_metadata.get("last_seen_reply_ts", ""))
    raw_reply_count = scan_metadata.get("reply_count", None)
    reply_count = None

    if raw_reply_count not in {None, ""}:
        try:
            reply_count = int(raw_reply_count)
        except (TypeError, ValueError):
            reply_count = None

    last_scanned_at = clean_text(scan_metadata.get("last_scanned_at", "")) or now

    with get_database_connection() as connection:
        existing = connection.execute(
            "SELECT id, created_at FROM thread_analyses WHERE source_key = ?",
            (source_key,),
        ).fetchone()

        previous_state = {}

        if existing:
            analysis_id = existing["id"]
            previous_state = previous_item_state_map(connection, analysis_id)
            connection.execute(
                """
                UPDATE thread_analyses
                SET source_url = ?, summary = ?,
                    last_seen_reply_ts = COALESCE(NULLIF(?, ''), last_seen_reply_ts),
                    reply_count = COALESCE(?, reply_count),
                    last_scanned_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (source_url, summary, last_seen_reply_ts, reply_count, last_scanned_at, now, analysis_id),
            )
            connection.execute(
                "DELETE FROM detected_items WHERE analysis_id = ?",
                (analysis_id,),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO thread_analyses (
                    source_key, source_url, summary, last_seen_reply_ts,
                    reply_count, last_scanned_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_key, source_url, summary, last_seen_reply_ts, reply_count, last_scanned_at, now, now),
            )
            analysis_id = cursor.lastrowid

        for item in analysis.get("action_items", []) or []:
            insert_detected_item(connection, analysis_id, "action_item", item, now, previous_state)

        for item in analysis.get("unresolved_questions", []) or []:
            insert_detected_item(connection, analysis_id, "unresolved_question", item, now, previous_state)

        for item in analysis.get("blockers", []) or []:
            insert_detected_item(connection, analysis_id, "blocker", item, now, previous_state)

        for item in analysis.get("proposed_issues", []) or []:
            item_id = insert_detected_item(connection, analysis_id, "proposed_issue", item, now, previous_state)
            item["detected_item_id"] = item_id

        connection.commit()

    logger.info("Saved thread analysis to SQLite: source_key=%s", source_key)
    return analysis_id


def insert_detected_item(connection, analysis_id, item_type, item, now, previous_state=None):
    previous_state = previous_state or {}
    title = item_title_for_type(item_type, item)

    if item_type == "action_item":
        description = title
    elif item_type in {"unresolved_question", "blocker"}:
        description = ""
    else:
        description = clean_text(item.get("description", ""))

    if not title:
        return None

    existing_match = clean_text(item.get("existing_issue_match", ""))
    existing_match_type = clean_text(item.get("existing_issue_match_type", ""))
    linear_url = clean_text(item.get("existing_issue_url", ""))
    linear_identifier = linear_identifier_from_match(existing_match)
    snoozed_until = clean_text(item.get("snoozed_until", ""))
    github_status = clean_text(item.get("github_status", ""))
    github_url = clean_text(item.get("github_url", ""))
    github_checked_at = clean_text(item.get("github_checked_at", ""))

    if item_type == "proposed_issue":
        if existing_match_type == "already_tracked":
            status = "matched"
        elif existing_match:
            status = "possible_duplicate"
        else:
            status = "new"
    else:
        status = "new"

    old_state = previous_item_state_for(previous_state, item_type, title)

    if not old_state:
        old_state = previous_related_item_state_for(previous_state, item_type, title)

    if old_state:
        old_status = old_state.get("status", "")

        if old_status in {"ignored", "created", "matched"}:
            status = old_status

        linear_identifier = linear_identifier or old_state.get("linear_identifier", "")
        linear_url = linear_url or old_state.get("linear_url", "")
        existing_match = existing_match or old_state.get("existing_issue_match", "")
        existing_match_type = existing_match_type or old_state.get("existing_issue_match_type", "")
        snoozed_until = snoozed_until or old_state.get("snoozed_until", "")
        github_status = github_status or old_state.get("github_status", "")
        github_url = github_url or old_state.get("github_url", "")
        github_checked_at = github_checked_at or old_state.get("github_checked_at", "")

    if item_type == "proposed_issue":
        item["status"] = status

        if linear_identifier:
            item["linear_identifier"] = linear_identifier

        if linear_url:
            item["existing_issue_url"] = linear_url

        if existing_match:
            item["existing_issue_match"] = existing_match

        if existing_match_type:
            item["existing_issue_match_type"] = existing_match_type

        if snoozed_until:
            item["snoozed_until"] = snoozed_until

        if github_status:
            item["github_status"] = github_status

        if github_url:
            item["github_url"] = github_url

        if github_checked_at:
            item["github_checked_at"] = github_checked_at

    cursor = connection.execute(
        """
        INSERT INTO detected_items (
            analysis_id, item_type, title, description, assignee_name, due_date,
            priority, evidence, status, linear_identifier, linear_url,
            existing_issue_match, existing_issue_match_type, snoozed_until,
            github_status, github_url, github_checked_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            item_type,
            title,
            description,
            clean_text(item.get("assignee_name", "") or item.get("owner", "")),
            clean_text(item.get("due_date", "")),
            clean_text(item.get("priority", "")),
            clean_text(item.get("evidence", "")),
            status,
            linear_identifier,
            linear_url,
            existing_match,
            existing_match_type,
            snoozed_until,
            github_status,
            github_url,
            github_checked_at,
            now,
            now,
        ),
    )
    return cursor.lastrowid


def linear_identifier_from_match(existing_match):
    existing_match = clean_text(existing_match)
    match = re.match(r"([A-Z]+-\d+):", existing_match)
    return match.group(1) if match else ""


def linear_reference_parts(value):
    value = clean_text(value)

    if not value:
        return "", ""

    identifier_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", value, re.IGNORECASE)
    identifier = identifier_match.group(1).upper() if identifier_match else ""
    url = value if re.match(r"https?://", value, re.IGNORECASE) else ""

    return identifier, url


def linear_display_from_reference(identifier, url):
    identifier = clean_text(identifier)
    url = clean_text(url)

    if identifier and url:
        return f"{identifier}\n  {url}"

    return identifier or url


def mark_detected_item_created(detected_item_id, issue):
    if not detected_item_id:
        return

    now = current_timestamp()

    with get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET status = 'created', linear_identifier = ?, linear_url = ?, snoozed_until = '', updated_at = ?
            WHERE id = ?
            """,
            (
                issue.get("identifier", ""),
                issue.get("url", ""),
                now,
                detected_item_id,
            ),
        )
        connection.commit()


def update_detected_item_status(item_id, status):
    allowed_statuses = {"new", "ignored", "created", "matched", "possible_duplicate"}

    if status not in allowed_statuses:
        raise ValueError(f"Unsupported detected item status: {status}")

    now = current_timestamp()

    with get_database_connection() as connection:
        connection.execute(
            """
            UPDATE detected_items
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, now, item_id),
        )
        connection.commit()




def ignore_related_detected_items(item_id):
    """Ignore the selected dashboard item and its paired action/proposed row.

    This prevents a dismissed action item from still appearing as a createable
    proposed Linear issue in Slack after the same thread is analyzed again.
    """
    now = current_timestamp()

    with get_database_connection() as connection:
        selected = connection.execute(
            "SELECT * FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if not selected:
            return

        connection.execute(
            """
            UPDATE detected_items
            SET status = 'ignored', updated_at = ?
            WHERE id = ?
            """,
            (now, item_id),
        )

        selected_type = selected["item_type"]
        selected_title = selected["title"] or ""

        if selected_type == "action_item":
            related_types = ["proposed_issue"]
        elif selected_type == "proposed_issue":
            related_types = ["action_item"]
        else:
            related_types = []

        if related_types:
            candidates = connection.execute(
                """
                SELECT * FROM detected_items
                WHERE analysis_id = ?
                  AND item_type IN ({})
                  AND status != 'ignored'
                """.format(",".join("?" for _ in related_types)),
                (selected["analysis_id"], *related_types),
            ).fetchall()

            selected_issue = {"title": selected_title}

            for candidate in candidates:
                candidate_title = candidate["title"] or ""
                candidate_issue = {"title": candidate_title}

                if incompatible_task_types(selected_issue, candidate_issue):
                    continue

                score = max(
                    similarity(selected_title, candidate_title),
                    token_overlap_score(selected_title, candidate_title),
                )

                normalized_selected = normalize_name(selected_title)
                normalized_candidate = normalize_name(candidate_title)

                if normalized_selected and normalized_candidate:
                    if normalized_selected in normalized_candidate or normalized_candidate in normalized_selected:
                        score = max(score, 0.9)

                if score >= 0.82:
                    connection.execute(
                        """
                        UPDATE detected_items
                        SET status = 'ignored', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, candidate["id"]),
                    )
                    logger.info(
                        "Ignored related dashboard item: selected='%s' related='%s' score=%.2f",
                        selected_title,
                        candidate_title,
                        score,
                    )

        connection.commit()



def related_detected_item_candidates(connection, selected, related_types):
    if not related_types:
        return []

    return connection.execute(
        """
        SELECT * FROM detected_items
        WHERE analysis_id = ?
          AND item_type IN ({})
        """.format(",".join("?" for _ in related_types)),
        (selected["analysis_id"], *related_types),
    ).fetchall()


def matching_related_detected_items(connection, selected, related_types):
    selected_title = selected["title"] or ""
    selected_issue = {"title": selected_title}
    matches = []

    for candidate in related_detected_item_candidates(connection, selected, related_types):
        if candidate["id"] == selected["id"]:
            continue

        candidate_title = candidate["title"] or ""
        candidate_issue = {"title": candidate_title}

        if incompatible_task_types(selected_issue, candidate_issue):
            continue

        score = max(
            similarity(selected_title, candidate_title),
            token_overlap_score(selected_title, candidate_title),
        )

        normalized_selected = normalize_name(selected_title)
        normalized_candidate = normalize_name(candidate_title)

        if normalized_selected and normalized_candidate:
            if normalized_selected in normalized_candidate or normalized_candidate in normalized_selected:
                score = max(score, 0.9)

        if score >= 0.82:
            matches.append(candidate)

    return matches


def mark_related_detected_items_tracked(item_id, linear_reference=""):
    now = current_timestamp()
    linear_identifier, linear_url = linear_reference_parts(linear_reference)
    existing_issue_match = linear_display_from_reference(linear_identifier, linear_url)

    with get_database_connection() as connection:
        selected = connection.execute(
            "SELECT * FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if not selected:
            return

        connection.execute(
            """
            UPDATE detected_items
            SET status = 'matched',
                linear_identifier = COALESCE(NULLIF(?, ''), linear_identifier),
                linear_url = COALESCE(NULLIF(?, ''), linear_url),
                existing_issue_match = COALESCE(NULLIF(?, ''), existing_issue_match),
                existing_issue_match_type = COALESCE(NULLIF(?, ''), existing_issue_match_type),
                snoozed_until = '',
                updated_at = ?
            WHERE id = ?
            """,
            (
                linear_identifier,
                linear_url,
                existing_issue_match,
                "manually_tracked" if existing_issue_match else "",
                now,
                item_id,
            ),
        )

        selected_type = selected["item_type"]

        if selected_type == "action_item":
            related_types = ["proposed_issue"]
        elif selected_type == "proposed_issue":
            related_types = ["action_item"]
        else:
            related_types = []

        for candidate in matching_related_detected_items(connection, selected, related_types):
            connection.execute(
                """
                UPDATE detected_items
                SET status = 'matched',
                    linear_identifier = COALESCE(NULLIF(?, ''), linear_identifier),
                    linear_url = COALESCE(NULLIF(?, ''), linear_url),
                    existing_issue_match = COALESCE(NULLIF(?, ''), existing_issue_match),
                    existing_issue_match_type = COALESCE(NULLIF(?, ''), existing_issue_match_type),
                    snoozed_until = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    linear_identifier,
                    linear_url,
                    existing_issue_match,
                    "manually_tracked" if existing_issue_match else "",
                    now,
                    candidate["id"],
                ),
            )
            logger.info(
                "Marked related dashboard item tracked: selected='%s' related='%s' reference='%s'",
                selected["title"] or "",
                candidate["title"] or "",
                existing_issue_match,
            )

        connection.commit()


def snooze_related_detected_items(item_id, hours):
    hours = max(1, int(hours))
    now = current_timestamp()
    snoozed_until = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    with get_database_connection() as connection:
        selected = connection.execute(
            "SELECT * FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if not selected:
            return

        connection.execute(
            """
            UPDATE detected_items
            SET snoozed_until = ?, updated_at = ?
            WHERE id = ?
            """,
            (snoozed_until, now, item_id),
        )

        selected_type = selected["item_type"]

        if selected_type == "action_item":
            related_types = ["proposed_issue"]
        elif selected_type == "proposed_issue":
            related_types = ["action_item"]
        else:
            related_types = []

        for candidate in matching_related_detected_items(connection, selected, related_types):
            connection.execute(
                """
                UPDATE detected_items
                SET snoozed_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (snoozed_until, now, candidate["id"]),
            )
            logger.info(
                "Snoozed related dashboard item: selected='%s' related='%s' until=%s",
                selected["title"] or "",
                candidate["title"] or "",
                snoozed_until,
            )

        connection.commit()


def is_future_timestamp(value):
    timestamp = parse_database_timestamp(value)
    return bool(timestamp and timestamp > datetime.now())

def get_recent_thread_analyses(limit=20, item_filter="all"):
    init_database()
    item_filter = normalize_dashboard_filter(item_filter)

    with get_database_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, source_key, source_url, summary, last_seen_reply_ts,
                   reply_count, last_scanned_at, created_at, updated_at
            FROM thread_analyses
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        entries = []

        for row in rows:
            item_rows = connection.execute(
                """
                SELECT * FROM detected_items
                WHERE analysis_id = ?
                ORDER BY id ASC
                """,
                (row["id"],),
            ).fetchall()

            entry = {
                "id": row["id"],
                "source_key": row["source_key"],
                "source_url": row["source_url"],
                "summary": row["summary"],
                "last_seen_reply_ts": row["last_seen_reply_ts"],
                "reply_count": row["reply_count"],
                "last_scanned_at": row["last_scanned_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "action_items": [],
                "unresolved_questions": [],
                "blockers": [],
                "proposed_issues": [],
            }

            for item in item_rows:
                if not detected_item_matches_dashboard_filter(item, item_filter):
                    continue

                item_dict = detected_item_to_dashboard_dict(item)
                item_type = item["item_type"]

                if item_type == "action_item":
                    entry["action_items"].append(item_dict)
                elif item_type == "unresolved_question":
                    entry["unresolved_questions"].append(item_dict)
                elif item_type == "blocker":
                    entry["blockers"].append(item_dict)
                elif item_type == "proposed_issue":
                    entry["proposed_issues"].append(item_dict)

            has_visible_items = any(
                entry[key]
                for key in [
                    "action_items",
                    "unresolved_questions",
                    "blockers",
                    "proposed_issues",
                ]
            )

            if item_filter != "all" and not has_visible_items:
                continue

            proposed_issues = entry["proposed_issues"]
            entry["createable_count"] = sum(
                1 for item in proposed_issues if item.get("status") == "new"
            )
            entry["tracked_count"] = len(proposed_issues) - entry["createable_count"]
            entries.append(entry)

        return entries


def detected_item_to_dashboard_dict(item):
    item_type = item["item_type"]

    if item_type == "action_item":
        result = {"id": item["id"], "item_type": item_type, "task": item["title"]}
    elif item_type == "unresolved_question":
        result = {"id": item["id"], "item_type": item_type, "question": item["title"]}
    elif item_type == "blocker":
        result = {"id": item["id"], "item_type": item_type, "blocker": item["title"]}
    else:
        result = {
            "id": item["id"],
            "item_type": item_type,
            "title": item["title"],
            "description": item["description"],
        }

    optional_fields = [
        "assignee_name",
        "due_date",
        "priority",
        "evidence",
        "status",
        "linear_identifier",
        "linear_url",
        "existing_issue_match",
        "existing_issue_match_type",
        "snoozed_until",
        "github_status",
        "github_url",
        "github_checked_at",
    ]

    for field in optional_fields:
        value = item[field]
        if value:
            result[field] = value

    return result


def parse_database_timestamp(value):
    value = clean_text(value)

    if not value:
        return None

    for timestamp_format in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(value, timestamp_format)
        except ValueError:
            continue

    return None


def hours_since_timestamp(value):
    timestamp = parse_database_timestamp(value)

    if not timestamp:
        return 0

    return max(0, (datetime.now() - timestamp).total_seconds() / 3600)


def due_date_within_days(value, days):
    value = clean_text(value)

    if not value:
        return False

    try:
        parsed_due_date = date.fromisoformat(value)
    except ValueError:
        return False

    today = date.today()
    return today <= parsed_due_date <= today + timedelta(days=days)


def risk_item_primary_field(item_type):
    if item_type == "unresolved_question":
        return "question"

    if item_type == "blocker":
        return "blocker"

    if item_type == "action_item":
        return "task"

    return "title"


def get_dashboard_risks(limit=50):
    init_database()

    with get_database_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                detected_items.*,
                thread_analyses.source_url AS source_url,
                thread_analyses.summary AS analysis_summary
            FROM detected_items
            JOIN thread_analyses ON thread_analyses.id = detected_items.analysis_id
            WHERE detected_items.status = 'new'
              AND detected_items.item_type IN ('proposed_issue', 'unresolved_question', 'blocker')
              AND (
                  detected_items.snoozed_until IS NULL
                  OR detected_items.snoozed_until = ''
                  OR detected_items.snoozed_until <= ?
              )
            ORDER BY detected_items.created_at ASC
            LIMIT ?
            """,
            (current_timestamp(), limit),
        ).fetchall()

    risks = []

    for row in rows:
        item_type = row["item_type"]
        reasons = []
        age_hours = hours_since_timestamp(row["created_at"])
        priority = normalize_name(row["priority"] or "")

        if item_type == "proposed_issue":
            if age_hours >= 24:
                reasons.append("Untracked action item older than 24 hours")

            if priority in {"high", "urgent"}:
                reasons.append("High-priority item has not been created or matched")

            if due_date_within_days(row["due_date"], 2):
                reasons.append("Due within 2 days and not yet tracked")

        elif item_type == "unresolved_question":
            if age_hours >= 48:
                reasons.append("Unresolved question older than 48 hours")

        elif item_type == "blocker":
            reasons.append("Blocker still needs review")

        if not reasons:
            continue

        item = detected_item_to_dashboard_dict(row)
        primary_field = risk_item_primary_field(item_type)

        risks.append(
            {
                "id": row["id"],
                "item_type": item_type,
                "title": item.get(primary_field, row["title"]),
                "primary_field": primary_field,
                "reasons": reasons,
                "source_url": row["source_url"] or "",
                "analysis_summary": row["analysis_summary"] or "",
                "created_at": row["created_at"] or "",
                "item": item,
            }
        )

    return risks


def require_env_vars():
    required_vars = {
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "LINEAR_API_KEY": LINEAR_API_KEY,
        "LINEAR_TEAM_ID": LINEAR_TEAM_ID,
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    }

    missing = [name for name, value in required_vars.items() if not value]

    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def verify_slack_request(request_body, timestamp, slack_signature):
    if not SLACK_SIGNING_SECRET:
        raise RuntimeError("Missing SLACK_SIGNING_SECRET")

    if not timestamp or not slack_signature:
        return False

    current_time = int(time.time())

    if abs(current_time - int(timestamp)) > 60 * 5:
        return False

    basestring = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    calculated_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated_signature, slack_signature)


def slack_api_get(endpoint, params=None):
    response = requests.get(
        f"https://slack.com/api/{endpoint}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params=params or {},
        timeout=10,
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error from {endpoint}: {data}")

    return data



def require_github_config():
    missing = []
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not GITHUB_OWNER:
        missing.append("GITHUB_OWNER")
    if not GITHUB_REPO:
        missing.append("GITHUB_REPO")

    if missing:
        raise RuntimeError(f"Missing GitHub configuration: {', '.join(missing)}")


def github_api_get(endpoint, params=None):
    require_github_config()
    response = requests.get(
        f"{GITHUB_API_URL}/{endpoint.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params or {},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def github_pull_request_search_query(linear_identifier):
    linear_identifier = clean_text(linear_identifier).upper()
    if not linear_identifier:
        return ""

    return f'repo:{GITHUB_OWNER}/{GITHUB_REPO} is:pr {linear_identifier} in:title,body'


def search_github_pull_request_for_linear_identifier(linear_identifier):
    query = github_pull_request_search_query(linear_identifier)
    if not query:
        return None

    data = github_api_get(
        "search/issues",
        {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": 1,
        },
    )
    items = data.get("items", []) or []

    if not items:
        return None

    item = items[0]
    return {
        "title": item.get("title", ""),
        "url": item.get("html_url", ""),
        "state": item.get("state", ""),
        "number": item.get("number", ""),
    }


def update_detected_item_github_result(item_id, github_status, github_url=""):
    """Save the latest GitHub lookup result and mirror it to paired rows.

    A manual GitHub check should always overwrite stale results. If a previous
    lookup found a PR, a later not_found or error result must clear the old URL
    so the dashboard does not keep showing a stale success.
    """
    now = current_timestamp()
    github_status = clean_text(github_status)
    github_url = clean_text(github_url) if github_status == "found" else ""

    with get_database_connection() as connection:
        selected = connection.execute(
            "SELECT * FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if not selected:
            return

        item_ids = [item_id]
        selected_type = selected["item_type"]

        if selected_type == "action_item":
            related_types = ["proposed_issue"]
        elif selected_type == "proposed_issue":
            related_types = ["action_item"]
        else:
            related_types = []

        for candidate in matching_related_detected_items(connection, selected, related_types):
            item_ids.append(candidate["id"])

        connection.executemany(
            """
            UPDATE detected_items
            SET github_status = ?, github_url = ?, github_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            [(github_status, github_url, now, now, related_item_id) for related_item_id in item_ids],
        )
        connection.commit()


def check_github_for_detected_item(item_id):
    init_database()
    with get_database_connection() as connection:
        item = connection.execute(
            "SELECT * FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()

    if not item:
        return {"status": "error", "url": "", "message": "Dashboard item not found."}

    linear_identifier = clean_text(item["linear_identifier"] or "")

    if not linear_identifier:
        update_detected_item_github_result(item_id, "error", "")
        return {"status": "error", "url": "", "message": "No Linear identifier saved on this item."}

    try:
        match = search_github_pull_request_for_linear_identifier(linear_identifier)
    except Exception as error:
        logger.exception("GitHub lookup failed for Linear identifier %s", linear_identifier)
        update_detected_item_github_result(item_id, "error", "")
        return {"status": "error", "url": "", "message": str(error)}

    if match:
        github_url = match.get("url", "")
        update_detected_item_github_result(item_id, "found", github_url)
        return {"status": "found", "url": github_url, "message": "GitHub pull request found."}

    update_detected_item_github_result(item_id, "not_found", "")
    return {"status": "not_found", "url": "", "message": "No GitHub pull request found."}




def fetch_slack_channel_messages(channel_id, lookback_hours=24, limit=100):
    channel_id = clean_text(channel_id)

    try:
        lookback_hours = int(lookback_hours or 24)
    except (TypeError, ValueError):
        lookback_hours = 24

    lookback_hours = max(1, lookback_hours)
    oldest = str(time.time() - lookback_hours * 60 * 60)

    if not channel_id:
        raise ValueError("channel_id is required")

    data = slack_api_get(
        "conversations.history",
        {
            "channel": channel_id,
            "oldest": oldest,
            "limit": limit,
        },
    )

    messages = data.get("messages", []) or []
    logger.info(
        "Fetched %s Slack channel messages from channel=%s lookback_hours=%s",
        len(messages),
        channel_id,
        lookback_hours,
    )
    return messages


def source_key_exists(source_key):
    source_key = clean_text(source_key)

    if not source_key:
        return False

    init_database()

    with get_database_connection() as connection:
        row = connection.execute(
            "SELECT id FROM thread_analyses WHERE source_key = ?",
            (source_key,),
        ).fetchone()

    return row is not None


def get_thread_scan_metadata(source_key):
    source_key = clean_text(source_key)

    if not source_key:
        return None

    init_database()

    with get_database_connection() as connection:
        row = connection.execute(
            """
            SELECT last_seen_reply_ts, reply_count, last_scanned_at
            FROM thread_analyses
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()

    return dict(row) if row else None


def slack_thread_parent_metadata(parent):
    latest_reply = clean_text(
        parent.get("latest_reply", "")
        or parent.get("thread_ts", "")
        or parent.get("ts", "")
    )

    try:
        reply_count = int(parent.get("reply_count") or 0)
    except (TypeError, ValueError):
        reply_count = 0

    return {
        "last_seen_reply_ts": latest_reply,
        "reply_count": reply_count,
        "last_scanned_at": current_timestamp(),
    }


def thread_scan_metadata_changed(previous_metadata, current_metadata):
    if not previous_metadata:
        return True

    previous_reply_ts = clean_text(previous_metadata.get("last_seen_reply_ts", ""))
    current_reply_ts = clean_text(current_metadata.get("last_seen_reply_ts", ""))
    previous_reply_count = previous_metadata.get("reply_count")
    current_reply_count = current_metadata.get("reply_count")

    if previous_reply_ts and current_reply_ts and previous_reply_ts != current_reply_ts:
        return True

    if previous_reply_count is not None and current_reply_count is not None:
        try:
            return int(previous_reply_count) != int(current_reply_count)
        except (TypeError, ValueError):
            return True

    return True


def save_channel_scan_history(channel_id, lookback_hours, force_rescan, result):
    init_database()
    now = current_timestamp()

    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_scan_history (
                channel_id, lookback_hours, force_rescan, threads_found,
                analyzed, skipped_existing, failed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_text(channel_id),
                int(lookback_hours or 24),
                1 if force_rescan else 0,
                int(result.get("threads_found", 0)),
                int(result.get("analyzed", 0)),
                int(result.get("skipped_existing", 0)),
                int(result.get("failed", 0)),
                now,
            ),
        )
        connection.commit()


def get_recent_channel_scan_history(limit=5):
    init_database()

    with get_database_connection() as connection:
        rows = connection.execute(
            """
            SELECT channel_id, lookback_hours, force_rescan, threads_found,
                   analyzed, skipped_existing, failed, created_at
            FROM channel_scan_history
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


def normalize_dashboard_filter(value):
    value = normalize_name(value)
    allowed_filters = {"all", "risks", "new", "tracked", "snoozed", "ignored"}
    return value if value in allowed_filters else "all"


def detected_item_matches_dashboard_filter(item, item_filter):
    item_filter = normalize_dashboard_filter(item_filter)
    status = clean_text(item["status"] or "new")
    snoozed = is_future_timestamp(item["snoozed_until"] or "")

    if item_filter in {"all", "risks"}:
        return status != "ignored"

    if item_filter == "new":
        return status == "new" and not snoozed

    if item_filter == "tracked":
        return status in {"created", "matched", "possible_duplicate"}

    if item_filter == "snoozed":
        return snoozed and status not in {"ignored", "created", "matched"}

    if item_filter == "ignored":
        return status == "ignored"

    return status != "ignored"


def channel_thread_parent_messages(messages):
    parents = []
    seen_thread_ts = set()

    for message in messages or []:
        if message.get("subtype"):
            continue

        if int(message.get("reply_count") or 0) <= 0:
            continue

        thread_ts = message.get("thread_ts") or message.get("ts")

        if not thread_ts or thread_ts in seen_thread_ts:
            continue

        seen_thread_ts.add(thread_ts)
        parents.append(message)

    return parents


def analyze_and_save_thread_from_channel_scan(channel_id, thread_ts, scan_metadata=None):
    messages = fetch_slack_thread(channel_id, thread_ts)

    if not messages:
        return False

    analysis = analyze_thread_with_ai(messages)
    source_url = get_slack_thread_permalink(channel_id, thread_ts)
    source_key = build_slack_source_key(channel_id, thread_ts)
    analysis = annotate_existing_linear_matches(
        analysis,
        source_url=source_url,
        source_key=source_key,
    )
    save_thread_analysis(source_key, source_url, analysis, scan_metadata=scan_metadata)
    return True


def scan_slack_channel_for_threads(channel_id, lookback_hours=24, force_rescan=False):
    channel_id = clean_text(channel_id)

    try:
        lookback_hours = int(lookback_hours or 24)
    except (TypeError, ValueError):
        lookback_hours = 24

    lookback_hours = max(1, lookback_hours)
    force_rescan = bool(force_rescan)

    if not channel_id:
        raise ValueError("channel_id is required")

    messages = fetch_slack_channel_messages(channel_id, lookback_hours=lookback_hours)
    thread_parents = channel_thread_parent_messages(messages)
    thread_parents = sorted(
        thread_parents,
        key=lambda message: float(message.get("thread_ts") or message.get("ts") or 0),
    )
    analyzed_count = 0
    skipped_existing_count = 0
    failed_count = 0

    for parent in thread_parents:
        thread_ts = parent.get("thread_ts") or parent.get("ts")
        source_key = build_slack_source_key(channel_id, thread_ts)
        current_metadata = slack_thread_parent_metadata(parent)
        previous_metadata = get_thread_scan_metadata(source_key)

        if previous_metadata and not force_rescan:
            if not thread_scan_metadata_changed(previous_metadata, current_metadata):
                skipped_existing_count += 1
                continue

        try:
            if analyze_and_save_thread_from_channel_scan(
                channel_id,
                thread_ts,
                scan_metadata=current_metadata,
            ):
                analyzed_count += 1
        except Exception:
            failed_count += 1
            logger.exception(
                "Could not analyze scanned Slack thread: channel_id=%s thread_ts=%s",
                channel_id,
                thread_ts,
            )

    result = {
        "threads_found": len(thread_parents),
        "analyzed": analyzed_count,
        "skipped_existing": skipped_existing_count,
        "failed": failed_count,
    }
    save_channel_scan_history(channel_id, lookback_hours, force_rescan, result)
    logger.info("Channel scan result: %s", result)
    return result


def format_channel_scan_result(result):
    return (
        f"Scanned {result.get('threads_found', 0)} thread(s): "
        f"analyzed {result.get('analyzed', 0)} new or rescanned, "
        f"skipped {result.get('skipped_existing', 0)} existing, "
        f"failed {result.get('failed', 0)}."
    )

def fetch_slack_thread(channel_id, thread_ts):
    if not channel_id:
        raise ValueError("channel_id is required")

    if not thread_ts:
        raise ValueError("thread_ts is required")

    try:
        data = slack_api_get(
            "conversations.replies",
            {
                "channel": channel_id,
                "ts": thread_ts,
            },
        )
    except requests.RequestException as error:
        logger.exception("Slack thread fetch failed due to an HTTP error")
        raise RuntimeError("Could not fetch Slack thread") from error
    except Exception as error:
        logger.exception("Slack thread fetch failed")
        raise RuntimeError("Could not fetch Slack thread") from error

    messages = []

    for message in data.get("messages", []):
        messages.append(
            {
                "user": message.get("user", ""),
                "text": message.get("text", ""),
                "ts": message.get("ts", ""),
                "thread_ts": message.get("thread_ts") or message.get("ts", ""),
            }
        )

    logger.info("Fetched Slack thread with %s messages", len(messages))
    return messages


def fallback_slack_thread_url(channel_id, thread_ts):
    if not channel_id or not thread_ts:
        return ""

    return f"https://slack.com/app_redirect?channel={channel_id}&message_ts={thread_ts}"


def get_slack_thread_permalink(channel_id, thread_ts):
    if not channel_id or not thread_ts:
        return ""

    try:
        data = slack_api_get(
            "chat.getPermalink",
            {
                "channel": channel_id,
                "message_ts": thread_ts,
            },
        )
        return data.get("permalink") or fallback_slack_thread_url(channel_id, thread_ts)
    except Exception:
        logger.warning("Could not fetch Slack thread permalink; using app_redirect fallback")
        return fallback_slack_thread_url(channel_id, thread_ts)




def normalize_slack_ts(value):
    return clean_text(value)


def build_slack_source_key(channel_id, thread_ts):
    channel_id = clean_text(channel_id)
    thread_ts = normalize_slack_ts(thread_ts)

    if not channel_id or not thread_ts:
        return ""

    return f"slack-thread:{channel_id}:{thread_ts}"


def issue_contains_source_key(existing_issue, source_key):
    if not source_key:
        return False

    description = existing_issue.get("description") or ""
    return source_key in description


def source_key_issue_match_score(proposed_issue, existing_issue):
    proposed_title = proposed_issue.get("title", "")
    existing_title = existing_issue.get("title", "")

    return max(
        similarity(proposed_title, existing_title),
        token_overlap_score(proposed_title, existing_title),
    )


def source_key_existing_issue_match(proposed_issue, existing_issues, source_key):
    if not source_key:
        return None

    best_issue = None
    best_score = 0

    for existing_issue in existing_issues or []:
        if not issue_contains_source_key(existing_issue, source_key):
            continue

        if incompatible_task_types(proposed_issue, existing_issue):
            logger.info(
                "Skipped same-source duplicate match because task types differ: proposed='%s' existing='%s'",
                proposed_issue.get("title", ""),
                existing_issue.get("title", ""),
            )
            continue

        score = source_key_issue_match_score(proposed_issue, existing_issue)

        if score > best_score:
            best_score = score
            best_issue = existing_issue

    if best_issue and best_score >= 0.72:
        return best_issue

    return None


def get_slack_user_email(slack_user_id):
    if not slack_user_id:
        return None

    data = slack_api_get("users.info", {"user": slack_user_id})
    profile = data.get("user", {}).get("profile", {})
    email = profile.get("email")

    logger.info("Resolved Slack user email: %s", email or "not found")
    return email


def linear_graphql(query, variables=None):
    response = requests.post(
        LINEAR_API_URL,
        json={
            "query": query,
            "variables": variables or {},
        },
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    response.raise_for_status()
    data = response.json()

    if "errors" in data:
        raise RuntimeError(data["errors"])

    return data


def linear_priority_value(priority):
    priority_map = {
        "none": 0,
        "urgent": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }

    return priority_map.get((priority or "none").lower(), 0)


def normalize_name(value):
    return (value or "").strip().lower()


def similarity(a, b):
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def get_linear_users():
    query = """
    query Users {
      users {
        nodes {
          id
          name
          displayName
          email
          active
        }
      }
    }
    """

    data = linear_graphql(query)
    users = data["data"]["users"]["nodes"]
    return [user for user in users if user.get("active", True)]


def find_linear_user_by_email(email):
    if not email:
        return None

    users = get_linear_users()
    target_email = email.lower().strip()

    for user in users:
        linear_email = (user.get("email") or "").lower().strip()

        if linear_email == target_email:
            return user

    return None


def find_linear_user_by_name(assignee_name):
    if not assignee_name:
        return None

    users = get_linear_users()
    best_user = None
    best_score = 0

    for user in users:
        candidates = [
            user.get("name", ""),
            user.get("displayName", ""),
            user.get("email", ""),
        ]

        for candidate in candidates:
            score = similarity(assignee_name, candidate)

            if normalize_name(assignee_name) in normalize_name(candidate):
                score = max(score, 0.9)

            if score > best_score:
                best_score = score
                best_user = user

    if best_score >= 0.72 and best_user:
        return best_user

    return None


def display_linear_user(user):
    if not user:
        return ""

    return user.get("displayName") or user.get("name") or user.get("email") or ""


def resolve_assignee(assignee_name, requester_slack_user_id=""):
    assignee_lower = normalize_name(assignee_name)

    if assignee_lower in ["i", "me", "myself"]:
        slack_email = get_slack_user_email(requester_slack_user_id)
        user = find_linear_user_by_email(slack_email)

        if user:
            logger.info("Matched Slack user to Linear assignee: %s", display_linear_user(user))
        else:
            logger.info("Could not match Slack email to Linear user")

        return user

    if assignee_name:
        user = find_linear_user_by_name(assignee_name)

        if user:
            logger.info("Matched named assignee '%s' to Linear user: %s", assignee_name, display_linear_user(user))
        else:
            logger.info("Could not match named assignee: %s", assignee_name)

        return user

    return None


def create_linear_issue(
    title,
    description="",
    priority="none",
    assignee_name="",
    due_date="",
    requester_slack_user_id="",
):
    assignee = resolve_assignee(assignee_name, requester_slack_user_id)
    assignee_id = assignee["id"] if assignee else None

    mutation = """
    mutation IssueCreate(
      $teamId: String!,
      $title: String!,
      $description: String,
      $priority: Int,
      $assigneeId: String,
      $dueDate: TimelessDate
    ) {
      issueCreate(
        input: {
          teamId: $teamId,
          title: $title,
          description: $description,
          priority: $priority,
          assigneeId: $assigneeId,
          dueDate: $dueDate
        }
      ) {
        success
        issue {
          id
          identifier
          title
          priority
          priorityLabel
          url
          dueDate
          assignee {
            id
            name
            displayName
          }
        }
      }
    }
    """

    variables = {
        "teamId": LINEAR_TEAM_ID,
        "title": title,
        "description": description or "",
        "priority": linear_priority_value(priority),
        "assigneeId": assignee_id,
        "dueDate": due_date or None,
    }

    logger.info("Creating Linear issue: title='%s', priority='%s', due_date='%s'", title, priority, due_date or "none")
    return linear_graphql(mutation, variables)


def get_recent_linear_issues(limit=200):
    query = """
    query RecentIssues($teamId: String!, $first: Int!) {
      issues(
        first: $first,
        filter: { team: { id: { eq: $teamId } } }
      ) {
        nodes {
          id
          identifier
          title
          description
          url
        }
      }
    }
    """

    try:
        data = linear_graphql(
            query,
            {
                "teamId": LINEAR_TEAM_ID,
                "first": limit,
            },
        )
        return data["data"]["issues"]["nodes"]
    except Exception:
        logger.exception("Could not fetch recent Linear issues for duplicate matching")
        return []


def issue_contains_source_url(existing_issue, source_url):
    if not source_url:
        return False

    description = existing_issue.get("description") or ""
    return source_url in description



def task_type(text):
    text = normalize_name(text)

    if re.search(r"\b(test|verify|qa|check|validate)\b", text):
        return "test"

    if re.search(r"\b(fix|implement|build|update|resolve|repair)\b", text):
        return "implementation"

    return "other"


def incompatible_task_types(proposed_issue, existing_issue):
    proposed_type = task_type(proposed_issue.get("title", ""))
    existing_type = task_type(existing_issue.get("title", ""))

    return {proposed_type, existing_type} == {"test", "implementation"}


def issue_match_score(proposed_issue, existing_issue):
    proposed_text = " ".join(
        [
            proposed_issue.get("title", ""),
            proposed_issue.get("description", ""),
        ]
    )
    existing_text = " ".join(
        [
            existing_issue.get("title", ""),
            existing_issue.get("description", "") or "",
        ]
    )

    return max(
        similarity(proposed_issue.get("title", ""), existing_issue.get("title", "")),
        token_overlap_score(proposed_text, existing_text),
    )


def find_existing_linear_issue_match(proposed_issue, existing_issues, source_url="", source_key=""):
    source_key_match = source_key_existing_issue_match(
        proposed_issue,
        existing_issues,
        source_key,
    )

    if source_key_match:
        return source_key_match

    best_issue = None
    best_score = 0

    for existing_issue in existing_issues:
        if source_key and issue_contains_source_key(existing_issue, source_key):
            continue

        if source_key and source_url and issue_contains_source_url(existing_issue, source_url):
            continue

        if incompatible_task_types(proposed_issue, existing_issue):
            continue

        score = issue_match_score(proposed_issue, existing_issue)

        if source_url and not source_key and issue_contains_source_url(existing_issue, source_url):
            score = max(score, 0.75)

        if score > best_score:
            best_score = score
            best_issue = existing_issue

    if best_issue and best_score >= 0.78:
        return best_issue

    return None

def mark_existing_issue_match(proposed_issue, match, match_type="possible_duplicate"):
    proposed_issue["existing_issue_match"] = (
        f"{match['identifier']}: {match['title']}\n  {match['url']}"
    )
    proposed_issue["existing_issue_url"] = match.get("url", "")
    proposed_issue["existing_issue_match_type"] = match_type


def existing_issue_match_type(existing_issue, source_key=""):
    if source_key and issue_contains_source_key(existing_issue, source_key):
        return "already_tracked"

    return "possible_duplicate"


def annotate_existing_linear_matches(analysis, source_url="", source_key=""):
    proposed_issues = analysis.get("proposed_issues", []) or []

    if not proposed_issues:
        return analysis

    existing_issues = get_recent_linear_issues()

    if not existing_issues:
        return analysis

    for proposed_issue in proposed_issues:
        match = find_existing_linear_issue_match(
            proposed_issue,
            existing_issues,
            source_url=source_url,
            source_key=source_key,
        )

        if match:
            mark_existing_issue_match(
                proposed_issue,
                match,
                existing_issue_match_type(match, source_key),
            )
            logger.info(
                "Matched proposed issue '%s' to existing Linear issue %s",
                proposed_issue.get("title", ""),
                match.get("identifier", ""),
            )

    return analysis




def analyze_thread_with_ai(messages):
    if not messages:
        logger.info("Skipping Slack thread analysis because there are no messages")
        return {
            "summary": "",
            "decisions": [],
            "action_items": [],
            "blockers": [],
            "unresolved_questions": [],
            "proposed_issues": [],
        }

    today_date = date.today()
    today = today_date.isoformat()
    cleaned_messages = [
        {
            "user": message.get("user", ""),
            "text": message.get("text", ""),
            "ts": message.get("ts", ""),
            "thread_ts": message.get("thread_ts", ""),
        }
        for message in messages
    ]

    schema = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A short summary of the Slack thread.",
            },
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["decision", "evidence"],
                    "additionalProperties": False,
                },
            },
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "assignee_name": {"type": "string"},
                        "due_date": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["none", "low", "medium", "high", "urgent"],
                        },
                        "evidence": {"type": "string"},
                    },
                    "required": [
                        "task",
                        "assignee_name",
                        "due_date",
                        "priority",
                        "evidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "blockers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "blocker": {"type": "string"},
                        "owner": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["blocker", "owner", "evidence"],
                    "additionalProperties": False,
                },
            },
            "unresolved_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["question", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "summary",
            "decisions",
            "action_items",
            "blockers",
            "unresolved_questions",
        ],
        "additionalProperties": False,
    }

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Analyze the Slack thread and extract only information supported by the messages. "
                    "Do not invent facts, owners, deadlines, priorities, decisions, blockers, or questions. "
                    "Extract action_items only when someone explicitly commits to doing work, assigns work, "
                    "or clearly owns the task. Do not add questions to action_items unless a speaker explicitly "
                    "commits to answering or doing that work. "
                    "Do not generate Linear issues. The app will create proposed Linear issues from action_items only. "
                    "If a message asks a question and no later message answers it, include it in "
                    "unresolved_questions. For example, an unanswered question like "
                    "'Do we need this fixed on mobile too?' should be unresolved, not an action item. "
                    f"If a due date is relative, infer it using today's date: {today} "
                    f"({today_date.strftime('%A')}). For named weekdays, use today if it matches, "
                    "otherwise use the next matching weekday after today. If a due date is vague, "
                    "such as 'after that,' leave due_date as an empty string. "
                    "Use the message user field as the speaker name. If a speaker uses first-person "
                    "ownership language like 'I can,', 'I will,', 'I\'ll,', 'I should,', or 'I need to,' "
                    "set assignee_name to that speaker for the corresponding action item. "
                    "For example, if Evan says 'I can fix it by Friday,', assignee_name should be 'Evan'. "
                    "If Sarah says 'I\'ll test OAuth edge cases,', assignee_name should be 'Sarah'. "
                    "Use an empty string for missing assignee_name, owner, or due_date. "
                    "Use priority 'none' when no priority is implied."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(cleaned_messages),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "slack_thread_analysis",
                "schema": schema,
                "strict": True,
            }
        },
    )

    analysis = json.loads(response.output_text)
    analysis["proposed_issues"] = []
    analysis = apply_speaker_assignee_inference(analysis, cleaned_messages)
    analysis = clean_thread_analysis(analysis, today_date)
    analysis["proposed_issues"] = build_proposed_issues_from_action_items(
        analysis.get("action_items", [])
    )
    logger.info(
        "AI analyzed Slack thread with %s messages, %s action items, and %s proposed issues",
        len(cleaned_messages),
        len(analysis.get("action_items", [])),
        len(analysis.get("proposed_issues", [])),
    )
    return analysis

QUESTION_START_PATTERNS = [
    "should we",
    "do we",
    "do we need",
    "should i",
    "can we",
    "could we",
    "would it",
]


def has_named_commitment(text):
    return bool(
        re.search(
            r"\b[A-Z][a-z]+:\s*(I['’]?ll|I will|I can|I need to|I should|I am going to|I'm going to)\b",
            text,
        )
        or re.search(r"\b[A-Z][a-z]+\s+will\b", text)
    )


def has_explicit_owner_or_commitment(action_item):
    if clean_text(action_item.get("assignee_name", "")):
        return True

    evidence = clean_text(action_item.get("evidence", ""))

    return has_first_person_ownership(evidence) or has_named_commitment(evidence)


def looks_like_question_task(text, evidence=""):
    combined = clean_text(" ".join([text or "", evidence or ""]))
    normalized = normalize_name(combined)

    has_question_shape = "?" in combined or any(
        normalized.startswith(pattern)
        or f": {pattern}" in normalized
        for pattern in QUESTION_START_PATTERNS
    )

    if not has_question_shape:
        return False

    return not (
        has_first_person_ownership(combined)
        or has_named_commitment(combined)
        or "assigned to" in normalized
        or "please" in normalized
    )


def evidence_is_unanswered_question(evidence):
    evidence = clean_text(evidence)
    normalized = normalize_name(evidence)

    if not normalized:
        return False

    question_phrases = [
        "asked if",
        "asked whether",
        "asks if",
        "asks whether",
        "wondered if",
        "wondered whether",
        "raised whether",
        "raised the question whether",
        "questioned whether",
        "wanted to know if",
        "wants to know if",
        "whether we should",
        "whether they should",
        "if we should",
        "if they should",
    ]

    has_question_evidence = any(phrase in normalized for phrase in question_phrases)

    if not has_question_evidence:
        return False

    return not (
        has_first_person_ownership(evidence)
        or has_named_commitment(evidence)
        or "assigned to" in normalized
        or "will test" in normalized
        or "will fix" in normalized
    )


def title_as_question_from_action_task(task):
    task = clean_text(task).strip(" .")

    if not task:
        return ""

    lowered_task = task[:1].lower() + task[1:]
    return f"Should we {lowered_task}?"


def unresolved_question_from_action_item(action_item):
    task = clean_text(action_item.get("task", ""))
    evidence = clean_text(action_item.get("evidence", ""))
    question = ""

    if "?" in evidence:
        question_sentences = re.findall(r"([^.!?]*\?)", evidence)
        if question_sentences:
            question = clean_text(question_sentences[-1])
            question = re.sub(r"^[A-Za-z][A-Za-z\s]*:\s*", "", question).strip()

    if not question:
        match = re.search(
            r"\basked\s+(?:if|whether)\s+(?:we|they|the team)\s+should\s+(.+?)[\.?!]*$",
            evidence,
            flags=re.IGNORECASE,
        )
        if match:
            question = f"Should we {clean_text(match.group(1)).strip(' .?')}?"

    if not question:
        match = re.search(
            r"\bwondered\s+(?:if|whether)\s+(?:we|they|the team)\s+should\s+(.+?)[\.?!]*$",
            evidence,
            flags=re.IGNORECASE,
        )
        if match:
            question = f"Should we {clean_text(match.group(1)).strip(' .?')}?"

    if not question:
        question = title_as_question_from_action_task(task)

    return {
        "question": question,
        "evidence": evidence,
    }

def is_trackable_action_item(action_item):
    task = normalize_name(action_item.get("task", ""))
    evidence = clean_text(action_item.get("evidence", ""))

    if not task:
        return False

    if evidence_is_unanswered_question(evidence):
        logger.info("Filtered action item because evidence is an unanswered question: %s", action_item.get("task", ""))
        return False

    if looks_like_question_task(action_item.get("task", ""), evidence):
        logger.info("Filtered action item as unresolved question: %s", action_item.get("task", ""))
        return False

    if not has_explicit_owner_or_commitment(action_item):
        logger.info("Filtered action item without explicit owner or commitment: %s", action_item.get("task", ""))
        return False

    vague_tasks = {
        "discuss",
        "follow up",
        "look into this",
        "check this",
        "review",
        "talk about it",
    }

    if task in vague_tasks:
        return False

    return len(task.split()) >= 3 or bool(evidence.strip())


def dedupe_proposed_issues(proposed_issues):
    deduped = []

    for proposed_issue in proposed_issues or []:
        title = proposed_issue.get("title", "")
        normalized_title = normalize_name(title)
        proposed_type = task_type(title)
        duplicate_index = None

        for index, existing in enumerate(deduped):
            existing_title = existing.get("title", "")
            existing_normalized = normalize_name(existing_title)

            if proposed_type != task_type(existing_title):
                continue

            is_duplicate = (
                normalized_title == existing_normalized
                or normalized_title in existing_normalized
                or existing_normalized in normalized_title
                or similarity(title, existing_title) >= 0.84
            )

            if is_duplicate:
                duplicate_index = index
                break

        if duplicate_index is None:
            deduped.append(proposed_issue)
            continue

        current = deduped[duplicate_index]
        current_score = len(current.get("title", "")) + 20 * bool(current.get("assignee_name")) + 20 * bool(current.get("evidence"))
        proposed_score = len(proposed_issue.get("title", "")) + 20 * bool(proposed_issue.get("assignee_name")) + 20 * bool(proposed_issue.get("evidence"))

        if proposed_score > current_score:
            deduped[duplicate_index] = proposed_issue

    return deduped


def build_proposed_issues_from_action_items(action_items):
    proposed_issues = [
        proposed_issue_from_action_item(action_item)
        for action_item in action_items or []
        if is_trackable_action_item(action_item)
    ]

    return dedupe_proposed_issues(proposed_issues)

def proposed_issue_exists(action_item, proposed_issues):
    task = action_item.get("task", "")

    for issue in proposed_issues:
        title = issue.get("title", "")

        if similarity(task, title) >= 0.72:
            return True

    return False


def proposed_issue_from_action_item(action_item):
    task = action_item.get("task", "").strip()
    evidence = action_item.get("evidence", "").strip()

    return {
        "title": task,
        "description": task,
        "priority": action_item.get("priority") or "none",
        "assignee_name": action_item.get("assignee_name", ""),
        "due_date": action_item.get("due_date", ""),
        "evidence": evidence,
    }


def ensure_proposed_issues_for_action_items(action_items, proposed_issues):
    proposed_issues = list(proposed_issues or [])

    for action_item in action_items or []:
        if not is_trackable_action_item(action_item):
            continue

        if proposed_issue_exists(action_item, proposed_issues):
            continue

        proposed_issues.append(proposed_issue_from_action_item(action_item))

    return proposed_issues


FIRST_PERSON_OWNERSHIP_PATTERNS = [
    r"\bi can\b",
    r"\bi will\b",
    r"\bi['’]ll\b",
    r"\bi should\b",
    r"\bi need to\b",
    r"\bi can take\b",
    r"\bi can fix\b",
    r"\bi can test\b",
    r"\bi'll take\b",
    r"\bi’ll take\b",
]


THREAD_MATCH_STOPWORDS = {
    "the",
    "and",
    "that",
    "this",
    "with",
    "from",
    "after",
    "before",
    "will",
    "need",
    "needs",
    "should",
    "could",
    "would",
    "have",
    "has",
    "for",
    "too",
    "fix",
    "test",
}


def has_first_person_ownership(text):
    text = normalize_name(text)

    return any(
        re.search(pattern, text)
        for pattern in FIRST_PERSON_OWNERSHIP_PATTERNS
    )


def meaningful_tokens(text):
    tokens = re.findall(r"[a-z0-9]+", normalize_name(text))

    return {
        token
        for token in tokens
        if len(token) >= 3 and token not in THREAD_MATCH_STOPWORDS
    }


def token_overlap_score(a, b):
    a_tokens = meaningful_tokens(a)
    b_tokens = meaningful_tokens(b)

    if not a_tokens or not b_tokens:
        return 0

    return len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))


def infer_speaker_assignee_for_item(item, messages, primary_fields):
    current_assignee = clean_text(item.get("assignee_name", ""))

    if current_assignee:
        return current_assignee

    item_text = " ".join(
        clean_text(item.get(field, ""))
        for field in primary_fields
    )

    best_user = ""
    best_score = 0

    for message in messages:
        user = clean_text(message.get("user", ""))
        message_text = clean_text(message.get("text", ""))

        if not user or not message_text:
            continue

        if not has_first_person_ownership(message_text):
            continue

        score = max(
            token_overlap_score(item_text, message_text),
            token_overlap_score(item.get("evidence", ""), message_text),
        )

        evidence = normalize_name(item.get("evidence", ""))
        normalized_message = normalize_name(message_text)

        if evidence and (
            evidence in normalized_message or normalized_message in evidence
        ):
            score = max(score, 1.0)

        if score > best_score:
            best_score = score
            best_user = user

    if best_score >= 0.2:
        return best_user

    return ""


def apply_speaker_assignee_inference(analysis, messages):
    messages = messages or []

    for item in analysis.get("action_items", []) or []:
        inferred_assignee = infer_speaker_assignee_for_item(
            item,
            messages,
            ["task", "evidence"],
        )

        if inferred_assignee:
            item["assignee_name"] = inferred_assignee

    return analysis


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def clean_items(items, primary_field):
    cleaned = []

    for item in items or []:
        cleaned_item = {
            key: clean_text(value)
            for key, value in item.items()
        }

        if not cleaned_item.get(primary_field):
            continue

        cleaned.append(cleaned_item)

    return cleaned


def clean_due_date(value, evidence, today_date):
    value = clean_text(value)
    evidence_text = normalize_name(evidence)

    if value and has_vague_due_date(evidence_text) and not has_specific_due_date(evidence_text):
        return ""

    if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""

    weekday_due_date = due_date_from_weekday(evidence, today_date)

    if weekday_due_date:
        return weekday_due_date

    if value:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return ""

        if parsed_date < today_date and str(parsed_date.year) not in evidence_text:
            return ""

    return value


def has_vague_due_date(text):
    vague_phrases = [
        "after that",
        "later",
        "soon",
        "eventually",
        "next step",
        "then",
    ]

    return any(phrase in text for phrase in vague_phrases)


def has_specific_due_date(text):
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        return True

    if re.search(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", text):
        return True

    return any(
        re.search(rf"\b{weekday}\b", text)
        for weekday in [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
    )


def due_date_from_weekday(text, today_date):
    text = normalize_name(text)
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    for weekday_name, weekday_index in weekdays.items():
        if not re.search(rf"\b(by|on|before|this|next)?\s*{weekday_name}\b", text):
            continue

        days_until = (weekday_index - today_date.weekday()) % 7

        if "next" in text and days_until == 0:
            days_until = 7

        return (today_date + timedelta(days=days_until)).isoformat()

    return ""


def remove_evidence_from_description(description, evidence):
    description = clean_text(description)
    evidence = clean_text(evidence)

    if not description:
        return ""

    lines = [
        line.strip()
        for line in description.splitlines()
        if line.strip() and not line.strip().lower().startswith("evidence:")
    ]
    description = "\n".join(lines).strip()

    if evidence and evidence in description:
        description = description.replace(evidence, "").strip()
        description = re.sub(r"\s+", " ", description).strip(" :-")

    return description


def clean_thread_analysis(analysis, today_date):
    cleaned = {
        "summary": clean_text(analysis.get("summary", "")),
        "decisions": clean_items(analysis.get("decisions", []), "decision"),
        "action_items": clean_items(analysis.get("action_items", []), "task"),
        "blockers": clean_items(analysis.get("blockers", []), "blocker"),
        "unresolved_questions": clean_items(
            analysis.get("unresolved_questions", []),
            "question",
        ),
        "proposed_issues": clean_items(analysis.get("proposed_issues", []), "title"),
    }

    kept_action_items = []
    existing_questions = {
        normalize_name(item.get("question", ""))
        for item in cleaned["unresolved_questions"]
    }

    for item in cleaned["action_items"]:
        item["due_date"] = clean_due_date(
            item.get("due_date", ""),
            item.get("evidence", ""),
            today_date,
        )

        if evidence_is_unanswered_question(item.get("evidence", "")):
            question_item = unresolved_question_from_action_item(item)
            normalized_question = normalize_name(question_item.get("question", ""))

            if normalized_question and normalized_question not in existing_questions:
                cleaned["unresolved_questions"].append(question_item)
                existing_questions.add(normalized_question)

            logger.info(
                "Moved action item to unresolved question because evidence is a question: %s",
                item.get("task", ""),
            )
            continue

        kept_action_items.append(item)

    cleaned["action_items"] = kept_action_items

    for item in cleaned["proposed_issues"]:
        item["due_date"] = clean_due_date(
            item.get("due_date", ""),
            item.get("evidence", ""),
            today_date,
        )
        item["description"] = remove_evidence_from_description(
            item.get("description", ""),
            item.get("evidence", ""),
        )

    return cleaned


def format_issue_response(issue, priority, assignee_name="", due_date=""):
    assignee = issue.get("assignee")
    linear_assignee_name = display_linear_user(assignee)

    lines = [
        f"Created Linear issue {issue['identifier']}: {issue['title']}",
        f"Priority: {priority}",
    ]

    if assignee_name:
        if linear_assignee_name:
            lines.append(f"Assignee: {linear_assignee_name}")
        elif normalize_name(assignee_name) in ["i", "me", "myself"]:
            lines.append("Assignee: couldn’t match your Slack email to a Linear user.")
        else:
            lines.append(
                f"Assignee: {assignee_name} was mentioned, but I couldn’t match them to a Linear user."
            )

    if due_date:
        lines.append(f"Due date: {due_date}")

    lines.append(issue["url"])
    return "\n".join(lines)


def format_thread_analysis_response(analysis, source_url=""):
    analysis = clean_thread_analysis(analysis, date.today())
    lines = [
        "Thread analysis preview. No Linear issues were created.",
        "",
        "Summary",
        analysis.get("summary") or "No summary available.",
        "",
    ]

    if source_url:
        lines.extend(["Source Slack thread", source_url, ""])

    sections = [
        ("Decisions", analysis.get("decisions", []), ["decision", "evidence"]),
        (
            "Action items",
            analysis.get("action_items", []),
            ["task", "assignee_name", "due_date", "priority", "evidence"],
        ),
        ("Blockers", analysis.get("blockers", []), ["blocker", "owner", "evidence"]),
        (
            "Unresolved questions",
            analysis.get("unresolved_questions", []),
            ["question", "evidence"],
        ),
        (
            "Proposed Linear issues",
            analysis.get("proposed_issues", []),
            [
                "title",
                "description",
                "priority",
                "assignee_name",
                "due_date",
                "existing_issue_match",
                "evidence",
            ],
        ),
    ]

    sections = [
        (section_title, items, fields)
        for section_title, items, fields in sections
        if items
    ]

    if not sections:
        lines.append("Nothing actionable was found in this conversation.")
        return "\n".join(lines)

    for section_title, items, fields in sections:
        lines.append(section_title)

        for index, item in enumerate(items, start=1):
            primary_field = fields[0]
            primary_value = clean_text(item.get(primary_field, ""))
            lines.append(f"{index}. {primary_value}")
            displayed_values = {normalize_name(primary_value)}

            for field in fields[1:]:
                value = clean_text(item.get(field, ""))

                if not value:
                    continue

                normalized_value = normalize_name(value)

                if field == "evidence" and normalized_value in displayed_values:
                    continue

                label = field.replace("_", " ").title()

                if field == "existing_issue_match":
                    if item.get("existing_issue_match_type") == "already_tracked":
                        label = "Already Tracked Linear Issue"
                    else:
                        label = "Possible Existing Linear Issue"

                lines.append(f"   {label}: {value}")
                displayed_values.add(normalized_value)

        lines.append("")

    proposed_issues = analysis.get("proposed_issues", []) or []
    if proposed_issues and not createable_proposed_issues(proposed_issues):
        lines.append("All proposed Linear issues appear to already be tracked. No create button is shown.")

    return "\n".join(lines).strip()



def proposed_issue_has_existing_match(proposed_issue):
    status = clean_text(proposed_issue.get("status", ""))

    if status in {"ignored", "created", "matched", "possible_duplicate"}:
        return True

    if is_future_timestamp(proposed_issue.get("snoozed_until", "")):
        return True

    return bool(proposed_issue.get("existing_issue_match") or proposed_issue.get("existing_issue_url"))


def createable_proposed_issues(proposed_issues):
    return [
        proposed_issue
        for proposed_issue in proposed_issues or []
        if not proposed_issue_has_existing_match(proposed_issue)
    ]


def append_source_context(description, source_url="", source_key="", evidence=""):
    parts = [clean_text(description)]

    if source_url:
        parts.extend(["", f"Source Slack thread:\n{source_url}"])

    if source_key:
        parts.extend(["", f"Slack source key:\n{source_key}"])

    if evidence:
        parts.extend(["", f"Evidence:\n{evidence}"])

    return "\n".join(part for part in parts if part is not None).strip()





def build_thread_analysis_blocks(preview_text, preview_id, proposed_issue_count):
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{preview_text}```",
            },
        }
    ]

    if proposed_issue_count > 0:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"Create {proposed_issue_count} new Linear issue(s)",
                        },
                        "style": "primary",
                        "action_id": "create_thread_issues",
                        "value": preview_id,
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Cancel",
                        },
                        "action_id": "cancel_thread_issues",
                        "value": preview_id,
                    },
                ],
            }
        )

    return blocks




def compact_item_copy(item, fields):
    return {
        field: clean_text(item.get(field, ""))
        for field in fields
        if clean_text(item.get(field, ""))
    }


def record_thread_analysis_history(analysis, source_url="", source_key=""):
    # Kept as a compatibility wrapper for the dashboard flow.
    # The data now persists in SQLite instead of only living in memory.
    return save_thread_analysis(source_key, source_url, analysis)

def html_escape(value):
    return html.escape(clean_text(value), quote=True)


def render_ignore_button(item):
    item_id = item.get("id")
    status = clean_text(item.get("status", "new"))

    if not item_id or status == "ignored":
        return ""

    return f"""
        <form class="inline-form" method="post" action="/dashboard/items/{html_escape(item_id)}/ignore">
            <button type="submit" class="ignore-button">Ignore</button>
        </form>
    """


def render_mark_tracked_button(item):
    item_id = item.get("id")
    item_type = clean_text(item.get("item_type", ""))
    status = clean_text(item.get("status", "new"))

    if not item_id or status in {"ignored", "created", "matched"}:
        return ""

    if item_type not in {"action_item", "proposed_issue", "blocker"}:
        return ""

    return f"""
        <form class="inline-form track-form" method="post" action="/dashboard/items/{html_escape(item_id)}/mark-tracked">
            <input class="linear-reference-input" type="text" name="linear_reference" placeholder="FLO-123 or Linear URL" aria-label="Linear issue reference">
            <button type="submit" class="track-button">Mark tracked</button>
        </form>
    """


def render_snooze_button(item, hours=24):
    item_id = item.get("id")
    status = clean_text(item.get("status", "new"))

    if not item_id or status in {"ignored", "created", "matched"}:
        return ""

    return f"""
        <form class="inline-form" method="post" action="/dashboard/items/{html_escape(item_id)}/snooze/{hours}">
            <button type="submit" class="snooze-button">Snooze {hours}h</button>
        </form>
    """



def render_github_check_button(item):
    item_id = item.get("id")
    linear_identifier = clean_text(item.get("linear_identifier", ""))

    if not item_id or not linear_identifier:
        return ""

    return f"""
        <form class="inline-form" method="post" action="/dashboard/items/{html_escape(item_id)}/check-github">
            <button type="submit" class="github-button">Check GitHub</button>
        </form>
    """


def render_item_actions(item):
    actions = [
        render_mark_tracked_button(item),
        render_github_check_button(item),
        render_snooze_button(item, 24),
        render_ignore_button(item),
    ]
    actions = [action for action in actions if action]

    if not actions:
        return ""

    return '<div class="item-actions">' + "".join(actions) + "</div>"


def item_display_title(item, primary_field):
    return clean_text(item.get(primary_field, ""))


def item_status_label(item):
    status = clean_text(item.get("status", "new")) or "new"

    if is_future_timestamp(item.get("snoozed_until", "")) and status not in {"ignored", "created", "matched"}:
        return "snoozed"

    if status == "possible_duplicate":
        return "possible duplicate"

    return status


def item_meta_parts(item):
    parts = []
    assignee = clean_text(item.get("assignee_name", "") or item.get("owner", ""))
    priority = clean_text(item.get("priority", ""))
    due_date = clean_text(item.get("due_date", ""))
    linear_identifier = clean_text(item.get("linear_identifier", ""))
    github_status = clean_text(item.get("github_status", ""))

    if assignee:
        parts.append(assignee)
    if priority and priority != "none":
        parts.append(priority)
    if due_date:
        parts.append(f"due {due_date}")
    if linear_identifier:
        parts.append(linear_identifier)
    if github_status:
        parts.append(f"GitHub: {github_status.replace('_', ' ')}")

    return parts


def render_status_pill(item):
    label = item_status_label(item)
    css_label = normalize_name(label).replace(" ", "-") or "new"
    return f'<span class="status-pill status-{html_escape(css_label)}">{html_escape(label)}</span>'


def render_evidence_details(item):
    evidence = clean_text(item.get("evidence", ""))
    description = clean_text(item.get("description", ""))
    existing_match = clean_text(item.get("existing_issue_match", ""))
    linear_url = clean_text(item.get("linear_url", ""))
    github_status = clean_text(item.get("github_status", ""))
    github_url = clean_text(item.get("github_url", ""))
    github_checked_at = clean_text(item.get("github_checked_at", ""))
    snoozed_until = clean_text(item.get("snoozed_until", ""))
    rows = []

    if description and description != clean_text(item.get("title", "")):
        rows.append(f'<div><span class="label">Description:</span> {html_escape(description)}</div>')
    if existing_match:
        rows.append(f'<div><span class="label">Linear:</span> {html_escape(existing_match)}</div>')
    elif linear_url:
        rows.append(f'<div><span class="label">Linear:</span> <a href="{html_escape(linear_url)}" target="_blank" rel="noreferrer">{html_escape(linear_url)}</a></div>')
    if github_status:
        github_label = github_status.replace("_", " ")
        if github_url:
            rows.append(f'<div><span class="label">GitHub:</span> {html_escape(github_label)} · <a href="{html_escape(github_url)}" target="_blank" rel="noreferrer">Open pull request</a></div>')
        else:
            rows.append(f'<div><span class="label">GitHub:</span> {html_escape(github_label)}</div>')
    if github_checked_at:
        rows.append(f'<div><span class="label">GitHub checked:</span> {html_escape(github_checked_at)}</div>')
    if snoozed_until:
        rows.append(f'<div><span class="label">Snoozed until:</span> {html_escape(snoozed_until)}</div>')
    if evidence:
        rows.append(f'<div><span class="label">Evidence:</span> {html_escape(evidence)}</div>')

    if not rows:
        return ""

    return f'''
        <details class="item-details">
            <summary>Details</summary>
            <div class="details">{''.join(rows)}</div>
        </details>
    '''


def render_item_row(item, primary_field):
    title = item_display_title(item, primary_field)
    meta = " · ".join(item_meta_parts(item))
    meta_html = f'<div class="item-meta">{html_escape(meta)}</div>' if meta else ""

    return f'''
        <li class="item-row">
            <div class="item-row-main">
                <div class="item-copy">
                    <div class="item-title-line">
                        <strong>{html_escape(title)}</strong>
                        {render_status_pill(item)}
                    </div>
                    {meta_html}
                </div>
                {render_item_actions(item)}
            </div>
            {render_evidence_details(item)}
        </li>
    '''


def render_item_list(items, primary_field):
    if not items:
        return '<p class="muted compact-empty">None</p>'

    rows = [render_item_row(item, primary_field) for item in items]
    return '<ol class="compact-item-list">' + ''.join(rows) + '</ol>'


def render_section_if_items(title, items, primary_field):
    if not items:
        return ""

    return f'''
        <section class="card-section">
            <h3>{html_escape(title)}</h3>
            {render_item_list(items, primary_field)}
        </section>
    '''


def render_dashboard_risks(risks):
    if not risks:
        return """
            <section class="risk-section review-section">
                <div class="section-header">
                    <div>
                        <h2>Needs review</h2>
                        <p class="meta">No current risks based on saved dashboard items.</p>
                    </div>
                    <span class="badge muted-badge">0 active</span>
                </div>
            </section>
        """

    cards = []

    for risk in risks:
        source_url = risk.get("source_url", "")
        source_link = (
            f'<a href="{html_escape(source_url)}" target="_blank" rel="noreferrer">Open Slack thread</a>'
            if source_url else '<span class="muted">No source URL</span>'
        )
        item = risk.get("item", {})
        reasons = "".join(f'<li>{html_escape(reason)}</li>' for reason in risk.get("reasons", []))
        summary = html_escape(risk.get("analysis_summary", ""))
        summary_line = f'<p class="risk-summary">{summary}</p>' if summary else ""
        cards.append(f"""
            <article class="risk-card">
                <div class="risk-card-main">
                    <div>
                        <h3>{html_escape(risk.get('title', 'Untitled risk'))}</h3>
                        <p class="meta">{html_escape(risk.get('created_at', ''))} · {source_link}</p>
                        {summary_line}
                        <ul class="risk-reasons">{reasons}</ul>
                    </div>
                    {render_item_actions(item)}
                </div>
            </article>
        """)

    return f"""
        <section class="risk-section review-section">
            <div class="section-header">
                <div>
                    <h2>Needs review</h2>
                    <p class="meta">Risks, blockers, and untracked work that may need attention.</p>
                </div>
                <span class="badge risk-badge">{len(risks)} active</span>
            </div>
            <div class="risk-grid">{''.join(cards)}</div>
        </section>
    """


def dashboard_redirect_location(request, extra_params=None):
    extra_params = extra_params or {}
    referer = request.headers.get("referer", "")
    host_prefix = f"{request.url.scheme}://{request.url.netloc}"

    if referer.startswith(host_prefix):
        location = referer[len(host_prefix):]
    elif referer.startswith("/dashboard"):
        location = referer
    else:
        location = "/dashboard"

    if not location.startswith("/dashboard"):
        location = "/dashboard"

    if extra_params:
        separator = "&" if "?" in location else "?"
        location = f"{location}{separator}{urlencode(extra_params)}"

    return location


def render_dashboard_notice(github_status="", github_identifier="", github_url=""):
    github_status = clean_text(github_status)
    github_identifier = clean_text(github_identifier)
    github_url = clean_text(github_url)

    if not github_status:
        return ""

    if github_status == "found":
        message = f"GitHub pull request found for {github_identifier}." if github_identifier else "GitHub pull request found."
        link = f' <a href="{html_escape(github_url)}" target="_blank" rel="noreferrer">Open pull request</a>' if github_url else ""
        css_class = "notice-success"
    elif github_status == "not_found":
        message = f"No GitHub pull request found for {github_identifier}." if github_identifier else "No GitHub pull request found."
        link = ""
        css_class = "notice-muted"
    elif github_status == "error":
        message = f"GitHub lookup failed for {github_identifier}. Check GitHub repo settings and token permissions." if github_identifier else "GitHub lookup failed. Check GitHub repo settings and token permissions."
        link = ""
        css_class = "notice-error"
    else:
        message = github_status
        link = ""
        css_class = "notice-muted"

    return f'<div class="dashboard-notice {css_class}">{html_escape(message)}{link}</div>'


def dashboard_search_url(item_filter, search_query=""):
    base = f"/dashboard?filter={quote(clean_text(item_filter) or 'all')}"
    search_query = clean_text(search_query)
    if search_query:
        base += f"&q={quote(search_query)}"
    return base


def render_dashboard_filter_tabs(active_filter="all", search_query=""):
    active_filter = normalize_dashboard_filter(active_filter)
    filters = [("all", "All"), ("risks", "Risks"), ("new", "New"), ("tracked", "Tracked"), ("snoozed", "Snoozed"), ("ignored", "Ignored")]
    links = []
    for value, label in filters:
        active_class = " active-filter" if value == active_filter else ""
        links.append(f'<a class="filter-link{active_class}" href="{html_escape(dashboard_search_url(value, search_query))}">{html_escape(label)}</a>')
    return '<nav class="filter-tabs">' + "".join(links) + '</nav>'


def render_dashboard_search_form(search_query="", item_filter="all"):
    return f'''
        <form class="dashboard-search" method="get" action="/dashboard">
            <input type="hidden" name="filter" value="{html_escape(normalize_dashboard_filter(item_filter))}">
            <label>
                Search dashboard
                <input name="q" type="search" value="{html_escape(search_query)}" placeholder="Search summary, item, assignee, FLO ID">
            </label>
            <button type="submit">Search</button>
            <a class="clear-search" href="/dashboard?filter={html_escape(normalize_dashboard_filter(item_filter))}">Clear</a>
        </form>
    '''


def render_channel_scan_history(limit=5):
    scans = get_recent_channel_scan_history(limit)
    if not scans:
        return ""
    rows = []
    for scan in scans:
        force_label = " · force" if scan.get("force_rescan") else ""
        rows.append(
            "<li>"
            f"<strong>{html_escape(scan.get('channel_id', ''))}</strong>"
            f"<span>{html_escape(scan.get('created_at', ''))}</span>"
            f"<span>{html_escape(scan.get('lookback_hours', ''))}h{force_label}</span>"
            f"<span>found {html_escape(scan.get('threads_found', 0))}, analyzed {html_escape(scan.get('analyzed', 0))}, skipped {html_escape(scan.get('skipped_existing', 0))}, failed {html_escape(scan.get('failed', 0))}</span>"
            "</li>"
        )
    return f"""
        <section class="scan-history-section sidebar-panel">
            <h2>Recent scans</h2>
            <ol class="scan-history-list">{''.join(rows)}</ol>
        </section>
    """


def render_scan_channel_form(scan_result=""):
    result_html = f'<div class="scan-result">{html_escape(scan_result)}</div>' if scan_result else ""
    return f"""
        <section class="scan-section sidebar-panel">
            <div>
                <h2>Scan channel</h2>
                <p class="meta">Scan new and changed Slack threads. Use force rescan when you want to refresh everything.</p>
            </div>
            <form class="scan-form" method="post" action="/dashboard/scan-channel">
                <label>Channel ID<input name="channel_id" type="text" placeholder="C1234567890" required></label>
                <label>Lookback hours<input name="lookback_hours" type="number" min="1" max="168" value="24" required></label>
                <label class="checkbox-label"><input name="force_rescan" type="checkbox" value="1"> Force rescan existing threads</label>
                <button type="submit">Scan channel</button>
            </form>
            {result_html}
        </section>
    """


def entry_item_text(item):
    return " ".join(clean_text(value) for value in item.values() if value)


def entry_matches_search(entry, search_query=""):
    search_query = normalize_name(search_query)
    if not search_query:
        return True
    haystack_parts = [entry.get("source_key", ""), entry.get("source_url", ""), entry.get("summary", ""), entry.get("created_at", ""), entry.get("updated_at", "")]
    for key in ["action_items", "unresolved_questions", "blockers", "proposed_issues"]:
        for item in entry.get(key, []) or []:
            haystack_parts.append(entry_item_text(item))
    return search_query in normalize_name(" ".join(haystack_parts))


def risk_matches_search(risk, search_query=""):
    search_query = normalize_name(search_query)
    if not search_query:
        return True
    haystack = " ".join([risk.get("title", ""), risk.get("analysis_summary", ""), risk.get("source_url", ""), " ".join(risk.get("reasons", []) or []), entry_item_text(risk.get("item", {}) or {})])
    return search_query in normalize_name(haystack)


def entry_counts(entry):
    return {
        "issues": len(entry.get("proposed_issues", []) or []),
        "questions": len(entry.get("unresolved_questions", []) or []),
        "blockers": len(entry.get("blockers", []) or []),
        "new": int(entry.get("createable_count", 0) or 0),
        "tracked": int(entry.get("tracked_count", 0) or 0),
    }


def render_count_chips(counts):
    chips = []
    for key, label in [("new", "new"), ("tracked", "tracked"), ("issues", "issues"), ("questions", "questions"), ("blockers", "blockers")]:
        value = counts.get(key, 0)
        if value:
            chips.append(f'<span class="count-chip">{html_escape(value)} {html_escape(label)}</span>')
    if not chips:
        chips.append('<span class="count-chip muted-chip">no visible items</span>')
    return '<div class="count-chips">' + ''.join(chips) + '</div>'


def render_thread_card(entry, open_by_default=False):
    source_url = entry.get("source_url", "")
    source_link = f'<a href="{html_escape(source_url)}" target="_blank" rel="noreferrer">Open Slack thread</a>' if source_url else '<span class="muted">No source URL</span>'
    open_attr = " open" if open_by_default else ""
    freshness = []
    if entry.get("reply_count") is not None:
        freshness.append(f"{entry.get('reply_count')} replies")
    if entry.get("last_scanned_at"):
        freshness.append(f"last scanned {entry.get('last_scanned_at')}")
    freshness_html = f'<p class="meta card-freshness">{" · ".join(html_escape(part) for part in freshness)}</p>' if freshness else ""
    sections = "".join([
        render_section_if_items("Proposed Linear issues", entry.get("proposed_issues", []), "title"),
        render_section_if_items("Unresolved questions", entry.get("unresolved_questions", []), "question"),
        render_section_if_items("Blockers", entry.get("blockers", []), "blocker"),
    ]) or '<p class="muted">No visible dashboard items in this filter.</p>'
    return f'''
        <details class="card thread-card"{open_attr}>
            <summary class="thread-summary">
                <div class="summary-main">
                    <h2>{html_escape(entry.get('summary', 'No summary available.'))}</h2>
                    <p class="meta">{html_escape(entry.get('updated_at', '') or entry.get('created_at', ''))} · {source_link}</p>
                    {freshness_html}
                </div>
                {render_count_chips(entry_counts(entry))}
            </summary>
            <div class="thread-body">{sections}</div>
        </details>
    '''


def render_dashboard_html(scan_result="", item_filter="all", search_query="", github_status="", github_identifier="", github_url=""):
    item_filter = normalize_dashboard_filter(item_filter)
    search_query = clean_text(search_query)
    risks = [risk for risk in get_dashboard_risks() if risk_matches_search(risk, search_query)]
    risk_html = render_dashboard_risks(risks) if item_filter in {"all", "risks"} else ""
    filter_tabs_html = render_dashboard_filter_tabs(item_filter, search_query)
    search_html = render_dashboard_search_form(search_query, item_filter)
    scan_history_html = render_channel_scan_history()
    notice_html = render_dashboard_notice(github_status, github_identifier, github_url)
    entries = [] if item_filter == "risks" else get_recent_thread_analyses(MAX_ANALYSIS_HISTORY, item_filter=item_filter)
    entries = [entry for entry in entries if entry_matches_search(entry, search_query)]
    cards = [render_thread_card(entry, open_by_default=False) for entry in entries]
    if not cards and item_filter != "risks":
        cards.append("""
            <article class="card empty">
                <h2>No matching dashboard items</h2>
                <p>Run the Slack message shortcut or scan a channel, then refresh this page. Saved analyses will stay after a server restart.</p>
            </article>
        """)
    history_html = f'''
        <section class="history-section">
            <div class="section-header">
                <div>
                    <h2>Recent thread history</h2>
                    <p class="meta">Cards are collapsed by default. Expand one when you need evidence or item controls.</p>
                </div>
                <span class="badge muted-badge">{len(entries)} shown</span>
            </div>
            {''.join(cards)}
        </section>
    ''' if item_filter != "risks" else ""

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Slack Linear Dashboard</title>
        <style>
            :root {{ --border: #e5e7eb; --muted: #6b7280; --blue: #2563eb; --bg: #f6f7f9; }}
            * {{ box-sizing: border-box; }}
            body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: #1f2937; }}
            header {{ padding: 26px 36px; background: white; border-bottom: 1px solid var(--border); }}
            h1 {{ margin: 0 0 8px; font-size: 28px; }}
            h2 {{ margin: 0; font-size: 18px; }}
            h3 {{ margin: 0 0 10px; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: #4b5563; }}
            main {{ padding: 24px 36px 48px; max-width: 1280px; }}
            .dashboard-layout {{ display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 22px; align-items: start; }}
            .dashboard-sidebar {{ position: sticky; top: 18px; display: flex; flex-direction: column; gap: 16px; }}
            .dashboard-content {{ min-width: 0; }}
            .sidebar-panel, .card, .risk-section, .history-section {{ background: white; border: 1px solid var(--border); border-radius: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
            .scan-section, .scan-history-section {{ padding: 18px; }}
            .scan-form {{ display: grid; gap: 12px; margin-top: 14px; }}
            .scan-form label, .dashboard-search label {{ display: flex; flex-direction: column; gap: 5px; font-size: 13px; color: #4b5563; }}
            .scan-form input, .dashboard-search input, .linear-reference-input {{ border: 1px solid #d1d5db; border-radius: 10px; padding: 8px 10px; font-size: 14px; width: 100%; }}
            .scan-form input[type=checkbox] {{ width: auto; }}
            .checkbox-label {{ flex-direction: row !important; align-items: center; }}
            button {{ border: 1px solid var(--blue); background: var(--blue); color: white; border-radius: 999px; padding: 8px 12px; font-size: 14px; cursor: pointer; }}
            button:hover {{ background: #1d4ed8; }}
            .scan-result {{ margin-top: 12px; background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; border-radius: 12px; padding: 10px 12px; font-size: 14px; }}
            .dashboard-notice {{ margin-bottom: 14px; border-radius: 14px; padding: 12px 14px; font-size: 14px; border: 1px solid var(--border); }}
            .notice-success {{ background: #ecfdf5; border-color: #bbf7d0; color: #047857; }}
            .notice-muted {{ background: #f8fafc; border-color: #e5e7eb; color: #475569; }}
            .notice-error {{ background: #fef2f2; border-color: #fecaca; color: #b91c1c; }}
            .filter-tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
            .filter-link {{ border: 1px solid #d1d5db; background: white; color: #374151; border-radius: 999px; padding: 7px 11px; font-size: 14px; }}
            .active-filter {{ border-color: var(--blue); background: #eff6ff; color: #1d4ed8; font-weight: 600; }}
            .dashboard-search {{ display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 10px; align-items: end; background: white; border: 1px solid var(--border); border-radius: 16px; padding: 14px; margin-bottom: 18px; }}
            .clear-search {{ align-self: center; color: var(--muted); font-size: 14px; }}
            .scan-history-list {{ list-style: none; margin: 12px 0 0; padding: 0; display: grid; gap: 10px; color: #4b5563; font-size: 13px; }}
            .scan-history-list li {{ display: grid; gap: 2px; margin: 0; padding-bottom: 10px; border-bottom: 1px solid #f3f4f6; }}
            .section-header {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 14px; }}
            .history-section, .risk-section {{ padding: 18px; margin-bottom: 20px; }}
            .card {{ margin-bottom: 14px; overflow: hidden; }}
            .thread-card {{ padding: 0; }}
            .thread-summary {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; padding: 18px; cursor: pointer; list-style: none; }}
            .thread-summary::-webkit-details-marker {{ display: none; }}
            .thread-summary::before {{ content: "▸"; color: var(--muted); margin-top: 2px; }}
            .thread-card[open] .thread-summary::before {{ content: "▾"; }}
            .summary-main {{ flex: 1; min-width: 0; }}
            .thread-body {{ border-top: 1px solid #f3f4f6; padding: 18px; display: grid; gap: 16px; }}
            .card-section {{ border: 1px solid #f3f4f6; border-radius: 14px; padding: 14px; background: #fcfcfd; }}
            .meta, .muted {{ color: var(--muted); }}
            .meta {{ margin: 6px 0 0; font-size: 14px; }}
            .badge, .count-chip, .status-pill {{ white-space: nowrap; border-radius: 999px; padding: 5px 9px; font-size: 12px; font-weight: 600; }}
            .badge {{ background: #eef2ff; color: #3730a3; }}
            .muted-badge, .muted-chip {{ background: #f3f4f6; color: var(--muted); }}
            .risk-badge {{ background: #fef3c7; color: #92400e; }}
            .count-chips {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }}
            .count-chip {{ background: #eef2ff; color: #3730a3; }}
            .risk-section {{ background: #fff7ed; border-color: #fed7aa; }}
            .risk-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
            .risk-card {{ background: white; border: 1px solid #fed7aa; border-radius: 14px; padding: 16px; }}
            .risk-card h3 {{ margin-top: 0; text-transform: none; letter-spacing: 0; font-size: 16px; color: #1f2937; }}
            .risk-card-main {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
            .risk-summary {{ margin: 8px 0; color: #4b5563; font-size: 14px; }}
            .risk-reasons {{ margin: 8px 0 0; padding-left: 18px; color: #92400e; font-size: 14px; }}
            .compact-item-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }}
            .item-row {{ margin: 0; padding: 12px; background: white; border: 1px solid #edf0f3; border-radius: 12px; }}
            .item-row-main {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
            .item-copy {{ min-width: 0; }}
            .item-title-line {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
            .item-meta {{ margin-top: 4px; color: var(--muted); font-size: 13px; }}
            .status-new {{ background: #eff6ff; color: #1d4ed8; }}
            .status-matched, .status-created {{ background: #ecfdf5; color: #047857; }}
            .status-possible-duplicate {{ background: #fef3c7; color: #92400e; }}
            .status-snoozed {{ background: #fef3c7; color: #92400e; }}
            .status-ignored {{ background: #f3f4f6; color: var(--muted); }}
            .inline-form {{ display: inline; margin: 0; }}
            .item-actions {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }}
            .track-form {{ display: inline-flex; gap: 6px; align-items: center; }}
            .linear-reference-input {{ border-radius: 999px; padding: 4px 8px; font-size: 12px; width: 150px; }}
            .ignore-button, .track-button, .snooze-button, .github-button {{ border: 1px solid #d1d5db; background: #fff; color: #4b5563; border-radius: 999px; padding: 4px 8px; font-size: 12px; cursor: pointer; }}
            .ignore-button:hover, .track-button:hover, .snooze-button:hover, .github-button:hover {{ background: #f3f4f6; color: #111827; }}
            .track-button {{ border-color: #bfdbfe; color: #1d4ed8; }}
            .github-button {{ border-color: #bbf7d0; color: #047857; }}
            .snooze-button {{ border-color: #fde68a; color: #92400e; }}
            .item-details {{ margin-top: 8px; }}
            .item-details summary {{ color: var(--muted); cursor: pointer; font-size: 13px; }}
            .details {{ margin-top: 6px; color: #374151; font-size: 14px; line-height: 1.45; }}
            .label {{ color: var(--muted); }}
            a {{ color: var(--blue); text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .empty {{ text-align: center; padding: 42px; }}
            @media (max-width: 1050px) {{ .dashboard-layout {{ grid-template-columns: 1fr; }} .dashboard-sidebar {{ position: static; }} }}
            @media (max-width: 800px) {{ header, main {{ padding-left: 18px; padding-right: 18px; }} .dashboard-search {{ grid-template-columns: 1fr; }} .risk-grid {{ grid-template-columns: 1fr; }} .thread-summary, .section-header, .risk-card-main, .item-row-main {{ flex-direction: column; }} .count-chips, .item-actions {{ justify-content: flex-start; }} }}
        </style>
    </head>
    <body>
        <header>
            <h1>Slack Linear Dashboard</h1>
            <p class="meta">Review Slack work signals, track what is handled, and expand cards only when you need details.</p>
        </header>
        <main>
            <div class="dashboard-layout">
                <aside class="dashboard-sidebar">
                    {render_scan_channel_form(scan_result)}
                    {scan_history_html}
                </aside>
                <section class="dashboard-content">
                    {notice_html}
                    {filter_tabs_html}
                    {search_html}
                    {risk_html}
                    {history_html}
                </section>
            </div>
        </main>
    </body>
    </html>
    """

def post_thread_analysis_preview(
    response_url,
    analysis,
    requester_slack_user_id="",
    source_url="",
    source_key="",
):
    proposed_issues = analysis.get("proposed_issues", []) or []
    preview_id = str(uuid.uuid4())

    if source_key and source_key in CREATED_THREAD_ISSUES_BY_SOURCE_KEY:
        existing_created_issues = CREATED_THREAD_ISSUES_BY_SOURCE_KEY[source_key]

        for proposed_issue in proposed_issues:
            if proposed_issue_has_existing_match(proposed_issue):
                continue

            match = find_existing_linear_issue_match(
                proposed_issue,
                existing_created_issues,
                source_url=source_url,
                source_key=source_key,
            )

            if match:
                mark_existing_issue_match(proposed_issue, match)

    record_thread_analysis_history(analysis, source_url, source_key)

    proposed_issues = analysis.get("proposed_issues", []) or []
    createable_issues = createable_proposed_issues(proposed_issues)
    message = format_thread_analysis_response(analysis, source_url)

    if createable_issues:
        PENDING_THREAD_PREVIEWS[preview_id] = {
            "proposed_issues": proposed_issues,
            "requester_slack_user_id": requester_slack_user_id,
            "source_url": source_url,
            "source_key": source_key,
            "created_at": time.time(),
        }
        logger.info(
            "Stored thread preview %s with %s proposed issues and %s createable issues",
            preview_id,
            len(proposed_issues),
            len(createable_issues),
        )

    payload = {
        "response_type": "ephemeral",
        "text": message,
    }

    if createable_issues:
        payload["blocks"] = build_thread_analysis_blocks(
            message,
            preview_id,
            len(createable_issues),
        )

    requests.post(response_url, json=payload, timeout=10)


def create_linear_issues_from_preview(preview):
    created_issues = []
    skipped_issues = []
    proposed_issues = preview.get("proposed_issues", []) or []
    requester_slack_user_id = preview.get("requester_slack_user_id", "")
    source_url = preview.get("source_url", "")
    source_key = preview.get("source_key", "")
    existing_issues = get_recent_linear_issues()

    if source_key:
        existing_issues.extend(CREATED_THREAD_ISSUES_BY_SOURCE_KEY.get(source_key, []))

    for proposed_issue in proposed_issues:
        if proposed_issue_has_existing_match(proposed_issue):
            skipped_issues.append(proposed_issue)
            continue

        match = find_existing_linear_issue_match(
            proposed_issue,
            existing_issues,
            source_url=source_url,
            source_key=source_key,
        )

        if match:
            mark_existing_issue_match(
                proposed_issue,
                match,
                existing_issue_match_type(match, source_key),
            )
            skipped_issues.append(proposed_issue)
            logger.info(
                "Skipped proposed issue '%s' because it matched existing Linear issue %s",
                proposed_issue.get("title", ""),
                match.get("identifier", ""),
            )
            continue

        description = append_source_context(
            proposed_issue.get("description", ""),
            source_url=source_url,
            source_key=source_key,
            evidence=proposed_issue.get("evidence", ""),
        )

        result = create_linear_issue(
            title=proposed_issue.get("title", ""),
            description=description,
            priority=proposed_issue.get("priority", "none"),
            assignee_name=proposed_issue.get("assignee_name", ""),
            due_date=proposed_issue.get("due_date", ""),
            requester_slack_user_id=requester_slack_user_id,
        )
        issue = result["data"]["issueCreate"]["issue"]
        created_issues.append(issue)
        mark_detected_item_created(proposed_issue.get("detected_item_id"), issue)

        existing_issue_record = {
            "id": issue.get("id", ""),
            "identifier": issue.get("identifier", ""),
            "title": issue.get("title", ""),
            "description": description,
            "url": issue.get("url", ""),
        }
        existing_issues.append(existing_issue_record)

        if source_key:
            CREATED_THREAD_ISSUES_BY_SOURCE_KEY.setdefault(source_key, []).append(
                existing_issue_record
            )

    return created_issues, skipped_issues



def format_created_thread_issues_response(created_issues, skipped_issues=None, source_url=""):
    skipped_issues = skipped_issues or []

    if not created_issues and not skipped_issues:
        return "No Linear issues were created."

    lines = []

    if created_issues:
        lines.append(f"Created {len(created_issues)} Linear issue(s) from this thread:")

        for issue in created_issues:
            lines.append(f"• {issue['identifier']}: {issue['title']}\n  {issue['url']}")

    if skipped_issues:
        if lines:
            lines.append("")

        lines.append("Skipped possible duplicate issue(s):")

        for proposed_issue in skipped_issues:
            existing_match = proposed_issue.get("existing_issue_match", "existing Linear issue")
            lines.append(f"• {proposed_issue.get('title', 'Untitled issue')}\n  Possible match: {existing_match}")

    if source_url:
        lines.extend(["", f"Source Slack thread:\n{source_url}"])

    return "\n".join(lines)



def process_create_thread_issues_and_respond(preview_id, response_url):
    try:
        preview = PENDING_THREAD_PREVIEWS.pop(preview_id, None)

        if not preview:
            logger.info("Ignoring create request for missing or already-used thread preview: %s", preview_id)
            return

        created_issues, skipped_issues = create_linear_issues_from_preview(preview)
        message = format_created_thread_issues_response(
            created_issues,
            skipped_issues,
            preview.get("source_url", ""),
        )

        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": message,
            },
            timeout=10,
        )

    except Exception:
        logger.exception("Error creating Linear issues from thread preview")

        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": "Couldn’t create Linear issues from this thread. Check the FastAPI terminal for details.",
            },
            timeout=10,
        )



def process_analyze_thread_and_respond(channel_id, thread_ts, response_url, requester_slack_user_id=""):
    try:
        messages = fetch_slack_thread(channel_id, thread_ts)
        analysis = analyze_thread_with_ai(messages)
        source_url = get_slack_thread_permalink(channel_id, thread_ts)
        source_key = build_slack_source_key(channel_id, thread_ts)
        analysis = annotate_existing_linear_matches(
            analysis,
            source_url=source_url,
            source_key=source_key,
        )
        post_thread_analysis_preview(
            response_url,
            analysis,
            requester_slack_user_id,
            source_url,
            source_key,
        )
    except Exception:
        logger.exception("Error processing Slack thread analysis")

        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": "Couldn’t analyze the Slack thread. Check the FastAPI terminal for details.",
            },
            timeout=10,
        )




@app.get("/")
def home():
    return {"message": "Slack Linear bot is running"}


@app.get("/dashboard")
def dashboard(request: Request):
    scan_result = request.query_params.get("scan_result", "")
    item_filter = request.query_params.get("filter", "all")
    search_query = request.query_params.get("q", "")
    github_status = request.query_params.get("github_status", "")
    github_identifier = request.query_params.get("github_identifier", "")
    github_url = request.query_params.get("github_url", "")
    return Response(
        content=render_dashboard_html(
            scan_result,
            item_filter,
            search_query,
            github_status=github_status,
            github_identifier=github_identifier,
            github_url=github_url,
        ),
        media_type="text/html",
    )


@app.post("/dashboard/scan-channel")
async def scan_channel_from_dashboard(request: Request):
    form = await request.form()
    channel_id = clean_text(form.get("channel_id", ""))
    lookback_hours = clean_text(form.get("lookback_hours", "24")) or "24"
    force_rescan = clean_text(form.get("force_rescan", "")) == "1"

    try:
        result = scan_slack_channel_for_threads(
            channel_id,
            lookback_hours,
            force_rescan=force_rescan,
        )
        message = format_channel_scan_result(result)
    except Exception as error:
        logger.exception("Dashboard channel scan failed")
        message = f"Channel scan failed: {error}"

    return Response(
        status_code=303,
        headers={"Location": f"/dashboard?scan_result={quote(message)}"},
    )


@app.post("/dashboard/items/{item_id}/ignore")
def ignore_dashboard_item(item_id: int, request: Request):
    ignore_related_detected_items(item_id)
    return Response(status_code=303, headers={"Location": dashboard_redirect_location(request)})


@app.post("/dashboard/items/{item_id}/mark-tracked")
async def mark_dashboard_item_tracked(item_id: int, request: Request):
    form = await request.form()
    linear_reference = clean_text(form.get("linear_reference", ""))
    mark_related_detected_items_tracked(item_id, linear_reference=linear_reference)
    return Response(status_code=303, headers={"Location": dashboard_redirect_location(request)})




@app.post("/dashboard/items/{item_id}/check-github")
def check_dashboard_item_github(item_id: int, request: Request):
    result = check_github_for_detected_item(item_id)
    with get_database_connection() as connection:
        item = connection.execute(
            "SELECT linear_identifier FROM detected_items WHERE id = ?",
            (item_id,),
        ).fetchone()
    linear_identifier = clean_text(item["linear_identifier"] if item else "")
    location = dashboard_redirect_location(
        request,
        {
            "github_status": result.get("status", ""),
            "github_identifier": linear_identifier,
            "github_url": result.get("url", ""),
        },
    )
    return Response(status_code=303, headers={"Location": location})


@app.post("/dashboard/items/{item_id}/snooze/{hours}")
def snooze_dashboard_item(item_id: int, hours: int, request: Request):
    snooze_related_detected_items(item_id, hours)
    return Response(status_code=303, headers={"Location": dashboard_redirect_location(request)})


@app.post("/slack/interactive")
async def slack_interactive(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    slack_signature = request.headers.get("X-Slack-Signature")

    if not verify_slack_request(body, timestamp, slack_signature):
        return {
            "response_type": "ephemeral",
            "text": "Request verification failed.",
        }

    form = await request.form()
    payload_raw = form.get("payload")

    if not payload_raw:
        return {
            "response_type": "ephemeral",
            "text": "Missing Slack interaction payload.",
        }

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        logger.exception("Could not parse Slack interaction payload")
        return {
            "response_type": "ephemeral",
            "text": "Couldn’t parse the Slack interaction payload.",
        }

    interaction_type = payload.get("type")
    callback_id = payload.get("callback_id")
    response_url = payload.get("response_url")

    if interaction_type == "block_actions":
        actions = payload.get("actions", [])
        action = actions[0] if actions else {}
        action_id = action.get("action_id")
        preview_id = action.get("value")

        if action_id == "cancel_thread_issues":
            PENDING_THREAD_PREVIEWS.pop(preview_id, None)

            if response_url:
                requests.post(
                    response_url,
                    json={
                        "response_type": "ephemeral",
                        "text": "Cancelled. No Linear issues were created.",
                    },
                    timeout=10,
                )

            return Response(status_code=200)

        if action_id == "create_thread_issues":
            if not preview_id or not response_url:
                logger.warning("Slack create action missing preview_id or response_url")
                return Response(status_code=200)

            background_tasks.add_task(
                process_create_thread_issues_and_respond,
                preview_id,
                response_url,
            )

            return Response(status_code=200)

        logger.info("Ignoring unsupported Slack button action: %s", action_id)
        return Response(status_code=200)

    if interaction_type != "message_action" or callback_id != "analyze_thread_for_linear":
        logger.info(
            "Ignoring unsupported Slack interaction: type=%s callback_id=%s",
            interaction_type,
            callback_id,
        )
        return Response(status_code=200)

    channel = payload.get("channel", {})
    message = payload.get("message", {})
    user = payload.get("user", {})

    channel_id = channel.get("id")
    thread_ts = message.get("thread_ts") or message.get("ts")
    requester_slack_user_id = user.get("id", "")

    if not channel_id or not thread_ts or not response_url:
        logger.warning(
            "Slack shortcut missing required context: channel_id=%s thread_ts=%s response_url_present=%s",
            bool(channel_id),
            bool(thread_ts),
            bool(response_url),
        )
        if response_url:
            requests.post(
                response_url,
                json={
                    "response_type": "ephemeral",
                    "text": "Couldn’t find the channel, thread, or response URL for this message.",
                },
                timeout=10,
            )

        return Response(status_code=200)

    background_tasks.add_task(
        process_analyze_thread_and_respond,
        channel_id,
        thread_ts,
        response_url,
        requester_slack_user_id,
    )

    return Response(status_code=200)


@app.post("/slack/command")
async def slack_command(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    slack_signature = request.headers.get("X-Slack-Signature")

    if not verify_slack_request(body, timestamp, slack_signature):
        return {
            "response_type": "ephemeral",
            "text": "Request verification failed.",
        }

    return {
        "response_type": "ephemeral",
        "text": (
            "Linear Bot now focuses on Slack thread analysis.\n\n"
            "Use the message shortcut instead:\n"
            "1. Open a Slack thread.\n"
            "2. Click the three-dot menu on the parent message.\n"
            "3. Choose `Analyze thread for Linear issues`.\n"
            "4. Review the preview, then click `Create new Linear issue(s)` or `Cancel`.\n\n"
            "The old single-message `/linear-task ai:` and pasted-text test modes have been removed."
        ),
    }
