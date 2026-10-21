# Samples

Two samples, each a starting app, its issuelooper configuration and a seed backlog.
One command turns a sample into a GitHub repository of your own, ready to run:

```bash
examples/create.sh <sample> <you>/<new-repo> [--dir DIR] [--public]
```

It copies the sample's app into a new directory, creates the GitHub repository
(private unless `--public`) and pushes, installs issuelooper with its labels, puts
the sample's configuration in `.issuelooper/`, prepares the app, and files the seed
backlog as issues with their priorities and dependencies.

| Sample | What it builds | Time | Needs |
|---|---|---|---|
| [`hello-world`](hello-world/) | `textstats`, a small Python CLI: 6 issues (two bugs, four features, one dependency) | 15-30 minutes | the [requirements](../README.md#requirements) |
| [`live-ai-concierge`](live-ai-concierge/) | A voice and text concierge on Google ADK and the Gemini Live API, from a first slice to the full spec: 54 issues, plus what the testers' UX runs and spec checks find | about 6 hours | also a Google Cloud project with Vertex AI |

Start with `hello-world` to see the loop work: the workers, the reviewer, the merges
and the monitor. `live-ai-concierge` is the app issuelooper was developed and
benchmarked on; it shows what the loop does unattended over hours, with the testers
making the next work.

Other files here:

- `create.sh`: the command above.
- `seed_issues.py`: files a backlog file as issues (`create.sh` runs it; run it again
  to resume an interrupted filing).

When you are done with a sample, stop the loop (`.claude/hooks/issuelooper stop --all`)
and delete the repository if you like (`gh repo delete <you>/<new-repo>`).
