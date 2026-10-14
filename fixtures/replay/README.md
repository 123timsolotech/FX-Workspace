# Replay fixtures

Each `*.json` file here is one agent run driven by recorded (or faithfully
reconstructed) model replies. `test_replay.py` and `test_effort.py` replay them
through the real pipeline with `replay_harness.run_fixture()` - no model and no
llama-server are involved, and nothing outside a temporary sandbox is touched
(file changes, the undo trail and model capabilities all live in the sandbox).

## Format (`agentic-replay/1`)

| Key | Meaning |
|---|---|
| `meta.provenance` | `recorded` (captured by `recorder.py` from a live run) or `reconstructed` (rebuilt from evidence) |
| `meta.evidence` | What the fixture is based on. Required. Reconstructed fixtures say which parts are inferred. |
| `meta.model` | The model the evidence came from, or `unrecorded` when that is not known |
| `setup` | `mode` (local/cloud), `entry` (`agent` = full orchestrator, `loop` = agent loop only), `settings`, `turn_policy`, optional `stop_at_response`, `settings_at_response`, `model_caps`, `props` |
| `request` | The user's message |
| `responses` | Model replies for agent steps, in order: `finish_reason`, `content`, `reasoning_content`, `tool_calls` (`id`, `name`, raw `arguments` string exactly as sent), `usage`; or `raise` (a server error); or explicit SSE `chunks`; `chunk_size` sets how finely a reply is fragmented |
| `aux_responses` | Replies for tool-free calls (planner, reviewer, final summary) |
| `approvals` | The user's answers to approval cards, in order |
| `tools` | `stub` (scripted `results` per tool) or `real` (tools really run, inside the sandbox) |
| `expect` | Checked by `replay_harness.check_expectations` |

`{sandbox}` / `{sandbox_json}` are replaced by the run's temporary folder (the
`_json` form is backslash-escaped for use inside a JSON-encoded argument string).

## Adding a fixture from a real run

1. Turn on `record_model_io` (Settings), reproduce the problem with a local model.
2. Copy the file from `data/recordings/` here, replace machine paths with `{sandbox_json}`,
   add `tools.results` for the tools it called and an `expect` block.
3. Keep `provenance: recorded`. Never edit recorded replies to show behaviour the model did not produce.
