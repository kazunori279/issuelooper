# Multi-agent development

This repository is developed by an issue loop of agent sessions
([issuelooper](https://github.com/kazunori279/issuelooper)) that coordinate only
through GitHub issues, labels, pull requests and signed comments.

- **Supervisor** (`.claude/hooks/ccpool.py`): reads GitHub every few seconds,
  claims issues, starts sessions, merges mergeable pull requests once the review
  gate (`review_gate.py`) and the merge gates (`role_guard.py`) pass, and deploys
  when the project has a deploy command.
- **Worker**: one claimed issue, from a failing test to a pull request.
- **Merger**: lands a pull request that conflicts with `main`.
- **Tester**: live checks against the live environment, UX runs over the
  deployed app that file what gets in a user's way, and spec checks when the
  workers have nothing to do (all optional).
- **Monitor**: triage, priorities, holds, dependencies and the owner's decisions.

Roles come from `CC_ROLE` (set by `.claude/hooks/ccrole.sh`), never from a
document. `role_guard.py` enforces who may label, merge and close, and that every
GitHub write is signed with the role's marker.

Start with the [shared skill](../.claude/skills/issuelooper-shared/SKILL.md),
then [the project notes](../.issuelooper/project.md), then the skill for your role.
