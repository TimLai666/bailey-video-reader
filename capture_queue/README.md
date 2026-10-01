# Finite local recording → ASR queue

Records may be captured independently, but **this CLI never controls a browser or starts recording**. It accepts only videos that have already landed as regular local files. It requires no new model or packages, never reads `.env`, and never contacts an external speech service.

The capture producer can enqueue B while the single ASR consumer processes A. It waits for durable local file handoff, **not A's transcription**. The default consumer processes ready work and exits; it is not a permanent daemon.

## Run

Use the existing reader's Python environment from this repository. From the repository root, enter `capture_queue/` as shown below and pass the root reader explicitly.

```
cd capture_queue
PYTHON=../.venv/bin/python
READER=../reader.py
$PYTHON queue_reader.py --queue ./runs/example-queue init

$PYTHON queue_reader.py --queue ./runs/example-queue register \
  --session 11111111-1111-4111-8111-111111111111 \
  --job-id 22222222-2222-4222-8222-222222222222 \
  --source-id 'recording-A'

$PYTHON queue_reader.py --queue ./runs/example-queue ready \
  --job 22222222-2222-4222-8222-222222222222 \
  --session 11111111-1111-4111-8111-111111111111 \
  --file ../fixtures/landed-recording.webm \
  --sha256 ACTUAL_SHA256_OF_LANDED_FILE

$PYTHON queue_reader.py --queue ./runs/example-queue drain \
  --reader "$READER" --threads 2 --language zh

$PYTHON queue_reader.py --queue ./runs/example-queue status
$PYTHON queue_reader.py --queue ./runs/example-queue recover
```

The UUIDs above are example identifiers, not shared IDs to reuse across independent recordings. Use one unique job ID per capture and its actual source session ID. Repeating register/ready for the same ID and same bytes is idempotent; changing that job's source/session/file identity is rejected. Completed jobs are not transcribed again.

Run producer commands in a separate process while `drain` is working. If `drain` finishes before the next recording lands, invoke it again after that handoff. `--max-jobs N` limits one invocation. There is deliberately no endless polling, live capture, scheduler or auto-start service.

## State and handoff

- `awaiting_transfer`: capture registered; no confirmed local artifact yet
- `ready`: copied, SHA-verified, FFprobe-readable local video and atomically committed ready receipt
- `transcribing`: one consumer owns an isolated attempt and its local reader process
- `completed`: exact input still matches and reader reports complete with verified output hashes
- `failed`: invalid input/metadata, changed identity/bytes, timeout or failed reader; other valid jobs continue

A native Chrome “download complete” label without a usable file is not ready. A browser Blob that cannot be retrieved is not accepted. No current un-retrieved browser recording was connected to this queue.

Readiness makes a queue-owned copy, hashes it against the required expected SHA, probes tracks/duration, fsyncs the bytes/directory and publishes `ready.json` by atomic rename. This prevents partially copied input from being consumed. The original file is unchanged. Files with missing audio retain `audio_status: missing_audio`; file decoding or ASR completion never implies sound existed or was understood.

The first version accepts local MP4/WebM/MKV/MOV/M4V/AVI, up to 2 GiB and two hours. Existing reader limits still apply, including its frame budget; queue admission does not promise a long programme will fit those processing limits.

## Recovery and isolation

- A per-queue consumer lock permits one ASR worker. Short state locks let the producer submit later jobs while ASR runs
- Each attempt has a new directory. Crash leftovers are preserved rather than overwritten
- A bounded launch handshake commits process identity before ASR starts. The wrapper then `exec`s the reader, retaining its PID; killing a wrapper cannot leave a separate ASR child alive
- Graceful interruption terminates that process group and reconciles output. Valid already-completed output is certified once; otherwise the job returns to ready for a fresh attempt
- After supervisor failure, `recover`/next `drain` checks the recorded PID/start-time. A live orphan prevents duplicate ASR. On a persisted deadline, it may terminate only a matching process group and exact launched command fingerprint, then mark that timed-out job failed so others can continue
- A dead worker with valid completed output is reconciled without rerunning; otherwise the interrupted attempt is retryable
- Malformed queue metadata is reported as failed and isolated. Input/attempt path redirection and symlink media are rejected
- Source bytes, ready receipt and source/session identity are verified before ASR and again before certification

This is crash recovery and accidental-mixing protection, not a security boundary against a malicious owner who can rewrite all queue files. Filesystem fsync/rename semantics depend on the storage platform. Worker processes inherit the selected local execution environment; use a trusted reader. Queue data is never uploaded or published.

## Timestamp meaning

A retrieved tab-recorded WebM's timestamps begin at the recording. They are **not automatically the original programme's clock**. The ready receipt explicitly preserves recording-origin timebase. Programme-source alignment requires separately established playback anchors; this queue does not invent them.

This MVP consumes landed local video files, not browser evidence JSON bundles. The separately developed browser-bundle importer remains a distinct path.

## Tests

```
../.venv/bin/python -m pytest -q tests
python -m py_compile queue_reader.py queue_worker.py
```

The unit/integration suite creates an original tiny FFmpeg test video and uses a self-authored mock reader. It proves A can remain transcribing while B/C are handed off, B failure does not block C, missing files never become ready, identities/hashes are enforced, duplicates do not rerun, interrupts and orphan deadlines recover, and crash leftovers are preserved. These are not real-browser capture or multi-tab tests.

A separate real local-Whisper smoke test used a 6.72-second original CC0 synthetic video. See `VALIDATION.md`. This source release contains no recordings or generated queue state and starts no permanent worker.
