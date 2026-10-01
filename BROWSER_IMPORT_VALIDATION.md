# Browser importer validation — 2026-10-01

## Verified

- 81 Python tests passed, zero skipped: 38 new browser-import checks plus 43 existing reader/batch checks
- All existing single-file and batch integration artifacts were freshly regenerated in this staging checkout with the current CLI before the final suite; no old reports were copied in
- Local syntax/compilation and dependency consistency checks passed
- Node AudioWorklet unit test passed: gated start/stop, five-second split/tail, exact sample ranges and context frames, two separate processor instances, empty-input flag
- Node sampling diagnostics passed: zero frames, leading/trailing/interior gaps, nonmonotonic clocks, normal spacing and invalid windows

The Node tests use simulated buffers and timestamps, not browser playback.

## Actual local Whisper run on offline importer fixture

The offline generator decoded 12.000 seconds from our self-created CC0 synthetic chart/speech fixture, supplied 12 chart images and deliberately conflicting sidecar text, and explicitly marked the bundle as synthetic. The v0.2 bundle imported through the real `reader.py --browser-evidence` route.

Final run: 4.809 seconds processing; 646.88 MB peak reader RSS. The existing pinned small CPU/int8 model transcribed both English statements, including “orange” earns more profit, while the independent caption incorrectly says “blue”. Source estimates stayed within the fixture’s declared 0–12-second window. This establishes importer/ASR plumbing only, never browser-capture success, visual understanding or programme coverage.

## Fresh legacy regression measurements

- Four-file sequential preprocessing: 20.755 s; 613.52 MB sampled aggregate process-tree peak
- Same four files, two workers: 12.787 s; 1,258.29 MB sampled aggregate process-tree peak
- Exact resume: reused 4/4, 0.394 s
- Controlled ASR-file hash tamper: reprocessed 1, reused 3, 5.944 s
- Missing/corrupt inputs: both valid files completed and both invalid files failed independently
- Short watchdog test: worker recorded as timed out

These are local-file preprocessing tests, not parallel browser tabs. Batch source was unchanged. Performance varies with workload and hardware; process RSS is not total-machine memory.

## Independent review and fixes

Independent reviewer found and rechecked clock-alignment bounds: malicious constant offsets could previously map ASR outside the capture window. Fixed by bounding source anchors, requiring monotonic AudioContext clocks and multiple sufficient anchors, and withholding source alignment for any out-of-window group. Added positive/negative offset and sparse-clock regressions.

Also fixed mandatory per-record run/tab IDs, honest manifest variant/compatibility declaration, and bounded plain-JSON reads. ZIP members are never extracted. Only a fixed ordinary `bundle.json` entry is allowed. No .env access or remote requests are part of this code path.

## Not verified / release boundary

- Real browser capture: NOT RUN
- Browser evidence download/import end-to-end: NOT RUN
- Two/many browser tabs, background throttling, audio isolation and browser CPU/RAM: NOT RUN
- YouTube, cross-origin media, DRM, browser extension/tabCapture: NOT SUPPORTED/NOT VERIFIED
- Remote Whisper: NOT IMPLEMENTED/NOT CONTACTED; local model remains default
- The separately deployed private prototype exports v0.1. Local page source is v0.2 and un-deployed. The new CLI requires v0.2, so the deployed page and new importer are deliberately not reported as interoperable
- This release publishes the offline CLI importer only; the matching v0.2 browser page remains local and un-deployed

Only explicitly selected source, tests, documentation, and the blank/disabled environment example are included. No credentials, captured media, transcripts, or execution reports are published.

Final independent review: the reviewer inspected all remaining fixes and reran the focused importer suite: **38 passed in 0.48 s**. Mandatory identity, v0.2-only acceptance, explicit non-batch manifest and bounded plain-JSON input were confirmed. No source edits or protected environment-file access occurred during review. This remains offline verification only.
