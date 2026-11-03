# ProjectFlow time tracking skill

Records how long Claude Code and Codex work on each prompt, per repository. Entries upload to a
[ProjectFlow](https://getprojectflow.com) project or stay local and export as CSV for a later import.
No prompt contents are stored; only start, end, a one-line activity summary and token counts.

Requirements: Python 3 (standard library only), Claude Code and/or Codex with hooks.

## Install

**Claude Code plugin**

```
/plugin marketplace add ChristophLabestin/projectflow-time-tracking
/plugin install projectflow-time-tracking@projectflow
```

**Skill folder (Claude Code or Codex)** — download `projectflow-time-tracking.zip` from the
[latest release](https://github.com/ChristophLabestin/projectflow-time-tracking/releases/latest) and unzip it into
`~/.claude/skills/` (Claude Code) or `~/.codex/skills/` (Codex):

```sh
curl -fsSL -o /tmp/pf-skill.zip https://github.com/ChristophLabestin/projectflow-time-tracking/releases/latest/download/projectflow-time-tracking.zip
unzip -o /tmp/pf-skill.zip -d ~/.claude/skills/
```

## Use

In the repository you want to track, ask the agent: “Set up ProjectFlow time tracking”. It links the repository to a
ProjectFlow project, or records locally only. Tracking starts with the next prompt.

Local-only commands (`<skill>` is the unzipped folder):

```sh
python3 <skill>/scripts/agent_time.py link --offline --cwd .
python3 <skill>/scripts/agent_time.py install --client claude   # or codex
python3 <skill>/scripts/agent_time.py export --out ~/Downloads
```

Import the CSV in ProjectFlow: project → timer icon in the header → “Import past times”.

This repository is published from the ProjectFlow monorepo; changes made here are overwritten by the next release.
