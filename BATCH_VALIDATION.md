# Bounded batch validation

Validated 2026-10-01 on the same CPU-only environment as the single-file reader. This adds local-file queueing; it does not add YouTube downloading or assert that an assistant has understood the outputs.

## Actual measured comparison

The same four inputs were processed with one versus two workers: two short videos (13.06-second English chart/speech fixture and 8.04-second licensed Mandarin speech with a test-added chart), one 6.72-second WAV, and one 6.34-second MP3. Language detection was automatic. Each worker used four CPU inference threads; the environment reported nine logical CPUs.

| Configuration | Elapsed | Sampled aggregate process-tree peak RSS | Successful items |
|---|---:|---:|---:|
| Sequential, one worker | 22.122 s | 637 MB | 4/4 |
| Two workers | 13.760 s | 1,186 MB | 4/4 |

For this batch, two workers gave **1.61× throughput** and **38% lower elapsed time**, while sampled peak memory was about 1.9× higher. Earlier runs of the same workload measured 22.71 versus 13.47 seconds and 21.38 versus 12.42 seconds, so these are measured examples rather than a stable performance guarantee. Model initialization is repeated per item, and the benefit can differ substantially on long recordings or a busy machine. No more than two workers were tested or allowed.

RSS was sampled from the batch process and its descendant process trees every 50 ms. It is not whole-machine memory, a hard maximum, or a memory sandbox; brief peaks may be missed. Admission uses a 900 MB estimate per worker plus 256 MB reserve, constrained by visible available memory/cgroup headroom. This machine exposed `/proc/meminfo` but no usable cgroup memory limit. Unknown headroom reduces a two-worker request to one. CPU thread totals are capped against the detected CPU count.

## Correctness checks

- Default is sequential; CLI permits only one or two workers
- Jobs use isolated directories and fresh numbered attempt directories. A failed item does not delete or overwrite another item's artifacts
- Each summary reports source duration/window, image count, ASR status/segments, caption tracks/issues, unclassified audio gaps and low-confidence segments
- Every item explicitly says `analysis_status: not_performed`; no `watched=true` or complete-understanding claim is emitted
- A missing path and a corrupt media file failed independently while both valid files finished
- The per-item watchdog was exercised with a deliberately tiny timeout; it terminated that worker and recorded `timed_out`
- Exact resume verified and reused all four completed jobs in 0.388 s, without rerunning ASR
- A controlled edit to one generated ASR JSON broke its artifact hash. Resume created a new attempt only for that item and reused the other three; the repair run took 6.81 s
- Unit tests change caption content, source content, options/version context, manifest contents and artifacts to confirm that stale/mismatched evidence is rejected
- URL inputs are explicitly rejected; JSON manifests accept local path strings only

The resume identity includes source bytes/hash, sidecar content hashes, reader and batch code, upstream manifest, model files, relevant options, Python/dependency versions and FFmpeg version. The successful manifest itself and every artifact must still match saved SHA-256 hashes. This is intended to prevent accidental stale-cache reuse, not to authenticate against a malicious owner who can rewrite both evidence and checksum metadata.

Batch-specific tests: **26 passed** on the generated fixtures. Combined local suite: **43 passed**, plus compile and dependency checks. Single-file tests remain separately applicable; fresh checkout integration assertions skip until their fixture outputs exist. No new dependencies were required for batching.

## Practical limits

The default 1200-frame budget is approximately 20 minutes at a one-second floor and can be consumed earlier by scene changes. It intentionally refuses excess frames rather than silently cutting off evidence. Longer programs need deliberate windowing or adjusted budgets/intervals. The default worker timeout is one hour and can be changed explicitly.

Long-form financial shows, network acquisition, channel completeness, all-program daily coverage, exhaustive chart reading and the assistant's synthesis throughput are **not validated by this short-file benchmark**. Preprocessing is only the first stage of actual review.

## Pre-publication review fixes

Malformed but valid JSON cache documents (arrays/null) are rejected rather than aborting resume. Source/sidecar identity is rechecked before launch and before certification, with worker model/options/runtime/caption hashes verified against prepared identity. Run-history numbering survives missing middle reports without overwriting later reports. A per-output-directory lock rejects simultaneous writers, and outstanding workers are terminated on abnormal batch exit. These checks have dedicated regression tests.
