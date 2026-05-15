import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import date, timedelta
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
PENDING_THREAD_PREVIEWS = {}
CREATED_THREAD_ISSUES_BY_SOURCE_KEY = {}


@app.on_event("startup")
def startup_check():
    require_env_vars()
    logger.info("Slack Linear bot started")


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


def is_trackable_action_item(action_item):
    task = normalize_name(action_item.get("task", ""))
    evidence = clean_text(action_item.get("evidence", ""))

    if not task:
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
    return str(value or "").strip()


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

    for item in cleaned["action_items"]:
        item["due_date"] = clean_due_date(
            item.get("due_date", ""),
            item.get("evidence", ""),
            today_date,
        )

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



def post_thread_analysis_preview(
    response_url,
    analysis,
    requester_slack_user_id="",
    source_url="",
    source_key="",
):
    message = format_thread_analysis_response(analysis, source_url)
    proposed_issues = analysis.get("proposed_issues", []) or []
    createable_issues = createable_proposed_issues(proposed_issues)
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
