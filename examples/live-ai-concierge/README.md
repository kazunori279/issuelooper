# Example: Live AI Concierge

The project issuelooper was developed on: a FastAPI + vanilla JS voice and text
concierge with a scripted mode for tests and a live mode against a real model.
Copy these files into a project's `.issuelooper/` as a starting point.

| File | Goes to |
|---|---|
| `config.toml` | `.issuelooper/config.toml` |
| `project.md` | `.issuelooper/project.md` |
| `review_rules.md` | `.issuelooper/review_rules.md` |
| `monitor_kickoff.md` | `.issuelooper/monitor_kickoff.md` (the monitor's first message, with the seed backlog's priorities) |

`review_prompt_original.md` is the single review prompt the project used before
its app-specific rules were split out into `review_rules.md`.
