# Multi-agent development

This repository is developed by an issue loop of AI coding agents ([issuelooper](https://github.com/kazunori279/issuelooper)) that coordinate through GitHub issues, labels, pull requests, and signed comments.

- **Supervisor** (`.claude/hooks/ccpool.py`): deterministic program that polls GitHub, claims issues, starts agent sessions in isolated git worktrees, checks the LLM diff reviewer (`review_gate.py`) and the 8 merge gates (`role_guard.py`), runs the merge train, and deploys `main`.
- **Monitor**: one long-lived session that triages issues, sets priorities (`P0`–`P2`) and dependencies, manages holds, and routes product questions to the owner (`stage:owner`).
- **Aide**: interactive session(s) for the owner's research, debugging, and filing issues (`<!-- aide -->`).
- **Worker**: takes one claimed issue from a failing pytest test to a green sweep (and optional live `devcheck`) and opens a pull request.
- **Reviewer**: single model call (`review_gate.py`) that checks production diffs against built-in fabrication/test-gaming rules and `.issuelooper/review_rules.md`.
- **Merger**: resolves a pull request that conflicts with `main` so the supervisor can merge it.
- **Tester / Explorer**: runs post-merge acceptance checks on the deployed build, whole-app UX journeys, focused exploratory testing, and spec compliance checks.

Roles come from `CC_ROLE` (set by `.claude/hooks/ccrole.sh`), never from a document. `role_guard.py` enforces who may label, merge, and close, and requires every GitHub write to carry the role's HTML comment marker.

Start with the [shared skill](../.claude/skills/issuelooper-shared/SKILL.md), then [the project notes](../.issuelooper/project.md), then the skill for your role. For the full architecture and gate rules, see [How issuelooper Works](https://github.com/kazunori279/issuelooper/blob/main/docs/how-it-works.md).
