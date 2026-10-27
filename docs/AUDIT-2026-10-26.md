# Technical audit and focused fixes — 2026-10-26

Baseline: `62075c25ca967146980196ca180f5252d41eb295`.

This pass traced the server/agent wiring, shared state, tool execution and approvals, chat persistence, uploads, filesystem operations, model configuration/downloads, streaming, and desktop/UI boundaries. All 83 existing Python test modules were attempted individually and compared with an untouched baseline checkout. This is a broad audit with focused fixes, not a claim that every line or every integration is correct. The application targets Windows; this review ran on macOS without its GPU models, Roblox Studio, or Windows automation APIs.

## Fixed

| Priority | Files / functions | Problem and change |
| --- | --- | --- |
| High | `fs_tools.py:unzip_file` | ZIP entries could follow an existing destination symlink and overwrite files outside the extraction directory. Validate resolved paths before extracting anything. This addresses existing symlinks/junctions; it does not provide an OS sandbox against a separate process changing links during extraction. |
| High | `composer_tools.py:FileStore.save/get` | An attachment called `meta.json` was overwritten by the store's own metadata, including case variants on Windows. New payloads use a separate internal filename; display names, old uploads, and temporary-chat adoption remain supported. |
| High | `chat_store.py:save/rename/set_flags/set_project` | Metadata edits loaded a chat before acquiring the write lock. A concurrent reply save could then be overwritten by the old transcript. Lock each complete read/modify/write transaction with a reentrant lock, including flag preservation. These are in-process locks, not cross-process database transactions. |
| High | `history.py:recent_turns`, `chat_store.py:save` | Saving the last 300 individual messages could orphan tool responses from their calls. Persist whole recent user turns using the existing synthetic-message exclusions. The newest turn is retained whole even when it exceeds the soft message cap; existing context fitting remains responsible for model token limits. Already-corrupted historical files are not reconstructed. |
| High | `llama_local.py:serialized_ini/update_model_config/set_model_options`, `model_scan.py:scan/add_file/_append/ensure_ini` | Settings, scans and downloads could race while editing the same model file; direct writes could leave it truncated. Share one reentrant lock across catalog readers and writers, and publish edits by atomic replacement. Concurrent duplicate adds are refused instead of creating duplicate sections. Existing backup behavior is retained. |
| High | `model_finder.py:download/_fetch/_matches_hash` | Different owners or subfolders shared download destinations, existing same-size files bypassed SHA-256 validation, and resumed responses were appended without checking their byte range. Namespace destinations by repository/subfolder, verify existing files when a hash is published, and validate Content-Range before appending. Cancellation is checked before file reuse and during hashing. Existing configured models keep their paths; re-downloading into the new namespace can use additional disk space. |
| High | `llm_stream.py:http_transport` | The existing real-socket regression showed Stop taking 30 seconds while waiting for the first token. Closing the client from another thread did not interrupt the read. Shut down the established response socket before closing the client, and treat a cancelled EOF as cancellation. The stalled-response test now passes; connection establishment or a server that has not sent response headers still depends on the HTTP client's connection/read behavior. |
| Medium | `stream_hub.py:StreamHub.publish/since/forget/merge` | After 6,000 events, each publication shifted a list and every incremental poll scanned the full buffer. Use a bounded deque and scan only the unseen tail. Join delta fragments once per group instead of repeatedly copying growing strings, and never merge deltas across temporary-chat boundaries. |
| Medium | `fixtures/review/system_messages_ui.test.js` | The UI parse check assumed CRLF and one relevant script block, so a normal LF checkout failed before testing the application. Parse the actual inline script blocks independently. |

## Validation and evidence

- Added `test_audit_regressions.py`: **14 tests pass**. Twelve fail against the original code; two cover supported resume/buffer behavior that must remain working. Tests use temporary stores, controlled interleavings, and fake HTTP responses.
- Final focused chat/history run: **69 tests pass**, including the 14 new regressions, projects, message editing, reviewed turn flows, temporary chats and run history.
- Socket-enabled stream/network run: **30 tests pass**. Before the transport fix, the same run had one failure: Stop took 30.09 seconds rather than less than five. The final entire 30-test run took 3.65 seconds.
- UI fixtures: **77 Node tests pass**, including voice lifecycle, widgets, context/effort controls, system messages and globe behavior.
- Broad comparison: 83 existing modules plus the then-current 12-test audit module were attempted separately with 25-second timeouts. **1,348 tests completed**, with 13 failures, 16 errors and 19 skips; `test_roblox` timed out. Every existing failing/error test and the timeout matched the untouched checkout. This broad comparison preceded the final history, socket-cancellation and UI-test fixes; the affected suites were rerun afterward as listed above.
- Baseline problems include Windows path/API assumptions, missing image/video model fixtures, blocked local sockets/process enumeration in the sandbox, the old UI parser assumption, and the Roblox timeout. They are not all classified as product defects. A combined unsandboxed run importing the Windows approval UI also aborted in macOS Tk; the successful socket run excluded those GUI imports.
- `git diff --check` and Python compilation checks passed.
- Local microbenchmark: 10,000 publish + last-event reads with a full 6,000-event buffer took **0.9566 s before / 0.0149 s after**. This measures the buffer operation on this machine, not end-to-end application performance.

Useful commands from the repository root (activate the Python environment first):

```sh
python -m unittest test_audit_regressions test_projects test_messages test_review_turn_fixes test_temp_chat test_run_history
python -m unittest test_streaming test_netlayer
node --test fixtures/review/*.test.js
```

The network tests require loopback sockets. Set `LITELLM_LOCAL_MODEL_COST_MAP=True` when offline so dependency imports use their bundled pricing data. On the supported Windows target, run the complete suite plus live desktop, model loading, voice and Roblox smoke tests before release.

## Remaining issues and risky changes deferred

| Priority | Evidence | Why it remains / recommended next change |
| --- | --- | --- |
| High | `api/server.py:set_local_model_config/force_load_model`, `api/extras.py:_with_model`, `model_finder.py:_run` | Model lifecycle exclusion uses separate locks and check-then-act `state_lock.locked()` checks. For example, a download can decide to restart the router immediately before another request starts using it; extras do not share the force-load lock. Introduce one reservation for router changes/model usage across chat, Compare, downloads and diagnostics. A partial lock change can deadlock nested calls or make long model loads block chat navigation; validate the unified lifecycle on the real Windows/local-model setup. |
| High | `tool_executor.py:ToolExecutor._run_once/execute` | An isolated worker survives a timeout/cancellation. Retrying read-only calls can overlap old workers, and late work may observe changed global state. Python cannot safely kill these threads. Propagate cooperative cancellation and per-run context through network/browser/media calls, and bound outstanding workers; do not claim cancellation proves an external action did not happen. This needs integration-level changes, not a cosmetic timeout adjustment. |
| Medium | `model_finder.py:files/download` | The Hugging Face tree is read as one page, with no pagination or complete-shard validation. Large repos can yield incomplete split-model groups. Implement pagination and validate shard counts against mocked multi-page responses before enabling downloads; the current tests do not establish live Hub behavior. |
| Medium | `index.html`, `api/server.py`, `core/state.py`, `agent/tasks.py` | The interface is about 19,700 lines; the API module about 3,400. Feature initialization, global mutable state and import-time GUI wiring make tests order-sensitive and complicate concurrency ownership. Extract feature routers/controllers and explicit startup/shutdown incrementally, rather than rewriting the app during a correctness patch. |
| Medium | `test_*.py`, `fixtures/review` | Many tests depend on Windows paths, installed assets, GUI startup or mutable module globals. The monolithic baseline run stalled; isolated runs expose the same existing failures predictably. Add Windows CI and separate pure unit tests from desktop/GPU integration tests with explicit fixture requirements and timeouts. |

No production accounts were connected, no models were downloaded, and no changes were pushed to GitHub. The changes remain in the local checkout for review.
