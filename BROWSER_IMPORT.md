# Browser evidence input: offline importer, browser workflow unverified

## Current status

The CLI has a new explicit browser-evidence input path. It imports exported image/PCM/caption evidence and runs the existing local CPU Whisper model. It does not operate a browser, download a website video, or prove that an exported bundle was genuinely captured.

The separately deployed private browser prototype uses **old v0.1** exports. Actual browser capture remains untested. The **local un-deployed** page source now emits **v0.2**, with mandatory per-record run/tab identity. This importer accepts v0.2 only. The deployed v0.1 page must be updated before an end-to-end browser → CLI test; it is not currently interoperable with this importer. Publishing this CLI does not deploy or expose the private browser prototype, install an extension, or establish a browser login.

## Commands

```
.venv/bin/python reader.py evidence.json --browser-evidence -o new-output --language zh
.venv/bin/python reader.py evidence.zip --browser-evidence -o new-output --skip-browser-asr
```

The optional ZIP may contain exactly one ordinary file named `bundle.json`. No paths are extracted. Traversal paths, links, devices, extra entries, encrypted members and nonstandard compression are rejected. JSON and decoded evidence are bounded to 32 MiB; images, arrays, audio sample counts and timestamps are bounded. Only JPEG frames and signed 16-bit little-endian mono PCM are accepted.

`--skip-browser-asr` is explicit structural validation, never successful speech transcription. Empty audio produces `missing_audio`; no frames produces a whole-window visual gap. Missing chunk/sample/context ranges split audio into separate WAV groups rather than joining across missing material or inventing silence.

## Bundle v0.2 contract

- `schema`: `browser-playback-evidence/0.2`
- `run_id`, `tab_instance`: UUID strings
- `source.sha256`: source-media hash **claim**, not authenticated acquisition proof
- `settings`: requested start, duration (bounded to 45 s including safety margin), frame interval
- `sample_rate`: supported integer PCM rate
- `frames[]`: matching `run_id` and `tab_instance`, `media_time`, JPEG base64
- `audio_chunks[]`: matching identities, monotonic `seq`, `sample_start`, `sample_end`, `context_frame_end`, PCM base64, optional matching sample rate
- `clock[]`: matching identities, monotonic wall/AudioContext times, in-window source time, paused/ready-state/rate fields
- `source.captions.cues[]`: independent timestamped text, never fed into ASR
- `summary.source_end`: observed end claim; client gap counts are not trusted and are recomputed

Source mapping uses retained playback/AudioContext observations. It needs at least two plausible anchors spanning the captured audio. Unstable offsets, backward clocks or mapped groups outside the capture window prevent source alignment. Speech can still be transcribed into an explicitly unmapped audio-time stream; the importer never emits those words as source-aligned evidence. This is clock-based estimated alignment, not forced alignment.

## Outputs and compatibility

The entry point is the existing `reader.py` CLI, using `--browser-evidence`. `frames.json`, `asr.json`, `captions.json` and `alignment.json` follow the reader’s evidence layout; additional `browser_capture.json` keeps raw timing/event claims and recomputed gaps. `manifest.json` uses the honest variant schema `bailey-browser-evidence-import/1` and status `imported_evidence`, with modality and completeness explicit.

This is **not** supported by the media-only batch runner or its `complete`/resume cache contract. Browser recordings are not automatically inserted into the old batch queue. Each output directory must be new or empty, and session IDs are required on every media/clock record. A rewritten bundle and its claims are not cryptographically authenticated; this protects against accidental mixing, not a malicious forger.

## ASR backend boundary

`asr_backends.py` exposes a small `transcribe(wav_path, output_directory, duration)` adapter. Only `local` is implemented and selectable. The existing pinned model is verified and no new model/API is used. Remote ASR is not implemented or contacted. Reserved `.env.example` values are blank and disabled; current code does not load them. Real environment files and credentials are excluded from this repository.

Future remote work needs a redacted, verified service schema, explicit destination/transmission scope, secret-safe configuration, and separate tests. Never copy authentication values or remote authenticated URLs into manifests, logs, reports, source or command arguments.

## Reproducible offline tests

```
.venv/bin/python -m pytest -q tests/test_browser_import.py
.venv/bin/python tests/make_browser_bundle_fixture.py --output reports/browser-fixture.json
.venv/bin/python reader.py reports/browser-fixture.json --browser-evidence -o reports/browser-import-new --language en --threads 2
```

The generator decodes our own CC0 synthetic speech/chart fixture offline. Its clocks are simulated and `synthetic_fixture=true`. It is never called browser-capture proof. It preserves the deliberately incorrect independent caption so the imported ASR and chart evidence can reveal disagreement.

On a fresh staging checkout with the existing local fixtures/models present, `tests/run_media_regression.py` rebuilds all legacy integration outputs, runs one/two-worker file preprocessing, failure isolation, timeout, resume and controlled tamper repair, then runs the full tests. It does not run browser tabs. Existing outputs are intentionally not overwritten, so use a fresh output tree before rerunning this setup.

## Remaining acceptance gates

1. Deploy the matching v0.2 page source while preserving private access
2. Securely sign into the private Site in this same cloud browser through a supported login flow
3. Actually capture 10–30 seconds, export through a normal browser download and import it
4. Compare captured audio/frame timestamps with source playback and inspect actual frames/transcript
5. Only then test two isolated tabs and measure frame loss, audio separation, background throttling and browser resource use

Cross-origin YouTube, tabCapture/extension execution, many tabs, live incremental ASR and complete-program understanding remain unverified or unimplemented.
