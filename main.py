import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date
from difflib import SequenceMatcher

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
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


def parse_task_with_ai(raw_text):
    today = date.today().isoformat()

    schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "A short Linear issue title.",
            },
            "description": {
                "type": "string",
                "description": "A clear Linear issue description with useful context.",
            },
            "priority": {
                "type": "string",
                "enum": ["none", "low", "medium", "high", "urgent"],
                "description": "The task priority inferred from the message.",
            },
            "assignee_name": {
                "type": "string",
                "description": "The person's name if an assignee is mentioned. Use 'me' if the user assigns the task to themselves.",
            },
            "due_date": {
                "type": "string",
                "description": "Due date in YYYY-MM-DD format if mentioned, otherwise an empty string.",
            },
        },
        "required": [
            "title",
            "description",
            "priority",
            "assignee_name",
            "due_date",
        ],
        "additionalProperties": False,
    }

    response = client.responses.create(
        model="gpt-4.1-nano",
        input=[
            {
                "role": "system",
                "content": (
                    "Extract a clean Linear issue from the user's Slack message. "
                    "Do not invent facts. "
                    f"If a due date is relative, infer it using today's date: {today}. "
                    "If the user says they need to do something, must do something, will do something, "
                    "or uses first-person language like 'I,' 'me,' 'my,' or 'myself,' set assignee_name to 'me'. "
                    "If the user names another person, set assignee_name to that person's name. "
                    "Return an empty string for assignee_name only if no assignee is implied. "
                    "Return an empty string for due_date if no due date is provided."
                ),
            },
            {
                "role": "user",
                "content": raw_text,
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "linear_task",
                "schema": schema,
                "strict": True,
            }
        },
    )

    parsed_task = json.loads(response.output_text)
    logger.info("AI parsed task: %s", parsed_task)
    return parsed_task


def apply_first_person_fallback(raw_text, assignee_name):
    if assignee_name:
        return assignee_name

    raw_text_lower = raw_text.lower()
    first_person_phrases = [
        "i need to",
        "i should",
        "i will",
        "i'll",
        "assign this to me",
        "assign to me",
        "me to",
        "my task",
    ]

    if any(phrase in raw_text_lower for phrase in first_person_phrases):
        return "me"

    return assignee_name


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


def format_preview_response(parsed_task, slack_user_id):
    title = parsed_task["title"]
    description = parsed_task["description"]
    priority = parsed_task["priority"]
    assignee_name = parsed_task["assignee_name"]
    due_date = parsed_task["due_date"]

    assignee = resolve_assignee(assignee_name, slack_user_id)
    assignee_display = display_linear_user(assignee)

    lines = [
        "Preview only. No Linear issue was created.",
        f"Title: {title}",
        f"Description: {description or 'none'}",
        f"Priority: {priority}",
    ]

    if assignee_name:
        if assignee_display:
            lines.append(f"Assignee: {assignee_display}")
        elif normalize_name(assignee_name) in ["i", "me", "myself"]:
            lines.append("Assignee: couldn’t match your Slack email to a Linear user")
        else:
            lines.append(f"Assignee: {assignee_name} could not be matched")
    else:
        lines.append("Assignee: none")

    lines.append(f"Due date: {due_date or 'none'}")
    return "\n".join(lines)


def process_ai_task_and_respond(raw_text, response_url, slack_user_id, preview=False):
    try:
        parsed_task = parse_task_with_ai(raw_text)
        parsed_task["assignee_name"] = apply_first_person_fallback(
            raw_text,
            parsed_task["assignee_name"],
        )

        if preview:
            message = format_preview_response(parsed_task, slack_user_id)
        else:
            result = create_linear_issue(
                title=parsed_task["title"],
                description=parsed_task["description"],
                priority=parsed_task["priority"],
                assignee_name=parsed_task["assignee_name"],
                due_date=parsed_task["due_date"],
                requester_slack_user_id=slack_user_id,
            )

            issue = result["data"]["issueCreate"]["issue"]
            message = format_issue_response(
                issue=issue,
                priority=parsed_task["priority"],
                assignee_name=parsed_task["assignee_name"],
                due_date=parsed_task["due_date"],
            )

        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": message,
            },
            timeout=10,
        )

    except Exception as error:
        logger.exception("Error processing AI Linear issue")

        requests.post(
            response_url,
            json={
                "response_type": "ephemeral",
                "text": "Couldn’t process the AI-parsed Linear issue. Check the FastAPI terminal for details.",
            },
            timeout=10,
        )


def parse_manual_task(command_text):
    if "|" in command_text:
        title, description = command_text.split("|", 1)
        return title.strip(), description.strip()

    return command_text.strip(), ""


@app.get("/")
def home():
    return {"message": "Slack Linear bot is running"}


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

    form = await request.form()
    command_text = form.get("text", "").strip()
    response_url = form.get("response_url")
    slack_user_id = form.get("user_id")

    if not command_text:
        return {
            "response_type": "ephemeral",
            "text": "Please include a task. Example: `/linear-task Fix login bug`",
        }

    command_lower = command_text.lower()

    if command_lower in ["help", "--help", "-h"]:
        return {
            "response_type": "ephemeral",
            "text": (
                "Linear Bot examples:\n"
                "• `/linear-task Fix login bug`\n"
                "• `/linear-task Fix login bug | Login redirects after OAuth.`\n"
                "• `/linear-task ai: I need to fix the login redirect bug by Friday and make it urgent`\n"
                "• `/linear-task preview ai: I need to fix the login redirect bug by Friday and make it urgent`\n\n"
                "Use `ai:` to create a Linear issue from natural language.\n"
                "Use `preview ai:` to see what would be created without creating an issue."
            ),
        }

    if command_lower.startswith("preview ai:"):
        raw_text = command_text[len("preview ai:"):].strip()

        if not raw_text:
            return {
                "response_type": "ephemeral",
                "text": "Please include a task after `preview ai:`.",
            }

        background_tasks.add_task(
            process_ai_task_and_respond,
            raw_text,
            response_url,
            slack_user_id,
            True,
        )

        return {
            "response_type": "ephemeral",
            "text": "Working on the AI preview...",
        }

    if command_lower.startswith("ai:"):
        raw_text = command_text[3:].strip()

        if not raw_text:
            return {
                "response_type": "ephemeral",
                "text": "Please include a task after `ai:`.",
            }

        background_tasks.add_task(
            process_ai_task_and_respond,
            raw_text,
            response_url,
            slack_user_id,
            False,
        )

        return {
            "response_type": "ephemeral",
            "text": "Working on the AI-parsed Linear issue...",
        }

    title, description = parse_manual_task(command_text)

    if not title:
        return {
            "response_type": "ephemeral",
            "text": "Please include a task title before the `|`.",
        }

    try:
        priority = "none"
        result = create_linear_issue(
            title=title,
            description=description,
            priority=priority,
        )

        issue = result["data"]["issueCreate"]["issue"]

        return {
            "response_type": "ephemeral",
            "text": format_issue_response(issue, priority),
        }

    except Exception:
        logger.exception("Error creating manual Linear issue")

        return {
            "response_type": "ephemeral",
            "text": "Couldn’t create the Linear issue. Check the FastAPI terminal for details.",
        }
