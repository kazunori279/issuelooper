# agy stream-json protocol (probed on agy 1.3.0)

broker.py drives the monitor this way; agent_cli.py holds the agy-specific parts.

## Launch

```
agy --input-format stream-json --output-format stream-json [--model M] \
    --dangerously-skip-permissions [--conversation ID]    # cwd = the shared checkout
```

## Input (stdin, one JSON object per line)

```json
{"event": "user", "message": {"content": "<text>"}}
```

- `event` is required, and `user` is the only supported value. Unknown events (`interrupt`, `cancel`, `stop`, `abort`) are ignored with a warning on stderr.
- `message` must be an object with `content`. A plain string fails to decode and ends the session with an ERROR result.
- **There is no interrupt.** A message sent during a turn is queued and runs as the next turn after the current one ends.
- Closing stdin ends the session after the current turn.

## Output (stdout)

- `{"event":"init", "conversation_id":…, "init":{cwd, tools, permission_mode}}` is emitted once, after the first input.
- `{"event":"step_update","step_update":{conversation_id, step_index, state: ACTIVE|DONE|ERROR, step_type, …}}`
  - `step_type` is one of `user_input`, `agent_response` (`text_delta`, `usage`), `tool` (`tool_name`, `tool_info{parameters, output|error}`), `subagent` (`subagent_info`), `system_message`.
  - Each step reports ACTIVE and then DONE or ERROR. `duration_seconds` and `usage` come with DONE.
- `{"event":"result","result":{conversation_id, status: SUCCESS|ERROR, response, duration_seconds, num_turns, usage, error?}}` is emitted **exactly once per user message**, in order, and marks the end of the turn.
- Context persists across turns within the session.
