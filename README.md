# Saki ZeroClaw

<p align="center">
  <img src="assets/saki-zeroclaw-minecraft.jpg" alt="Saki ZeroClaw Minecraft-style project art" width="360">
</p>

A personalized ZeroClaw distribution for `saki`, focused on QQ delivery, background agent workflows, remote rendering, and everyday research / file / media tasks.

This repository is based on a local forked snapshot of upstream ZeroClaw `v0.1.7`, as recorded in [source/Cargo.toml](source/Cargo.toml).

## Upstream Credit

This project builds on the excellent upstream ZeroClaw project by [zeroclaw-labs](https://github.com/zeroclaw-labs):

- Upstream repository: [zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw)
- Upstream docs / README: [upstream README](https://github.com/zeroclaw-labs/zeroclaw/blob/main/README.md)

If this fork is useful, please also star or follow the upstream project. The core runtime architecture, upstream codebase inside `source/`, and the original project direction all come from ZeroClaw.

## What This Fork Adds

This tree keeps the upstream `source/` codebase and layers extra workflow tooling around it. The main custom additions currently include:

- QQ-oriented operations improvements
  - richer attachment / callback-oriented helper scripts
  - operational `/status` and `/restart` style workflows
  - progress / background-task friendly delivery patterns
- Codex orchestration helpers
  - background Codex jobs in `tmux`
  - completion callbacks back into Saki / QQ
  - a Codex research bridge for broad public-web investigation
- Remote Manim workflow
  - queue Manim work onto a stronger Linux machine
  - fetch rendered video back and deliver it through the chat flow
- Video pipeline
  - helper scripts for fetching source videos
  - Gemini-based video understanding helpers
- Office file support
  - extract / inspect `docx`, `xlsx`, `ods`, `csv`, `tsv` and related files
- Research digest automation
  - scheduled daily paper collection and QQ push helpers
- Runtime quality-of-life changes in the ZeroClaw fork itself
  - Codex / GPT backend tuning
  - per-message reasoning override via `/reasoning:<level> ...`
  - additional channel / tool integration work tracked in `source/`

## Repository Layout

- `source/` — upstream-style ZeroClaw Rust codebase, plus fork modifications
- `bin/` — local wrapper scripts and operator-facing helpers
- `codex-tmux/` — background Codex job manager and QQ callback helpers
- `manim-remote/` — remote Manim rendering workflow
- `video-fetch/` — video acquisition helpers
- `video-understanding/` — video understanding helpers
- `office-files/` — office document extraction helpers
- `research-digest/` — daily paper digest generation / push helpers
- `open-skills/` — local skill files used by the customized deployment

## Privacy / Publishing Notes

This public repo is intended to exclude machine-local persona and deployment state. In particular, it should not publish:

- `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
- local secrets, auth tokens, or service env files
- downloaded media, local build outputs, or compiled release binaries
- personal runtime state under local workspace directories

The root `.gitignore` is set up with that publishing model in mind.

## Current Deployment Style

In the private live deployment, Saki is typically run with:

- a GPT / Codex-compatible backend
- QQ as the primary mobile-facing channel
- Codex delegated in the background for heavier research / coding tasks
- remote execution for heavier rendering jobs such as Manim

This repository intentionally keeps those integrations in code, while leaving machine-specific secrets and persona files out of version control.
