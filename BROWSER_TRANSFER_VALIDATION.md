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

## 2026-10-01 14:35 UTC: separate manual external ASR check

On 2026-10-01, a browser operator generated English captions for the same recorded 60-second fixture in Super Captions, with AI correction disabled; the backend model/version was not exposed. Eighteen timestamp/text triples were copied from the visible editor and reconstructed as SRT, rather than downloaded as a service export. Comparison against the separate authored fixture script matched all 48 explicitly checked fields across six marker groups: marker number, spoken time, and each color label, metric and value. This is a limited semantic check, not a word-error-rate result or a general accuracy estimate.

The website step was manual; no CLI-to-service connector was added, and the external service was not rerun during review.

The unchanged CLI at `56a7052` imported that external-ASR-derived SRT using `--skip-asr --captions <reconstructed-srt>`. With `--interval 1 --max-frames 1200`, it produced 58 sampled frames in 3.459 seconds; all 18 cues overlapped at least one sampled frame. SRT bytes and captured-audio PCM were preserved, and text/chart values were checked together. The earlier five-second sampling run produced 12 frames but only three cue overlaps, so it is not evidence of full cue coverage. Local ASR remained skipped; `supplied_sidecar` is the import provenance, while the separate record identifies the external ASR source.

Cue order and recording bounds passed. Exact speech-boundary alignment remains unverified: marker 3 ends about 1.663 seconds before the audio-clock-mapped authored speech-segment endpoint, which itself is not a forced-aligned word boundary. The existing approximately 0.1-second audiovisual skew remains. This result does not establish direct assistant listening, Chinese television accuracy, or successful viewing of the intended television programme. The prior no-ASR experiments remain unchanged; this is a separate later experiment. No media, full captions, account details or session identifiers are included.


## 2026-10-01: downloaded economics and financial-education clips

Two publicly downloadable clips exercised the local-file path with manually generated external captions. These are separate from the browser-recording test above. Source files remained unchanged; caption provenance stayed `supplied_sidecar`, and local ASR was skipped. A browser operator used Super Captions with AI enhancement disabled, selecting English for the first clip and Traditional Chinese for the second. The model/version was unavailable. Editor observations were reconstructed as SRT; no automated service connector was used.

- **Dallas Fed, 2017:** [What Does Labor Force Participation Tell Us About the Skills Gap?](https://fraser.stlouisfed.org/title/6146/item/594002/content/mp4/kaplan_laborforce20170405), archived by St. Louis Fed FRASER. The 82.048633-second source produced 83 sampled frames and imported 39 external-ASR cues with `--skip-asr --interval 1 --max-frames 180` on the unchanged `4720f63` runtime in 5.533 seconds. All sampled images were inspected. Source and upload preview decoded to identical native stereo PCM. The original and preview also matched all 3,847 audio-packet records. One cue lacked an inside-interval frame; an adjacent sample was explicitly labelled. The linked official transcript has a heading inconsistent with the on-screen question, so it was not used as word-for-word ground truth.
- **TWSE educational clip:** [official distribution page](https://www.emega.com.tw/emegaTran/bulletin.do?id=20260106092157995714), “主動式ETF與被動式平衡型ETF介紹.” The 33.109333-second source produced 39 inspected sampled frames and three externally generated cues. Source and upload preview matched all 1,552 AAC payloads and decoded PCM at the original sample rate and channel count; their final packet durations differed by 32 samples (0.667 ms). The three long cues are structurally valid but split phrases, and one ASR/burned-in-text wording difference was observed. These checks do not establish word timing or a transcription accuracy score.

Publication review found a separate AAC retention regression in the previously published runtime: the TWSE `audio_original.mka` kept compressed packets but lost the original 2,112-sample skip metadata. Native decoding therefore added 44 ms of leading samples. The original source and external-ASR preview were unaffected. The correction selects a suitable container and verifies compressed payloads, native decoded samples and exact decoded timestamps before accepting the retained audio. On the corrected code, independent full-window reruns preserved both clips’ sampled frames, captions and alignment; TWSE completed in 5.575 seconds and Dallas in 8.894 seconds with `--skip-asr --interval 1 --max-frames 180`. Both retained AAC tracks now decode identically to their sources. Eight additional generated/selected-stream cases passed independently, covering two AAC rates/channel layouts, WAV, Opus, MP3, ALAC, FLAC and a delayed second audio stream. These are distinct from the repository unit-test count.

These observations combine sampled-image inspection, external ASR and source-text cross-checks; they do not involve direct assistant listening. Untranscribed intervals do not prove silence. They establish neither YouTube acquisition nor full-length programme coverage, general ASR accuracy, continuous visual understanding or investment performance. The [FRASER terms](https://fraser.stlouisfed.org/terms-of-use) govern its source material, and no open redistribution licence was verified for the TWSE clip. Only technical measurements and attribution are published; media, images and full captions are excluded. Neither source publisher endorses Bailey.
