# AGENTS.md

## Project

This is a Slack app with a FastAPI backend. It turns Slack messages into Linear issues.

Current flow:
- Slack slash command receives `/linear-task ...`
- FastAPI verifies Slack request signatures
- OpenAI parses natural language into structured task fields
- Linear issue is created with title, description, priority, due date, and assignee
- Slack receives a confirmation
- Preview mode shows parsed output without creating an issue

Current commands:
- `/linear-task help`
- `/linear-task Fix login bug`
- `/linear-task Fix login bug | Login redirects after OAuth.`
- `/linear-task preview ai: I need to fix the login bug by Friday and make it urgent`
- `/linear-task ai: I need to fix the login bug by Friday and make it urgent`

## Product direction

Do not optimize only for “create Linear issue from Slack.” That already exists.

The differentiated direction is:
- Analyze Slack threads
- Detect action items, decisions, blockers, owners, and deadlines
- Compare detected work against existing Linear issues
- Preview proposed issues with evidence
- Create or link Linear issues
- Later connect GitHub activity to Linear execution state

The core product loop is:
communication → structured intent → tracked work → execution state → outcome

## Coding style

Keep the code simple and readable. This is an MVP.

Prefer:
- small helper functions
- clear names
- structured JSON outputs
- defensive error handling
- no hardcoded secrets
- no broad refactors unless requested

Avoid:
- committing `.env`
- logging API keys or tokens
- adding a database unless the task explicitly asks for persistence
- adding deployment configs unless requested
- making a big dashboard yet

## Required environment variables

These live in `.env`, which must never be committed:

```text
SLACK_SIGNING_SECRET=
SLACK_BOT_TOKEN=
LINEAR_API_KEY=
LINEAR_TEAM_ID=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-nano
LOG_LEVEL=INFO