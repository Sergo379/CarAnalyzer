# CarAnalyzer — PAD Lab 1 / technical knowledge RAG

This directory documents Task 2's technical-knowledge layer. The selected vehicle
is resolved against CarAnalyzer's canonical catalog (brand → model → generation →
engine/modification). Marketplace search remains a separate live flow. This
benchmark is explicitly **synthetic**; its fictional Eval Motors Aurora A1
material must never be presented as real automotive advice.

## Requirements checklist and status

The [current PAD README](https://github.com/Randwow/PAD) describes the Lab 1
grabber → processing → embeddings → vector storage → retrieval → reranking →
filtering → LLM → sourced answer sequence, evaluation and experiments. The
professor's approximately heard name “Garben” could not be identified in that
repository/README; no package was guessed or installed for it.

| Lab 1 / Task 2 requirement | Implementation / evidence | Status |
| --- | --- | --- |
| Automatic bounded grabber | Generic EN/RU query builder; bounded DDGS web search plus MediaWiki fallback | Implemented/tested |
| HTML cleaning, RU/EN, hash/dedup | `processing.py`, `repository.py`; repeated import tests | Implemented |
| LlamaIndex Documents/Nodes | `llama_ingest.py` transforms section documents; project types remain outside adapter | Implemented |
| Fixed, overlap, paragraph, section chunking | `processing.py`; four-strategy 40-question experiment | Implemented/evaluated |
| Two local multilingual embeddings | E5-small and multilingual MiniLM, 384 dimensions | Implemented/evaluated |
| Persistent local vector index + lexical | sqlite-vec `vec0` plus FTS5/BM25, SQLite embedding cache | Implemented/tested |
| Hybrid retrieval, metadata isolation | RRF lexical/vector merge; canonical fallback, incompatible scopes excluded | Implemented/tested |
| Local reranking and filtering | multilingual MiniLM CrossEncoder; score gate | Implemented/evaluated |
| Structured grounded LLM generation | Gemini SDK adapter; Pydantic schemas | Implemented with mock tests; live verification pending |
| Hallucination protection | allowed chunk IDs/scope, mandatory citations, technical-code guard, insufficient-evidence result | Implemented; semantic entailment still requires review |
| Evidence provenance | claim → chunk → document/source graph and API `chunk_ids` | Implemented/tested |
| Permanent knowledge cache | canonical `problem_profiles` with pipeline/model versions; no TTL | Implemented/tested |
| MLflow, Langfuse | local MLflow SQLite runs; optional keyless Langfuse spans | MLflow tested; Langfuse offline path tested |
| Evaluation dataset and metrics | 40 synthetic questions, Recall/Precision@K, MRR, Hit Rate, context relevance and generation proxies | Retrieval run; generation awaits Gemini key |
| Prompt variants / answer evaluation | Gemini answer mode and optional `--generation` CLI | Implemented; live run pending |
| Separate API and nonblocking frontend | `/api/knowledge/*`; market UI renders before knowledge request | Implemented/tested |

**Task 2 is not yet fully accepted.** Real-source profile quality, configured
Langfuse delivery, macOS packaging, and broader live localhost acceptance remain
to be verified. Search-engine availability and source licensing/quality remain
external constraints.

## Architecture and identity

```text
catalog canonical IDs ──> knowledge scope key
                           ├─ engine/modification
                           ├─ generation fallback
                           └─ model fallback
                                      │
bounded source providers → URL filter → loader → HTML cleanup/hash
                                      │
                           LlamaIndex section nodes
                                      │
                       40-word / 8-word overlap chunks
                                      │
                  SQLite FTS5 + sqlite-vec + source metadata
                                      │
                     hybrid RRF → local reranker → gate
                                      │
                    Gemini JSON → Pydantic/evidence guard
                                      │
                    permanent SQLite profile + claim graph
```

`backend/knowledge/identity.py` rejects provisional catalog identities for
technical profiles. Scope keys are canonical ID arrays, not display strings or
years. An engine-specific request may use its own scope, then the same
generation's general evidence, then model-wide evidence. It cannot see a
different known engine or generation. The provenance scope of a claim is set to
the broadest scope among its cited chunks, so a model-wide source cannot be
misrepresented as engine-specific proof.

The additive tables live in `backend/database/knowledge_schema.py`:
`knowledge_sources`, `knowledge_documents`, `knowledge_source_documents`,
`knowledge_chunks`, FTS5, `knowledge_embeddings`, `problem_profiles`,
`problem_claims`, `claim_evidence`, and evaluation/experiment tables. Existing
Task 1 tables and market behavior are untouched. Migrations are idempotent.
Content hashes share exact repeated pages within a scope; the same URL or hash
does not rechunk or re-embed unchanged content. Explicit rebuild is supported;
there is no periodic technical-knowledge TTL. Failed/blocked sources are not
misreported as technical claims.

## Collection and preprocessing

The default provider chain first uses the maintained no-key `ddgs` metasearch
adapter and then the public English/Russian MediaWiki API as supplemental
discovery. Queries are generated from canonical brand/model/generation/engine
display metadata and generic English/Russian intents for reliability, major
systems, repairs, recalls, inspection and owner communities. Results are
classified as official, recall, repair, owner-forum, technical-article or
encyclopedia candidates; classification is descriptive, not an authority claim.

Providers are pluggable and independently isolated. The grabber caps queries,
results per query, total sources, sources per domain, discovery/load time and
response bytes. It canonicalizes URLs, rejects local/private-address URLs, and
does not solve CAPTCHAs or evade access controls. A provider returning zero or
failing does not suppress later providers. If the no-key search dependency or
its backend is unavailable, the build reports `search_provider_unavailable`
instead of claiming that technical information does not exist.

Every attempt persists its provider, generated query, status, result count and
failure type. Every candidate source persists provider/query, URL/domain,
category, load status and document/chunk count. `production` and `evaluation`
corpora are explicitly separated; synthetic PAD fixtures cannot enter runtime
retrieval.

HTML preprocessing removes scripts, navigation, forms, footer, cookie/ad
blocks, normalizes whitespace, and preserves headings, list items, technical
codes and Cyrillic/Latin text. A live probe found and fixed a root-class bug:
`header` in the page's `<html>` class must not delete the entire document.
Bounded selectable-text PDFs (at most 25 pages and the configured byte cap)
are parsed with pypdf; image-only scans return `empty_document` rather than
inventing OCR content.

## Chunking, embeddings, store and retrieval

`processing.py` exposes fixed, fixed+overlap, paragraph and section-aware
strategies. The production candidate uses LlamaIndex section Documents and
SentenceSplitter Nodes, then bounded 40-word chunks with 8-word overlap.
Canonical/source/section/component/language metadata is stored in SQLite.
The LlamaIndex adapter is intentionally narrow; business services use
CarAnalyzer-owned interfaces and models.

Embeddings are local, batch-generated and lazily loaded from an ignored
project-local model cache: `intfloat/multilingual-e5-small` and
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. E5 uses its
required `query:`/`passage:` prefixes. The model/version/dimension and vector
blob are stored per chunk. The persistent `sqlite-vec` `vec0` tables provide
vector distance operations; scoped retrieval filters by canonical identity
*before* ranking. FTS5 preserves exact diagnostic-code lookup. RRF combines
lexical and semantic candidates, then exact-content duplicates collapse.
The optional multilingual CrossEncoder reranks local candidates. For a client
with ~8 GB RAM, no local generative model is required; Gemini is remote, while
embedding/reranker models load only on knowledge builds.

## LLM, evidence and cache

`backend/knowledge/gemini.py` uses the official Google Gen AI SDK. Configure
`GEMINI_API_KEY` and optionally `GEMINI_MODEL` in the local environment.
No key is stored, printed or required for tests. Only selected top evidence
chunks, never whole documents, are sent. Invalid structured output is never
saved as valid knowledge. Bounded retries apply to temporary service failures.

Profile output is strict Pydantic JSON: common problems, problematic
components, inspection points, expensive failures, risk summary, evidence IDs
and derived confidence. Every nonempty claim needs cited chunk IDs valid for
the requested scope. Unknown codes not present in cited evidence are rejected.
Confidence is a local heuristic: three independent domains *and* exact-scope
evidence = high; two domains = medium; otherwise low. Reposted URLs from one
domain do not count as independent domains. This is **not** a calibrated
probability or semantic entailment proof. Empty evidence returns and caches
`insufficient_evidence`; temporary block/timeout is not permanently cached.

On a cached canonical profile, `GET /api/knowledge/profile` reads SQLite only:
no discovery, embedding, reranking or Gemini call. Manual `POST /rebuild` may
reuse existing documents or explicitly refresh bounded sources. New builds run
as separate local background tasks. `GET /api/knowledge/status` reports the
current phase, start/elapsed time and live discovery, ingestion, retrieval and
LLM counters. Duplicate build requests for the same canonical scope reuse the
running task. `/api/search` never waits for this path.

## Evaluation, experiments and selection

`evaluation/corpus.json` contains ten deliberately fictional technical
documents with model/generation/engine scopes. `questions.json` has 40
questions: engine, gearbox, suspension, electronics, body, brakes, service,
inspection, recall, five multi-document and five intentionally unanswerable.
Each evaluated row is tagged with its canonical vehicle scope.

`evaluation/metrics.py` computes Recall@K, Precision@K, MRR and Hit Rate on
expected document IDs. Context relevance is the fraction of retrieved chunks
from expected documents. Unanswerable false-positive rate measures whether
any context remains. Generation evaluation computes citation-validity and
expected-fact **proxies**; they are not human truth judgments or model-based
faithfulness scores. Gemini-based answer experiments are implemented but cannot
be run without the user's local key.

Recorded experiments and full configurations are in
[`results/experiments.json`](results/experiments.json), with per-question rows
in the ignored `data/rag_experiments.db` and MLflow tracking in ignored
`data/mlruns/mlflow.db`. Representative 40-question results (rounded):

| Configuration | Recall@K | Precision@K | MRR | Unanswerable false positive | Mean retrieval latency |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lexical fixed, K=5 | 0.957 | 0.286 | 0.910 | 1.00 | 0.03 ms |
| E5 overlap, K=5, RRF 0.5 | 1.000 | 0.349 | 0.981 | 1.00 | 26 ms |
| MiniLM overlap, K=5, RRF 0.5 | 0.986 | 0.337 | 0.971 | 1.00 | 24 ms |
| E5 overlap, K=3 | 0.971 | 0.505 | 0.981 | 1.00 | 25 ms |
| E5 overlap, K=5 + reranker, no gate | 1.000 | 0.383 | 0.952 | 1.00 | 909 ms |
| E5 overlap, K=5 + reranker, score ≥ 0 | 0.814 | 0.194 | 0.829 | 0.00 | 705 ms |
| E5 LlamaIndex-section/overlap, K=5 | 0.971 | 0.394 | 0.957 | 1.00 | 26 ms |
| E5 LlamaIndex-section/overlap + reranker, score ≥ 0 | 0.743 | 0.177 | 0.771 | 0.00 | 700 ms |

Top-K values 3/5/10/20, RRF lexical weights 0.2/0.5/0.8 and filter
thresholds were also run. The conservative score gate makes unanswered queries
return no evidence in this synthetic set but loses some answerable evidence;
this safety trade-off requires real-source validation. The reranker scores
overlap: the lowest answerable top score was −5.16 while the highest
unanswerable top score was −1.38, so no single threshold can perfectly separate
both classes here. The old **binary** production gate used reranker score ≥ 0
after provenance/scope filtering. A negative cross-encoder score is not itself
proof that a passage is false; on a broad vehicle-profile query it discarded
useful technical evidence and returned `insufficient_evidence` before Gemini
could synthesize a cautious, cited inspection point.

The graded policy hard-rejects non-production/unlinked, incompatible, empty,
clearly nontechnical, duplicate, or extremely mismatched chunks, then labels
usable evidence HIGH/MEDIUM/LIMITED. Claim confidence is application-derived
from scope specificity, source category, relevance, unique document count and
independent domains. Owner-only and single model-wide support are LIMITED.
An exact professional repair source can be MEDIUM; a single general technical
article needs stronger relevance. HIGH needs exact strong relevance plus
independent technical corroboration or a strong official source. Three chunks
from one article do not count as three sources. No surviving evidence means
no Gemini call and no claim. Existing cached `low` profiles read as `limited`;
explicit `/api/knowledge/rebuild` regenerates on demand.

Focused PAD Lab 1 comparison on the same 40 synthetic questions, E5 + local
reranker, K=5; final MLflow run `16f0095e0e9b4acda6280067106db936`:

| Policy | Answerable questions with expected evidence | Context relevance | Unsupported-context fraction | Unanswerable context FPR |
| --- | ---: | ---: | ---: | ---: |
| Old score ≥ 0 | 77.1% | 86.1% | 13.9% | 0% |
| Hard gate + graded, relevance floor −1 | 85.7% | 85.7% | 14.3% | 0% |

This is a **context-risk comparison**, not a generated-claim result. No Gemini
answers or human entailment labels were produced in this run, so actual
unsupported-claim rate and semantic faithfulness remain unmeasured. The
synthetic benchmark asks narrow questions whereas production builds broad
vehicle profiles; its unanswerable-context FPR cannot be equated to the
end-user false-claim rate. An exploratory floor of −8 raised coverage to 100%
but also raised unanswerable-context FPR to 80% (MLflow run
`b7549e40d08a481bb49da37d4606bef6`), so it was rejected. The final −1
floor gives higher coverage with no measured context-FPR increase and nearly
unchanged context purity on this dataset. Answer-level Gemini generation and
human/entailment review are still needed to measure unsupported-claim rate and
semantic faithfulness directly.

Bounded live validation on four previously uncached canonical scopes (at most
three discovered sources per new-scope build) observed a true no-chunk
`insufficient_evidence` case, two complete LIMITED model-wide profiles, and
one complete profile with both HIGH and LIMITED cited claims across three
independent source domains. The profile with mixed tiers was explicitly
rebuilt from its existing local corpus after a manual evidence audit exposed
an unsupported repair inference; the revised prompt and category-specific
validator now require explicit cited repair/cost text for an
`expensive_failures` item. MEDIUM was covered by deterministic tests but was
not observed in this small live sample. These few live profiles do not
establish a population-wide hallucination rate.

## Install, run and API

From the project root (Windows PowerShell):

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync --extra knowledge --extra lab
uv run --extra knowledge --extra lab uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

The optional `knowledge` extra contains LlamaIndex, sentence-transformers and
sqlite-vec; `lab` adds local MLflow and optional Langfuse. Standard Task 1
installation remains lightweight. The first local model use downloads weights
to ignored `data/model_cache/`. No source crawl is performed on startup.

```powershell
uv run --extra knowledge python -m backend.tools.knowledge_config
uv run --extra knowledge python -m backend.tools.knowledge_ingest --brand Ford --model Fiesta --max-sources 1 --discover-only
uv run --extra knowledge python -m backend.tools.knowledge_ingest --brand Ford --model Fiesta --generation-id YOUR_CANONICAL_ID
uv run --extra knowledge --extra lab python -m backend.tools.rag_evaluate --suite
uv run --extra knowledge --extra lab python -m backend.tools.rag_evaluate --tune
uv run --extra knowledge --extra lab python -m backend.tools.rag_evaluate --embedding-model e5-small --chunk-strategy llama-section --reranker --generation --prompt-version grounded-v1
```

For the second generation mode, rerun with
`--prompt-version grounded-conservative-v2` **only after** configuring a real
Gemini key locally. The CLI refuses `--generation` when the provider is not
configured. `--rebuild` is explicit; `--refresh-sources` requires `--rebuild`.

Knowledge endpoints: `GET /api/knowledge/config`, `GET /profile`, `POST
/build`, `POST /rebuild`, `GET /status`, `GET /sources` (all under
`/api/knowledge`). Query/build selection fields are brand, model, optional
canonical generation ID, engine ID, modification ID. The API returns
`cached`, `building`, `complete`, `partial`, `no_sources`,
`insufficient_evidence`, `provider_not_configured`, `rebuild_preserved`, or `failed` where
applicable. Claim evidence refs map to `chunk_ids` exposed by `/sources`.
The frontend starts knowledge loading only after the market result is drawn.

Run checks:

```powershell
uv run --extra knowledge --extra lab pytest
uv run ruff check .
cd frontend
npm test -- --run
npm run build
```

## Defense summary and limitations

Why SQLite/FTS5/sqlite-vec: a local, persistent, portable desktop store; no
mandatory remote vector service. Why E5: it slightly exceeded multilingual
MiniLM on this measured corpus. Why a reranker: better context precision at a
one-time build latency cost; the conservative gate trades recall for avoiding
unsupported answers. Why separate background knowledge: marketplace latency
must not include ingestion/LLM work. Alternatives examined: larger local LLMs
(too costly for M2/8 GB), remote vector DBs (unnecessary service), semantic-only
retrieval (misses exact codes), and display/year-only profile keys (unsafe).

Remaining work before a truthful production-ready claim: add vetted technical
source providers beyond general Wikipedia; test document licensing and source
quality; use the user's locally configured Gemini account to run both prompt
variants and semantic/human faithfulness review; verify optional Langfuse
delivery; perform end-to-end localhost and macOS/M2 acceptance. A missing key
must never be worked around with fabricated generation results.
