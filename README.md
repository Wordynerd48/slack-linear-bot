# Slack Linear Bot

A Slack app that analyzes Slack threads and turns explicit action items into Linear issues.

The current product flow is focused on Slack thread analysis, not single-message slash commands.

## What it does

- Analyzes real Slack threads through a Slack message shortcut
- Extracts summaries, action items, unresolved questions, blockers, and decisions
- Creates proposed Linear issues only from explicit action items
- Keeps unresolved questions separate from Linear issues
- Infers assignees from speaker ownership, such as “I can fix it” or “I’ll test it”
- Infers due dates from relative dates, such as “by Wednesday”
- Shows a preview before creating Linear issues
- Lets the user choose **Create new Linear issue(s)** or **Cancel**
- Adds Slack source links, evidence, and source keys to created Linear issue descriptions
- Prevents duplicate Linear issues from the same Slack thread
- Avoids matching test/QA tasks as duplicates of fix/implementation tasks

## Current product flow

```text
Slack thread
→ Analyze thread for Linear issues
→ AI extracts action items and unresolved questions
→ Code creates proposed Linear issues from action items only
→ User reviews preview
→ User clicks Create
→ Linear issues are created with Slack source context
→ Re-analyzing the same thread shows already-tracked work
```

This is the main MVP loop.

## Key product rule

The AI does not directly generate proposed Linear issues.

Instead:

```text
AI extracts action_items
→ app converts action_items into proposed Linear issues
```

This keeps the output safer and prevents unanswered questions from becoming tickets.

Example:

```text
Evan: The checkout page loses the selected shipping method after refresh. I can fix it by Wednesday.
Sarah: I’ll test shipping method persistence after Evan’s fix.
Alex: Should we also test guest checkout separately?
```

Expected behavior:

```text
Action items:
1. Fix checkout page shipping method persistence
2. Test shipping method persistence after Evan’s fix

Unresolved questions:
1. Should we also test guest checkout separately?

Proposed Linear issues:
1. Fix checkout page shipping method persistence
2. Test shipping method persistence after Evan’s fix
```

No Linear issue should be created for Alex’s question unless someone explicitly commits to doing that work.

## Slack message shortcut

The preferred workflow uses a Slack message shortcut.

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

## Linear issue output

Created Linear issues include:

```text
Source Slack thread:
<Slack thread permalink>

Slack source key:
slack-thread:<channel_id>:<thread_ts>

Evidence:
<supporting Slack message>
```

The source key is used for duplicate prevention.

## Duplicate prevention

The bot checks existing Linear issues before creating new ones.

Current duplicate rules:

- Issues from the same Slack thread use a stable source key.
- Same-thread duplicates require strict title/task similarity.
- Test/QA tasks should not match fix/implementation tasks.
- Broad semantic matching is avoided for issues from the same Slack thread.
- Re-analyzing a thread after creation should show the issues as already tracked and hide the create button.

Example:

```text
Fix checkout page shipping method persistence
```

should not match:

```text
Test shipping method persistence after Evan’s fix
```

Those are related, but they are different tasks.

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

- Interactivity request URL ending in `/slack/interactive`
- Message shortcut callback ID: `analyze_thread_for_linear`

Bot token scopes:

```text
commands
users:read
users:read.email
channels:history
groups:history
```

Optional for future DM support:

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

The bot creates issues in the team specified by:

```text
LINEAR_TEAM_ID
```

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
https://your-ngrok-url.ngrok-free.dev/slack/interactive
```

## Quick test plan

Create a fresh Slack thread.

Parent message:

```text
Evan: The checkout page loses the selected shipping method after refresh. I can fix it by Wednesday.
```

Thread reply:

```text
Sarah: I’ll test shipping method persistence after Evan’s fix.
```

Thread reply:

```text
Alex: Should we also test guest checkout separately?
```

Run **Analyze thread for Linear issues** from the parent message.

Expected preview:

```text
Action items
1. fix the checkout page issue with shipping method persistence
2. test shipping method persistence after Evan’s fix

Unresolved questions
1. Should we also test guest checkout separately?

Proposed Linear issues
1. fix the checkout page issue with shipping method persistence
2. test shipping method persistence after Evan’s fix
```

Click **Create new Linear issue(s)**.

Expected result:

```text
Created 2 Linear issue(s)
```

Run **Analyze thread for Linear issues** again on the same parent message.

Expected result:

```text
Both proposed issues are matched to existing Linear issues.
No create button is shown.
```

## Slash command behavior

The old single-message `/linear-task ai:` and pasted-text test modes have been removed.

The `/linear-task` endpoint still exists, but it only tells users to use the Slack message shortcut.

## Git safety

Your `.gitignore` should include:

```text
.env
.venv
__pycache__
.DS_Store
.claude/
```

## Current limitations

- Pending previews are stored in memory, so they disappear if the server restarts.
- The session-created issue cache is also in memory.
- Persistent duplicate prevention depends on the Slack source key saved in Linear issue descriptions.
- The app is still local-first and uses ngrok for Slack testing.
- No database yet.
- No channel-wide scanning yet.
- No GitHub execution tracking yet.

## Product direction

The near-term product is not just “create Linear issues from Slack.” The stronger direction is:

```text
team conversation
→ detected action items and unresolved questions
→ proposed tracked work
→ Linear issue creation or matching
→ later GitHub execution tracking
→ alerts when work falls through the cracks
```

Before expanding to GitHub or channel-wide scanning, keep the Slack-thread-to-Linear flow stable.