# Verified fallback for missing WebM duration

Base: public `c7a634f3364d499668628eb5a5626b8797df801f`. This is a local repair, not a browser download implementation. No publication is included in this validation.

## Reproduced failure and result

An existing original synthetic video was locally encoded as VP9/Opus, then losslessly remuxed with FFmpeg's `-live 1` option. The ordinary 3-second WebM had a declared duration and worked. The live WebM had no `format.duration`; the old reader exited with `ERROR: 'duration'`, and queue readiness refused it.

With this fallback, the original live WebM is accepted without overwriting it. A temporary, non-reencoded remux established a 3.028-second container duration, consistent with the measured packet extent including codec timing. All 181 packets (30 video and 151 audio) retained identical payload hashes, PTS, DTS, packet durations and side data. The reader produced three frames and preserved original Opus audio, with ASR explicitly skipped. The same bytes pass queue readiness with verification provenance. The source SHA-256 is unchanged.

These are self-generated fixture results, not a recording retrieved from a browser. The original browser Blob still has not been transferred into the CLI. Separately, a normal HTTPS media download succeeded, so the observed failure is specific to the unresolved Blob/UI-download route, not proof that all browser transport is broken.

## Scope and safety of the fallback

Only a local `.webm` with missing container duration enters the fallback. Files with a valid declared duration take the normal path; invalid declared values are rejected. The fallback:

1. Bounds the regular source file and hashes its bytes
2. Streams each original packet record into a per-stream digest, without retaining packet JSON in memory
3. Remuxes every stream to an owned temporary WebM with `-copyts`, `-c copy` and `-avoid_negative_ts disabled`; it never overwrites the source
4. Repeats the packet scan and requires identical per-stream payload/timing/side-data digests and counts
5. Reads the new container duration, checks it against the observed packet extent, and rechecks source size/hash
6. Deletes the temporary remux and returns duration plus verification provenance; actual extraction continues from the original input

Any FFmpeg/FFprobe warning or error refuses the fallback, even when the process exits zero. This matters because a mid-packet truncated WebM produced `File ended prematurely` while FFmpeg still returned zero. No successful output is inferred merely because remux created a file.

The shared helper is used by reader and queue probing. Its SHA-256 is recorded in reader manifests, fallback provenance and the batch runtime identity. A helper-only change invalidates batch resume identity; existing cache validation was extended only for that dependency.

## Resource limits and cleanup

- Maximum fallback input: 256 MiB
- Maximum measured packet timeline: two hours
- Maximum packets: 250,000; maximum streams: 32
- Maximum streamed packet metadata: 64 MiB per scan; maximum individual line: 8 KiB
- Maximum diagnostic output: 64 KiB; any diagnostic content is rejected
- Total fallback deadline: 60 seconds, covering hashing, packet scans and remux; process termination/cleanup is additional overhead
- Temporary remux limit: 1.25 times input bytes plus 8 MiB, with an additional 32 MiB of free-space reserve
- Packet data is processed incrementally, keeping only digests/counts and a bounded line buffer

Normal exceptions and the helper's internal timeout terminate and wait for its child process, then remove its temporary directory. Main-thread CLI SIGTERM/SIGINT is temporarily converted into an exception so the same cleanup runs, including an outer queue timeout. Existing signal handlers are restored afterward. The signal exception deliberately is not `InterruptedError`, which selectors treats as a retryable syscall interruption.

SIGKILL, host failure, or non-main-thread process termination cannot be guaranteed to execute Python cleanup; an owned temporary directory may remain in those cases. No automatic deletion sweep of unrelated temporary files is performed.

This is an intentionally conservative verified-repair fallback. It reads the source multiple times, requires temporary storage, and may run once at readiness and again during reader processing. Readiness performs its validation while holding the existing queue state lock, so an unusually slow fallback can delay other state operations on that queue. Large or warning-bearing recordings may be refused; no production-scale long-programme throughput or memory stress claim is made. Files with ordinary duration metadata avoid these additional scans/remuxes.

## Verification

The added tests generate their own tiny VP9/Opus fixtures and cover successful recovery; original-file and packet preservation; zero ASR imports; queue handoff identity; no-audio input; declared-duration bypass; malformed/truncated input; bounded packet/metadata/file/disk resources; actual timeout child termination and cleanup; external SIGTERM cleanup; signature mismatch; and invalid duration rejection. A separate batch test proves a changed helper hash cannot reuse old evidence.

An independent reviewer generated additional VP8/Opus, negative-start, audio-only and video-only fixtures and verified packet equality, source mutation refusal, resource bounds and cleanup. An initial external queue-timeout cleanup defect was found and fixed; both deterministic and real queue-timeout checks then passed. The final local full-suite result and independent review are recorded in the accompanying review report/manifest.

Final results are separate runs: the implementation workspace freshly regenerated all legacy media/batch fixtures, then passed **138 tests, zero skipped** in 17.42 seconds. The independent clean source copy passed **126 repository tests, 12 skipped** because those legacy generated outputs were absent, plus **26 independent tests** using the reviewer's own fixtures. The six code/test file hashes matched the frozen review scope. The skipped assertions were not counted as independent passes.

Re-run focused tests with the existing environment:

```sh
.venv/bin/python -m pytest -q tests/test_webm_duration.py tests/test_batch_reader.py
```

Run the entire reader/importer/queue suite after generating integration artifacts on a fresh output tree:

```sh
.venv/bin/python tests/run_media_regression.py
.venv/bin/python -m pytest -q tests capture_queue/tests
```

## Remaining limits

A file ending cleanly at a valid packet/cluster boundary can look like an intentionally short recording. Container validity and a clean remux cannot establish that the intended recording end was captured; that requires independent expected-length/acquisition evidence. Missing PTS/DTS, non-finite timing, timestamp changes, unsupported mux behavior or warnings are refused instead of being repaired speculatively. The 100 ms extent comparison is a conservative container/codec consistency check, not word alignment or original-programme clock verification.

Transcription remains separate from visual/audio interpretation. The fallback neither runs a speech service nor turns sidecar text into original subtitles or direct listening. Browser capture permissions, source acquisition, Blob transfer, website automation and multi-tab recording are unaffected.


## MediaRecorder video packets with unspecified duration

A real, normally permitted browser recording exposed another legitimate WebM form: the container had no declared duration, and 41 of 140 VP9 packets had no packet duration. All PTS/DTS and 166 Opus packet durations were present. The published `4fa1be3` fallback conservatively refused this file. Matroska permits omitted `DefaultDuration`/`BlockDuration`; it does not establish the duration of the final displayed video frame. See the [Matroska element specification](https://www.matroska.org/technical/elements.html#BlockDuration).

The additional branch is restricted to VP8/VP9 video with Opus audio. It:

- Normalizes only the source packet positions whose video duration was originally unspecified. Any original known duration, PTS/DTS, payload hash, packet size/flags, and side data must remain identical. The independent raw packet digests before/after are also retained, so newly populated durations are visible rather than silently represented as identical
- Requires a reliable Opus packet endpoint to cover video timestamps, and decodes every source/remux frame to compare frame/sample timestamps and counts. Decoded Opus sample endpoints must agree with the packet endpoint within 100 ms; remux container duration is checked against the observed packet extent
- Reports the interval between the last decoded video frame timestamp and the audio endpoint as `video_tail_hold_unverified_seconds`. It does not fill that interval, manufacture a video duration, or infer real FPS from `r_frame_rate`
- Refuses missing audio duration, missing PTS/DTS, video extending past the audio endpoint, changed known timing/payloads/flags, decoder warnings, truncation warnings, or elapsed resource limits. Video-only files whose packet durations are missing still fail; the prior complete-timing video-only path remains supported

The new ordinal set holds at most 250,000 integers. Packet and decoded-frame metadata remain streamed and bounded at 64 MiB per scan; decoded-frame count is also capped at 250,000. Both extra full decodes share the existing 60-second fallback deadline. This may reject a large or expensive-to-decode recording; there is no long-programme throughput claim. Normal declared-duration inputs retain their fast path.

### Actual short recording, separately from synthetic tests

One approved foreground browser tab played the [W3C HTML5 Video demonstration](https://www.w3.org/2010/05/video/mediaevents.html), with its tab audio. The selected content was the Sintel teaser, © copyright Blender Foundation | durian.blender.org, [CC BY 3.0](https://durian.blender.org/sharing/). A 10-second MediaRecorder recording was stopped, manually uploaded to the owner's private test Site, and retrieved via an authenticated same-origin HTTPS download. Raw media, extracted audio, and frames are not part of this source release.

The 912,594-byte source SHA-256 is `a0ef4d5864b8d836d12aeb4639c77bebe278cf5d898f22a66d7c45852d02a1e1`, matching the UI upload receipt. The candidate CLI processed the original file with `--skip-asr` in 1.621 seconds, producing 10 sampled frames and retained Opus audio. The 306 encoded packet payloads and source timestamps were preserved. The media timeline ends at 9.99 seconds; the last decoded video frame timestamp is 9.883 seconds, leaving 0.107 seconds of unverified final-frame hold. Decoded mono audio contains 478,080 samples at 48 kHz (9.96 seconds of samples), RMS −17.203 dBFS, and is non-silent. The extracted audio decodes to exactly the same PCM SHA-256 as the recording: `1c27d92824f79572cf80b1bda7a87193cad27de7ec00be4ee1898f4b3bb6f227`.

These are distinct time measures: timestamp span is not replaced with sample-count duration. Sampled-frame timestamps match the original decoded video timestamps. No ASR, speech understanding, continuous perception, long-video, multiple-tab, or YouTube success is claimed. The browser operator observed the private temporary-copy deletion confirmation; a blocked follow-up URL was not treated as proof of an HTTP 404.

### Reproducible regression checks

`tests/test_mediarecorder_webm.py` creates its own tiny VP9/Opus live WebM, replaces only optional track `DefaultDuration` with an equal-size EBML Void, and verifies the resulting missing-duration path. This changes no encoded media bytes or parent element lengths and leaves no checksum mismatch. Fifteen added tests cover successful original-file reader/queue use, zero ASR imports, integrity/timing changes, no audio basis, malformed timing, truncation, and the shared decode deadline.

Run with the installed local dependencies:

```sh
python -m pytest -q tests capture_queue/tests
```

Candidate result: **141 passed, 12 skipped**. The 12 skips are existing media/ASR/batch integration artifacts not recreated in this clean staging directory; they are not reported as passing. The targeted new tests are **15 passed**. The real short recording above was independently verified separately, without publishing the clip.
