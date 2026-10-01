# Validation report

Validated 2026-10-01 on Linux, Python 3.12.14, FFmpeg 7.1.5, faster-whisper 1.2.1, CTranslate2 4.8.2, PyAV 16.1.0, and the pinned multilingual Whisper small model. All speech inference used CPU/int8 with four threads. These are short smoke tests, not a benchmark of arbitrary video or financial analysis accuracy.

## Measured runs

| Input / check | Result | Wall time | Peak reader RAM |
|---|---|---:|---:|
| Original synthetic English speech + two charts, 13.06 s | 13 timestamped 1600×900 frames, independent ASR and deliberately conflicting captions | 4.38 s | 646 MB |
| Source-time window 6.8–12.9 s | Seven frames at 6.862–12.862 s; ASR starts at 6.8 s, source caption boundary retained | 3.92 s | 643 MB |
| Video with no audio stream, 2 s | `no_audio_track`, two frames, no invented transcript | 0.50 s | 23 MB |
| Standalone synthetic WAV speech | `audio_only`, correct transcript, no frames | 3.86 s | 643 MB |
| Standalone synthetic MP3 speech | `audio_only`, correct transcript, no frames | 3.72 s | 642 MB |
| Real Mandarin FLEURS audio, 8.04 s | Correctly recovered the spoken 80% tariff figure and reference sentence | 3.82 s | 641 MB |
| Same Mandarin audio + test-added chart/reference cue | Eight 1600×900 frames, independent audio ASR and caption stream, time-aligned output | 4.20 s | 646 MB |

Reader RAM is process peak RSS, not the whole machine. FFmpeg child peak for the first English run was 195 MB; do not simply treat reader RSS as the entire job budget. The model occupies approximately 464 MB and the isolated environment approximately 492 MB. Model download took 45.7 seconds in this environment; installation/download speeds are not guaranteed. Elapsed times use a monotonic clock.

Final local checks: **17 tests passed**, Python compile checks passed, and `pip check` found no broken requirements. Tests cover local-only inputs, fresh output directories, subprocess protocol restrictions, failure propagation, caption source-clock windows, gap semantics, nearest-frame honesty, real generated outputs, WAV/MP3 entry points, native crop dimensions/hashes, and Mandarin 80% extraction. Upstream's entire test suite was not run: its unused yt-dlp/MLX/other optional extras are deliberately absent. Integration assertions skip on a fresh checkout until their named media outputs are generated.

## What independent modalities established

Two fresh evaluations received restricted evidence. Visual-only reading identified Atlas revenue 120 versus Boreal 80, and Boreal profit 30 versus Atlas 10, all in USD millions. It correctly could not establish what the speaker said or whether captions disagreed.

Transcript-only reading identified the spoken blue/orange references and a conflict between ASR (“orange” earns more profit) and the supplied caption (“blue”). It correctly could not identify product names, chart values, numerical differences, or which statement the chart supported.

Combined inspection connected orange to Boreal and the 30 versus 10 profit values. It therefore supported a USD 20 million profit difference and showed that the deliberately wrong caption conflicted with both the chart and the independently decoded speech. Both charts were actually visually inspected, not inferred from filenames. This is a demonstration on a controlled fixture, not a general understanding score.

## Mandarin source and limits

Source: [Google FLEURS](https://huggingface.co/datasets/google/fleurs), Mandarin configuration `cmn_hans_cn`, validation row 179, sample ID 1566, revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`. [License evidence](https://huggingface.co/datasets/google/fleurs/blob/70bb2e84b976b7e960aa89f1c648e09c59f894dd/README.md): CC BY 4.0. Attribution: FLEURS, Google; Alexis Conneau et al., [FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech (2022)](https://arxiv.org/abs/2205.12446).

The reference contains “百分之八十”; ASR produced “80%”. This is the same numerical value with different formatting. The first utterance carries a low-confidence-word flag, retained for review. The source's ellipsis/sentence fragment is preserved in reference metadata. It is read speech about tariffs, not validation of trading terminology, currency/decimal precision, Taiwan-accent conversational audio, overlapping speakers, or noisy recordings.

The Mandarin chart was explicitly created for this test; it is not an original video frame from FLEURS. Its reference caption was manually given a single full-clip time interval and is not an independently timed subtitle track. Raw media is not included in this repository; `tests/mandarin-source.json` preserves public attribution, reference and byte hash without an expiring signed download URL. `tests/fetch_mandarin_fixture.py` explicitly retrieves one sample and verifies revision/text/hash.

## Bugs found and fixed

- PyAV 19 removed an API argument used by faster-whisper 1.2.1. The working dependency is pinned to PyAV 16.1.0
- FFmpeg output-side duration limiting could log an extra frame in `showinfo` and drop it before writing. Extraction now limits input/trim duration and rejects any frame/timestamp count mismatch
- Audio-only files initially lacked an entry point. WAV and MP3 now use the exact independent ASR pipeline and explicitly have zero visual frames
- Caption availability never bypasses audio ASR. All candidate text tracks are retained separately, and a multi-audio input requires explicit track selection

## Known limitations / not tested

- ASR can mishear numbers and quiet/overlapping speech. Word timings are estimates; WhisperX forced alignment/diarization is not installed
- Lexical discrepancy flags are review hints. The shortened but consistent first English caption also triggered a flag: this is a known false positive caused by paraphrasing/segmentation. Semantic contradiction detection is not claimed
- No claim to interpreting music, sound effects, emotions, tone, or speaker identity. Original audio is retained, and untranscribed gaps are unclassified rather than labeled silent
- Frames are sampled. Brief animation, tiny text, and events between samples can be missed. Native-resolution crops and denser windows help but do not establish continuous-video understanding
- Burned-in captions require image reading; no complete automatic OCR pass is implemented. Text subtitle extraction failures remain explicit
- No web-video downloader, YouTube access, sign-in, paid API, or external speech endpoint was exercised
- Hugging Face offline/no-token/no-telemetry settings and ONNX Runtime telemetry disablement are explicit. A syscall-level network audit was unavailable because the validation container disallowed ptrace; no packet-level egress guarantee is claimed
- System FFmpeg/Python tools and the underlying dependencies remain part of the trust boundary. This is not a hardened hostile-media sandbox or an exhaustive security audit
