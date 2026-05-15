# Slack Linear Bot

A Slack app that turns Slack messages and threads into Linear issues.

It supports simple slash-command issue creation, AI-parsed natural language tasks, and Slack thread analysis with a safe preview → confirm → create flow.

## What it does

- Creates Linear issues from Slack using `/linear-task`
- Supports title-only tasks
- Supports title + description using `|`
- Uses AI to extract title, description, priority, assignee, and due date from natural language
- Maps Slack users to Linear users by email for “I,” “me,” and “myself” assignments
- Supports preview mode so you can check an AI-parsed task before creating an issue
- Analyzes pasted conversations for action items, unresolved questions, and proposed Linear issues
- Analyzes real Slack threads through a message shortcut
- Lets users confirm before creating Linear issues from a Slack thread
- Adds Slack source links, evidence, and source keys to created Linear issue descriptions
- Prevents duplicate Linear issue creation from the same Slack thread

## Current product flow

The main workflow is:

```text
Slack thread
→ Analyze thread for Linear issues
→ AI extracts action items, owners, due dates, unresolved questions, and proposed issues
→ User reviews preview
→ User clicks Create
→ Linear issues are created with Slack source context
```

This is the core differentiated feature. The basic `/linear-task ai:` command is useful, but the thread analysis flow is the more important product direction.

## Example slash commands

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
/linear-task analyze-text ai: Evan: The login redirect bug is blocking launch. I can fix it by Friday.
Sarah: I’ll test OAuth edge cases after that.
Alex: Do we need this fixed on mobile too?
```

```text
/linear-task analyze-thread CHANNEL_ID THREAD_TS
```

```text
/linear-task help
```

## Slack message shortcut

The preferred thread workflow uses a Slack message shortcut.

Create a message shortcut in the Slack app dashboard:

```text
Name: Analyze thread for Linear issues
Callback ID: analyze_thread_for_linear
Type: Message shortcut
```

Request URL:

```text
https://your-ngrok-url.ngrok-free.dev/slack/interactive
```

Usage:

1. Open a Slack thread.
2. Click the three-dot menu on the parent message.
3. Choose **Analyze thread for Linear issues**.
4. Review the preview.
5. Click **Create new Linear issue(s)** or **Cancel**.

Created Linear issues include:

- Slack source thread link
- Evidence from the Slack conversation
- A stable source key used for duplicate prevention

## Environment variables

Create a `.env` file in the same folder as `main.py`:

```text
SLACK_SIGNING_SECRET=
SLACK_BOT_TOKEN=
LINEAR_API_KEY=
LINEAR_TEAM_ID=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-nano
LOG_LEVEL=INFO
```

Do not commit `.env`.

## Slack setup

Your Slack app needs:

- Slash command: `/linear-task`
- Slash command request URL: your ngrok or deployed URL ending in `/slack/command`
- Interactivity request URL: your ngrok or deployed URL ending in `/slack/interactive`
- Message shortcut callback ID: `analyze_thread_for_linear`

Bot token scopes:

```text
commands
users:read
users:read.email
channels:history
groups:history
```

Optional if you want to support DMs and group DMs later:

```text
im:history
mpim:history
```

After changing scopes, reinstall the Slack app to your workspace.

For private channels, invite the bot into the channel:

```text
/invite @Linear Bot
```

## Linear setup

You need:

- A Linear workspace
- A Linear team ID
- A Linear API key with read/write access

The bot creates issues in the team specified by `LINEAR_TEAM_ID`.

## Run locally

```bash
cd ~/slack-linear-bot
source .venv/bin/activate
python -m uvicorn main:app --reload
```

In another terminal:

```bash
ngrok http 8000
```

Use the ngrok HTTPS URL in Slack settings:

```text
https://your-ngrok-url.ngrok-free.dev/slack/command
https://your-ngrok-url.ngrok-free.dev/slack/interactive
```

## Git safety

Your `.gitignore` should include:

```text
.env
.venv
__pycache__
.DS_Store
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

```text
/linear-task analyze-text ai: Evan: The login redirect bug is blocking launch. I can fix it by Friday.
Sarah: I’ll test OAuth edge cases after that.
Alex: Do we need this fixed on mobile too?
```

Should show a thread-style analysis preview and create no Linear issue.

For the message shortcut:

1. Create a Slack thread with test messages.
2. Run **Analyze thread for Linear issues** from the message shortcut.
3. Confirm the preview appears.
4. Click **Create new Linear issue(s)**.
5. Confirm Linear issues are created with source link and evidence.
6. Analyze the same thread again and click create.
7. Confirm duplicate issues are skipped.

## Current limitations

- Pending previews are stored in memory, so they disappear if the server restarts.
- Duplicate prevention works best for issues created after source-key support was added.
- The app is still local-first and uses ngrok for Slack testing.
- No database yet.
- No GitHub execution tracking yet.

## Product direction

The long-term goal is not just “create Linear issues from Slack.” Existing tools already do basic Slack-to-Linear workflows.

The differentiated direction is:

```text
team conversation
→ detected action items, decisions, blockers, and unresolved questions
→ proposed tracked work
→ Linear issue creation or matching
→ later GitHub execution tracking
→ alerts when work falls through the cracks
```

The next major feature should be existing-work detection and execution tracking across Linear and GitHub.