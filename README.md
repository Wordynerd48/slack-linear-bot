# Slack Linear Bot

A Slack slash command that creates Linear issues from either structured text or natural language.

## What it does

- Creates Linear issues from Slack using `/linear-task`
- Supports title-only tasks
- Supports title + description using `|`
- Uses AI to extract title, description, priority, assignee, and due date from natural language
- Maps Slack users to Linear users by email for “I,” “me,” and “myself” assignments
- Supports preview mode so you can check an AI-parsed task before creating an issue

## Example commands

```text
/linear-task Fix login bug
```

```text
/linear-task Fix login bug | Login redirects after OAuth.
```

```text
/linear-task ai: I need to fix the login redirect bug by Friday and make it urgent
```

```text
/linear-task ai: Sarah should fix the dashboard loading bug by Friday
```

```text
/linear-task preview ai: I need to fix the login redirect bug by Friday and make it urgent
```

```text
/linear-task help
```

## Environment variables

Create a `.env` file in the same folder as `main.py`:

```text
SLACK_SIGNING_SECRET=
SLACK_BOT_TOKEN=
LINEAR_API_KEY=
LINEAR_TEAM_ID=
OPENAI_API_KEY=
LOG_LEVEL=INFO
```

Do not commit `.env`.

## Slack setup

Your Slack app needs:

- Slash command: `/linear-task`
- Request URL: your ngrok or deployed URL ending in `/slack/command`
- Bot token scopes:
  - `users:read`
  - `users:read.email`

After changing scopes, reinstall the Slack app to your workspace.

## Linear setup

You need:

- A Linear workspace
- A Linear team ID
- A Linear API key with read/write access

The bot creates issues in the team specified by `LINEAR_TEAM_ID`.

## Run locally

```bash
cd ~/robotics-summer/opencv-practice/slack-linear-bot
source .venv/bin/activate
python -m uvicorn main:app --reload
```

In another terminal:

```bash
ngrok http 8000
```

Use the ngrok HTTPS URL in your Slack slash command settings:

```text
https://your-ngrok-url.ngrok-free.dev/slack/command
```

## Git safety

Your `.gitignore` should include:

```text
.env
.venv
__pycache__
```

## Quick test plan

```text
/linear-task help
```

Should show help text and create no Linear issue.

```text
/linear-task Fix login bug
```

Should create a title-only Linear issue.

```text
/linear-task Fix login bug | Login redirects after OAuth.
```

Should create a Linear issue with a description.

```text
/linear-task preview ai: I need to fix the login redirect bug by Friday and make it urgent
```

Should show a preview and create no Linear issue.

```text
/linear-task ai: I need to fix the login redirect bug by Friday and make it urgent
```

Should create a Linear issue with priority, due date, and assignee if your Slack email matches your Linear email.
