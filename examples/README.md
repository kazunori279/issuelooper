# Samples

Each sample includes a starting application, a complete `.issuelooper/` configuration, and a seed backlog. One command turns a sample into a fresh GitHub repository under your account, ready to run:

```bash
examples/create.sh <sample> <you>/<new-repo> [--dir DIR] [--public]
```

`create.sh` copies the sample's app into a new directory, creates the GitHub repository (private unless `--public` is passed) and pushes `main`, runs `install.sh` to install the hooks and GitHub labels, copies the sample's `.issuelooper/` configuration, runs `after_create.sh` to set up the environment, and files the seed backlog as issues with their priorities and dependencies.

| Sample | What it builds | Typical runtime | Requirements |
|---|---|---|---|
| [`hello-world`](hello-world/) | `textstats`, a small Python CLI: 6 issues (2 bugs, 4 features, 1 dependency) | 15–30 minutes | [Base requirements](../README.md#requirements) |
| [`live-ai-concierge`](live-ai-concierge/) | A voice and text concierge built with Google ADK and the Gemini Live API, from a first slice to the full spec: 54 seed issues, plus findings from UX runs, explorers, and spec checks | ~6 hours | Base requirements + a Google Cloud project with Vertex AI |

Start with [`hello-world`](hello-world/) to watch the full loop on a small codebase: parallel workers, the failing-test gate, the LLM diff reviewer, automatic conflict resolution, and the monitor. Move to [`live-ai-concierge`](live-ai-concierge/) to see how `issuelooper` runs unattended over several hours with live `devcheck`, post-merge `"after"` acceptance, area conversations, and testers generating the next wave of issues.

## Helper scripts in this directory

- `create.sh`: creates and seeds a sample repository.
- `seed_issues.py`: files a markdown backlog file as GitHub issues (`create.sh` calls it automatically; re-run it directly to resume an interrupted filing).

When you are finished with a sample, stop the loop (`.claude/hooks/issuelooper stop --all`) and delete the GitHub repository if you no longer need it (`gh repo delete <you>/<new-repo>`).
