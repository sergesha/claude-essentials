# Gemma product validation (2026-10-04)

Profile: pinned Gemma Q4, chunk budget256, overlap32, dimension256, bounded label
prefix, paragraph packing, exact FLAT cosine search and best-chunk parent ranking.
The public frozen corpus is `corpus.json`; its SHA256 is in the result artifact.
No production payloads or scope names are included.

`product.py` exercises the real HTTP embedding client, chunker and Redis storage/search.
On 27 documents and 26 queries it produced313 vectors: Top1 **18/26**, Top5 **26/26**.
Redis rankings matched an independent NumPy exact max-chunk ranking for all queries.
Ingestion took22.69s. Query latency (embedding included),104 samples per concurrency:
see `gemma-product.json` for p50/p95 at concurrency1 and4.

`scale.py` measures actual storage and search with a cached real query/document
vector, one chunk per parent, at1,000 and10,000 parents, two repeats and104 searches
per concurrency. It does **not** measure large-corpus embedding throughput or quality.
At10k: Redis used48.64–48.77MiB; cumulative saves43.43–44.39s;
search p95 at concurrency1 was31.16–31.55ms, concurrency4 was72.33–81.19ms.
No evictions. Exact indexes are deliberately retained; these results are not a
prediction for arbitrarily large corpora or worst-case repeated-chunk documents.

Environment: native ARM CPU ONNX Runtime1.30.0, two intra-op threads, serial model
service; Redis Stack AMD64 under Docker emulation on the same Apple Silicon host.
Absolute timings are specific to this environment. Redis `used_memory` excludes
model/runtime memory and differs from container RSS. The earlier model screen used
about594MiB for Gemma versus2.15GiB for the old TEI deployment; runtimes differ.

`gemma-rehearsal-result.json` records a verified private RDB restore into a separate
Redis Stack:94 canonical memories, one KV, preserved payload identities and absolute
expiries through build/resume/verify/cutover/repeated cleanup. Old+new indexes coexist
before cleanup. The live database was not migrated. Backup checksum only is public.

Run scripts only against disposable isolated Redis databases; product/scale scripts
require an empty database. Set `REDIS_BENCH_URL`, `EMBED_BENCH_URL`, and optionally a local
`EMBED_TOKENIZER_PATH`; install the server and its embeddings extra first. Set `BENCH_OUTPUT` for the JSON result path. Private model cache,
manifests and RDB files must stay outside Git.
