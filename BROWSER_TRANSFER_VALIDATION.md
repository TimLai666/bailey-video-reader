# Browser transfer validation: one 60-second recording

Observed 2026-10-01. Evaluated CLI: [4720f63](https://github.com/TimLai666/bailey-video-reader/commit/4720f6337bdcee7958e63b399db35298b1e2899a). This update changes documentation only. [Aggregate measurements](validation/browser-transfer-2026-10-01.json) contain the hashes and test outcomes.

## Recording to local evidence

One approved foreground tab and its tab audio were recorded with MediaRecorder and transferred through same-origin HTTPS. The 2,090,513-byte WebM has SHA-256 `b21746373a26092660ae35f8440b74690e14d3b1d5669628e08ff07d704f44ed`. The unchanged published CLI processed its 60.0-second media timeline in 2.977 seconds with `--skip-asr --interval 5 --max-frames 20`, producing 12 sampled frames and retained Opus audio.

- All sampled timestamps occur in the original decoded video. The recording has 872 decoded frames, versus 900 in the authored reference; complete reference-frame preservation is not established
- The recording and retained audio decode to identical PCM: 2,880,000 mono samples at 48 kHz, RMS −18.04 dBFS. No speech recognition was run
- Three 14-second waveform windows starting at recording times 2, 20 and 38 seconds place the source audio clock about 205 ms behind the recording clock. Their offsets span 0.375 ms. This is an observed spread in one run, not a general drift guarantee
- Three saved visual-clock frames lead the audio-derived source clock by approximately 0.092–0.100 seconds. The source is 15 fps; this is not sample-perfect audiovisual synchronization
- A separate six-cue authored test sidecar was imported as `supplied_sidecar`, with its bytes preserved. It is known test ground truth, not ASR or independently obtained programme subtitles

A separate independent run with `--skip-asr --interval 1 --max-frames 1200` produced 58 sampled frames in 3.098 seconds. Its artifact hashes, source timestamps and retained PCM also passed; this is a different run from the 12-frame result. PCM equality establishes preservation of the captured audio, not lossless equality to the original reference.

The reference is original synthetic material made for Bailey: Pillow charts and FFmpeg/libflite speech, declared CC0-1.0, with no external media. Its SHA-256 is `545be30628834e4e942589a2cb318453013208177269336da4e1404eb725cd97`. No recording, extracted audio, frames or full captions accompany this release.

## Blob versus HTTPS controls

Browser transfer, tab-lifecycle and deletion outcomes are operator observations. Independent review verified the landed media and CLI outputs, not those browser operations.

Four completed controls used identical 38-byte `application/octet-stream` content:

| Transfer | Outcome |
|---|---|
| HTTPS via the supported media-download API | Readable artifact in 151 ms; exact hash |
| HTTPS via a prearmed download event and normal link click | Readable artifact in 1,116 ms; exact hash |
| Retained Blob via the same event and an attached link | No artifact returned within 20 seconds; URL remained unrevoked |
| Blob revoked about two seconds after the same link click | No artifact returned within 20 seconds |

The retained tiny-Blob result shows that video codecs, large files and explicit URL revocation are unnecessary for this reproducer. It isolates a difference in the exposed Blob-to-workspace path, without identifying the hidden platform mechanism. A timeout does not prove that no bytes were written somewhere in the browser environment. Direct Blob export is not fixed. Additional media comparisons interrupted by a transport failure have unknown outcomes; remaining cases were not run. Explicit cleanup of the diagnostic Blob URLs was not observed.

## Interruptions, cleanup and limits

An earlier workspace reset and a later disappearance of temporary browser tabs were separate events. The first 60-second attempt was not retrieved before its tabs disappeared. A repeat with source and recorder tabs explicitly marked for retention succeeded; only that repeat is validated here. These observations do not establish the platform cause of either interruption.

Deletion of the temporary server copy was confirmed in the UI at 13:07:52 UTC. The download endpoint was not retested for HTTP 404. This is a UI-confirmed cleanup outcome, not an independent retention guarantee.

All 69 source blobs were checked against the evaluated public commit, output hashes were verified, saved visual clocks inspected, and original/retained PCM independently decoded and compared. The repository test suite was not rerun for this documentation-only update. The measured 60-second result does not validate long programmes, multiple tabs, YouTube, financial-programme interpretation, continuous visual coverage, or speech-recognition accuracy. Browser timer suspension and existing single-client storage restrictions remain limitations.
