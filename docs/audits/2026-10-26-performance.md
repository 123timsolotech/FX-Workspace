# Local inference regression and follow-up audit

Source baseline: `62075c25ca967146980196ca180f5252d41eb295` (1.4.0), compared with `v1.3.0` (`753a98b`). This is a source patch for 1.4.0, released as 1.4.1 after review (see `../releases/1.4.1.md`). See `2026-10-26-code-audit.md` for the first audit pass. The findings below supersede its lifecycle and worker-retry entries where specified.

## Why speed could appear or become slower

1. **High: live speed counted messages, not tokens.** `index.html:onStreamEvent/modelActivity` incremented the counter once per SSE event. `stream_hub.merge` combines multiple text deltas into one event. The displayed “tok/s” therefore measured delivery batching. Live output now counts Unicode characters and explicitly displays **char/s**. It is not comparable with model tokens/s.
2. **High: generation statistics included unrelated waiting.** `agent/sessions.py:metrics_of` treated helper calls without a first-token timestamp as decoding for their entire duration. `model_metrics.py` now prefers llama-server's `predicted_n/predicted_ms`; otherwise it uses streamed completion usage and the measured output interval. Calls without usable generation timing do not dilute the rate. Stream timing remains an approximation affected by transport buffering; server timing is preferred. Reasoning tokens are included when the server includes them in usage.
3. **High: 1.4 removed the effort-level context cap.** `_fit_for_local` and the context meter now apply the smaller of the model's available capacity and the selected effort profile's history budget again. An 80K model no longer accumulates 80K of history for a Quick request whose budget is 6K. This reduces unnecessary prompt work on long chats. It does not change the model's configured KV-cache allocation or GPU offload settings. Existing context compression remains available; older history may be summarized or trimmed.
   *Not shipped in 1.4.1:* the owner chose to keep 1.4.0's behaviour (history limited only by the model's context set in Settings › Models), so this cap was reverted before release.
4. **Medium: repeated GPU subprocess work.** `system_metrics.begin_model_load/finish_model_load` no longer resample GPU memory for every request when the same resident model already has an estimate.
5. **High: Stop could wait 30 seconds on a stalled response.** The previous pass fixed interruption of an established response socket. This delay concerns cancellation; it is not evidence that normal model decoding pauses for 30 seconds.

The version comparison did not show changes to llama-server build selection defaults, launch parameters or model presets between these revisions. No real GPU/model benchmark was possible here, so restoration from 0.3–3 to 20 tokens/s is **not verified**. Compare the same model, quantization, GPU layers, context configuration and prompt in a new chat and a long chat. Compare completed server-derived token rates separately from first-output latency and the live character rate. If a fresh chat's server-derived decode rate remains low, inspect actual GPU offload and memory use before attributing it to the UI.

## Other fixes in this follow-up

- **High — resource settings:** `api/server.py:set_local_model_config` preserves omitted options. Updating context alone no longer implicitly resets GPU-layer, batch or thread overrides. Explicit null still clears an override.
- **High — inference/lifecycle races:** `local_runtime.py` provides a shared reentrant reservation across local completions, model loads, unloads and build switches. Router restart refuses while another thread holds the reservation. Resource-triggered restart also reserves turn state. This protects active calls; it does not reserve the GPU across an entire multi-step workflow or across separate application processes. Real Windows/GPU stress testing remains needed.
- **High — timed-out tools:** `tool_executor.py` no longer automatically retries a read-only worker that is still running after its deadline. Completed transient failures retain their retry policy. Python threads still cannot be forcibly stopped; cooperative cancellation and bounding outstanding workers remain future work. A timeout does not establish that an external action did not happen.
- **High — knowledge boundaries:** `knowledge.py` now applies the existing protected-file rules during indexing and search, filters legacy indexed protected entries from results, and blocks symlinks escaping the selected root. `tools/files.py` additionally protects the app's mail configuration. Old protected content remains physically in an existing database until reindexing removes those entries; delete/rebuild the index if physical removal is required.
- **Medium — incremental indexing:** detect subsecond same-size edits, deduplicate overlapping roots, remove entries that become oversized, preserve an existing index when scanning is cancelled, and report actual completed counts. Folder filters escape SQL wildcards and require a directory boundary.
- **Medium — helper streaming:** local text helpers use the cancellable streaming transport even when their text is not displayed live. Replay fixtures route these calls to their separate helper queue; helpers do not pollute the main agent recording.

## Packaging

The source ZIP has one `FX-Workspace/` root and retains the repository layout, installers, assets, fixtures, licenses and meaningful automated tests. It excludes `.git`, `.venv`, caches, downloaded engines/models, runtime data and machine-specific settings/secrets. No tracked `test1.py` or equivalent numbered scratch script was present. Meaningful tests are retained so the published repository remains verifiable. Extract the ZIP and publish the contents of its root folder. It is source, not a bundled Windows executable or an installation containing your local models/settings.

## Remaining limitations

The first audit's Hugging Face pagination/split-shard completeness issue, large-module structure and platform-dependent integration tests remain. They were not broadly rewritten in this patch. Windows desktop/voice/Roblox workflows and real model performance require testing on the target machine. This audit does not establish that every issue in the application is fixed.

## Final validation

- Broad isolated run: 86 Python modules, 1,392 completed tests, 12 failures, 16 errors, 19 skips. No new failing/error test names compared with the original checkout; the UI parser failure was fixed and Roblox completed on this run. Remaining failures are detailed in the first audit's environment/platform notes. This is not a fully green Windows release certification.
- Final focused run: 158 tests, successful with two skips, covering new regressions, indexing, extras, replay/history and orchestration. Includes 31 new regression cases across the three added files and orchestration retry coverage.
- Real loopback stream/network run: all 30 tests pass in 3.70 seconds.
- JavaScript UI fixtures: all 77 tests pass.
- Source diff whitespace check passed. ZIP integrity, source manifest, fresh-extraction compilation and focused tests are checked during packaging.
