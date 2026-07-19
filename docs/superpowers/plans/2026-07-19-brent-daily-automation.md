# Brent Daily Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update and enable the existing Brent daily-report automation so it runs every day at 08:00 Asia/Shanghai and sends a concise result notification without changing source code.

**Architecture:** Keep the existing standalone local project automation with id `brent`. Replace its stale run prompt with durable run-only instructions, use an explicit Asia/Shanghai daily schedule, preserve the current project/model/execution settings, enable all-run notifications, and verify the stored configuration after the update.

**Tech Stack:** Codex scheduled-task API, RFC 5545 recurrence with `Asia/Shanghai`, local Python entry point `run_jin10_tracker.py`.

## Global Constraints

- Run every calendar day at 08:00 in `Asia/Shanghai`, including weekends and holidays.
- Report window is previous-day 08:00:00 inclusive through current-day 08:00:00 exclusive.
- Update automation id `brent`; do not create a duplicate automation.
- Do not modify project source code during scheduled runs.
- Use the latest HTML renderer with the K-line chart above the blue detail panel; keep Markdown and Audit logic unchanged.
- Deliver a concise Scheduled result with desktop notification, HTML link, Markdown path, Audit paths, and log path.

---

### Task 1: Update and verify the existing automation

**Files:**
- Read: `/Users/xiaoxin/.codex/automations/brent/automation.toml`
- Reference: `docs/superpowers/specs/2026-07-19-brent-daily-automation-design.md`
- Modify through the scheduled-task API: automation id `brent`

**Interfaces:**
- Consumes: existing local project id, execution environment, model, and reasoning effort from automation `brent`.
- Produces: one active standalone local daily automation with all-run notifications and an explicit next run at 08:00 Asia/Shanghai.

- [ ] **Step 1: View the existing task before mutation**

Call the scheduled-task view operation for id `brent`. Confirm it is the paused Brent automation for project `/Users/xiaoxin/Documents/抓取数据` and record all fields that must be preserved.

- [ ] **Step 2: Update the existing task**

Call the scheduled-task update operation with id `brent`, status active, the same name/project/model/reasoning/execution environment, all-run notifications, and an explicit daily 08:00 `Asia/Shanghai` recurrence. Replace the prompt with run-only instructions that execute:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py run-once --db-path data/jin10_tracker.sqlite --output-dir outputs/tracker --log-dir logs --timezone Asia/Shanghai
```

The prompt must require the 08:00-to-08:00 window, current HTML template, unchanged Markdown/Audit logic, honest weekend degradation, existing retry behavior, no source-code edits, output validation, and a concise result containing the requested links and paths.

- [ ] **Step 3: Verify the stored task**

View automation `brent` again and confirm:

- status is active;
- it runs daily at 08:00 Asia/Shanghai;
- the next occurrence is the next local 08:00;
- notification policy sends all completed-run notifications;
- target project remains `/Users/xiaoxin/Documents/抓取数据`;
- the prompt explicitly forbids source-code changes;
- no second Brent automation was created.

- [ ] **Step 4: Report the outcome**

Return the task name, active status, Beijing schedule, next run, delivery behavior, local-runtime prerequisite, and any verification caveat. Do not expose the raw recurrence rule.
