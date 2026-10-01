# Explicitly skip local ASR

Base: public main `e6838dc01f14e668ec007bda56fa330564e02d90`, from the separately verified frozen source snapshot. This review is local; nothing has been published.

## Change

`reader.py --skip-asr` accepts landed local media, preserves sampled frames and the selected full original audio stream, imports supplied/embedded text, and records that local ASR was not performed. It does not read or load a model. The default continues to run local Whisper even when captions are supplied.

The skipped result uses top-level `complete_without_asr`, ASR status `not_performed_user_skipped`, empty ASR segments and model-file metadata, and zero ASR execution time. Text availability is reported separately; no supplied text produces `no_transcript`. Original source bytes are unchanged and their SHA-256 remains in the manifest. A sidecar's original bytes, including BOM and CRLF, are retained exactly.

`--skip-asr` is rejected with browser-bundle input. Batch and queue CLIs reject the unsupported flag. Their existing completion checks reject `complete_without_asr`, so no batch/queue cache logic was changed. All queue and browser-import source files remain unchanged.

Changed product files: `reader.py`, `README.md`. Added regression tests: `tests/test_skip_asr.py`. This document is the fourth release/review file.

## Verification

- Eleven focused tests passed: no ASR imports or model dependency; exact sidecar bytes; actual frames and original AAC retention; no-transcript status; audio-only and no-audio inputs; default/legacy force behavior; unsupported mode rejection; batch-cache exclusion; remux failure remains a failure
- Every legacy media integration artifact was generated afresh using the changed reader. All 92 tests in `tests/` passed with zero skips, including browser-bundle tests
- The unchanged capture-queue suite passed all 26 tests; a final combined run is recorded in `reports/final-skip-asr-tests.txt`
- A true local Whisper run using frozen e6838dc and a true run using the changed reader produced identical default caption, frame and alignment JSON. Their ASR JSON matched after excluding only the original-audio Matroska container hash, which varies per remux. See `reports/default-equivalence.json`
- Python compilation passed for the changed reader and new tests
- An independent code review passed before release preparation

The first broad run lacked the source-only snapshot's external model/fixture setup. After using the already existing local model/fixture directories, the regression artifacts were regenerated and all corresponding assertions ran. No packages or models were downloaded.

## Manual transcript example

The existing 13.062012-second synthetic video and the SRT manually reconstructed from a transcription editor were processed with `--skip-asr`:

- 13 frames and 10 supplied-sidecar segments
- 107,391-byte original AAC stream remux retained
- Zero ASR segments, zero ASR execution time, no model files used
- 0.474 seconds for this one small example; not a general throughput benchmark
- Runtime guards recorded zero attempted ASR imports, model-file opens or Python network operations. FFmpeg/FFprobe retained their existing `file,pipe` protocol restriction

Proof: `reports/manual-transcript-skip-proof.json`; detailed evidence: `reports/manual-transcript-skip/`. The external transcript remains supplied text, with unknown model provenance; it is not local ASR, original subtitles or proof of direct listening. This test does not establish browser recording/download transfer, multiple-tab capture, or a complete programme review.

## Re-run

In a checkout with the existing trusted Python environment and local model/fixtures prepared:

```sh
.venv/bin/python -m pytest -q tests/test_skip_asr.py
.venv/bin/python -m pytest -q tests capture_queue/tests
```

On a fresh output tree, `tests/run_media_regression.py` regenerates the legacy media integration outputs before running `tests/`. It is not necessary for a user merely selecting `--skip-asr`; that mode does not require a Whisper model.

The code only handles an already available local recording. The assistant's manual website operation and obtaining the recording remain separate steps. No external service adapter, browser automation, credentials, or network feature was added.
