# Capture queue validation

Validated 2026-10-01 in an isolated staging directory before publication. No environment file was read and no browser-capture artifact was used. Only the reviewed queue source, tests and documentation are published.

## Offline suite

26 tests passed in 6.69 seconds. Fixtures are a self-created tiny FFmpeg video and a self-authored mock reader.

Verified:
- A remains transcribing while producer hands off B and C in under 0.6 seconds
- B's reader failure does not prevent C completing
- Missing local file remains awaiting_transfer and has no ready receipt
- Repeated register/ready/drain does not transcribe a completed job twice
- Session mismatch, source SHA mismatch, queued byte mutation and receipt mutation fail safely
- Missing audio remains explicit
- Graceful interruption returns unfinished work to ready with a fresh later attempt
- Completed output interrupted before job-state commit is reconciled without duplicate ASR
- Atomic ready-receipt crash recovery works
- Stale worker and crash-leftover attempt recovery work without overwriting existing files
- Only one consumer acquires the queue
- Reader PID replaces the handshake wrapper; no separate ASR child is left by killing that wrapper
- A supervisor-killed, expired orphan is terminated only after PID/start-time, process-group and command-fingerprint checks; next valid job completes
- Malformed metadata/history and unsafe attempt paths are isolated, not allowed to stop valid later work
- Config-file extension and source-symlink input are rejected before reading input bytes

These tests prove finite queue behavior and enqueue/ASR separation. They do not prove a browser can record B continuously while A transcribes, multiple-tab capture, browser audio-track integrity, or source-program timestamp alignment.

## Real local Whisper smoke

Input: an existing 6.72-second original CC0 synthetic chart/voice video. This is an ordinary local test file, not a captured browser stream.

- Job: completed
- Queue elapsed: 4.699 seconds
- Reader elapsed: 4.434 seconds
- Reader peak RSS: 644.38 MB
- ASR status: ok
- Text: “The blue bar is the higher revenue result, revenue alone does not tell us which product is more profitable.”
- Second drain: handled zero jobs; attempt count remained 1

Only the existing local CPU/int8 Whisper reader/model was used. No new model, paid API or remote transcription service was involved. Metrics are this small run's process measurements, not throughput guarantees.

## Review-driven fixes

Independent review identified process-wrapper orphaning, malformed metadata and crash-leftover directory issues. They were fixed with exec-based PID continuity, bounded launch handshake, strict per-job metadata validation/quarantine, safe attempt numbering, no-follow file-descriptor validation and persisted orphan deadlines. Additional tests cover malformed history and completion exactly at interruption.

## Source and local validation records

- queue_reader.py: finite producer/consumer CLI and atomic state/receipt handling
- queue_worker.py: bounded launch handshake, then execs the unchanged reader
- tests/test_queue.py: offline/mock tests
The local validation also produced a final test log and measured real-model smoke report. Those execution records are not included in this source release.

Generated queue inputs and reader outputs remain under runs/ and are excluded from the source package. No daemon or test worker remains intentionally running.

## Limits

- Linux/POSIX implementation (fcntl locks, process groups, /proc process identity)
- One ASR consumer, default two CPU threads; no multi-tab capture implementation
- No browser download recovery or direct Blob access
- No remote ASR, .env loading, HTTP upload or raw-media publication
- Recording timestamps are retained as recording-origin, not invented original-program clocks
- An alive orphan is not duplicated; expired-orphan cleanup happens when recover/drain is called, not through a permanent watchdog
- Queue-owned copies consume additional disk; original files are unchanged
- SHA/receipts protect integrity and accidental source mixing, not a malicious owner able to rewrite every queue record

Final independent review confirmed every reported fix and reran the complete focused suite: **26 passed in 6.58 seconds**. No remaining blocker was found for this scoped finite local queue; no source edits or environment-file reads were performed by the reviewer.
