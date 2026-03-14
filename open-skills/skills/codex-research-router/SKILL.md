---
name: codex-research-router
description: Route broad public-web research to Codex CLI and keep interactive browser work in ZeroClaw's native tools.
---

# Codex Research Router

Use this skill to decide whether a task should go to Codex research or stay inside ZeroClaw's own browser/tool loop.

## Decision policy

Prefer `$HOME/utils/zeroclaw/bin/codex-research` when the task is mainly about:
- researching, comparing, investigating, summarizing, or gathering links
- public web information, current docs, tutorials, pricing, changelogs, issues, news, or market scans
- multi-source synthesis where breadth matters more than clicking through a workflow
- fact-finding tasks that do not require interacting with a live web app

Prefer ZeroClaw's native `browser` tool when the task requires:
- clicking, typing, logging in, uploading, downloading, or submitting forms
- screenshots, PDFs, or visual verification
- JavaScript-triggered state changes or deterministic DOM interaction
- localhost, LAN, intranet, private dashboards, or authenticated sessions
- repeated page actions where a true browser is the main work, not the research summary

Use both when the task has two phases:
1. First research with Codex.
2. Then act with ZeroClaw browser/shell tools.

If the user explicitly asks to use Codex or explicitly asks to use the browser tool, obey that.

## How to call Codex research

Run this exact local command:

```bash
$HOME/utils/zeroclaw/bin/codex-research --reasoning medium -- "<research prompt>"
```

Prompt-writing rules for Codex research:
- State the desired output shape clearly.
- Ask for exact dates when recency matters.
- Ask for links when the user wants sources.
- Keep the prompt focused on public-web research, not local machine actions.

After Codex returns:
- If the Codex output already matches the user's requested format, pass it through verbatim.
- Do not append second guesses, alternate answers, or extra lines after a successful Codex result.
- Only override the Codex result if you intentionally do a second verification step and are confident the original result is wrong.
- If you do override it, replace the earlier result cleanly instead of appending both.
- If Codex research is insufficient or the task turns into a real browser workflow, switch to ZeroClaw's native browser tool.
