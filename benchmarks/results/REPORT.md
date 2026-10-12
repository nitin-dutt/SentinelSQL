# SentinelSQL benchmark report

Generated from `benchmarks/results/*.json` by `python -m benchmarks.report`. Every figure comes from a result file; intervals are 95% Wilson (proportions) or paired bootstrap. See each result file's `meta` block for model, package versions, dataset hashes and rules fingerprint.

## 1. Security: attacks submitted as SQL (policy engine)

Held-out set: 84 attacks and 66 benign queries, written before the policy was tuned against them (a separate development set of 111 attacks / 65 benign was used to build the rules). The first run is frozen in the result file; the post-fix numbers are therefore no longer strictly held-out.

|  | Attacks contained | Benign queries wrongly blocked |
| --- | --- | --- |
| First run on held-out | 98.8% (83/84), 95% CI 93.6-99.8% | 10.0% (6/60), 95% CI 4.7-20.1% |
| After fixing what that run exposed | 100.0% (84/84), 95% CI 95.6-100.0% | 1.7% (1/60), 95% CI 0.3-8.9% |

What the first run found: window functions, `INTERSECT`/`EXCEPT`, constants beside aggregates and `OR 2 > 1` were mishandled; all were fixed and are covered by regression tests.

### Baselines on the same held-out attacks

| Defence | Attack executed | Returned protected data | Attack stopped |
| --- | --- | --- | --- |
| Read-only database account only | 52.4% (44/84) | 25.0% (21/84) | - |
| Keyword blocklist only | - | - | 30.9% (26/84) |
| SentinelSQL policy engine | - | 0 | 100.0% (84/84) |

Data-exfiltration attacks only: read-only DB alone returned protected data for 51.2% (21/41); keyword filter stopped 4.9% (2/41); policy engine 100.0% (41/41).

_Natural-language attack prompts: pending (needs the live model)._

## 2. PII masking

93 queries over 48 synthetic probe rows, covering cards, SSNs, phones and emails, hidden behind 16 SQL transformations (alias, CASE, CONCAT, REPLACE, CTE, window, UNION ...). Same harness before and after the fixes.

| Metric | Before | After |
| --- | --- | --- |
| Span recall, end to end (policy + masking) | 98.1% (3997/4075) | 100.0% (4075/4075) |
| Span recall, masking alone | 98.1% (3997/4075) | 99.2% (4043/4075) |
| Precision of masked cells | 91.6% (2098/2290) | 100.0% (2107/2107) |
| Harmless cells wrongly masked | 27.6% (192/697) | 0.0% (0/697) |
| Partial-value (fragment) exposure | 5.4% (99/1832) | 1.7% (31/1832) |

The remaining masking-only misses and fragment exposure come from one pattern, `REPLACE()` on free-text notes; the policy engine refuses such queries before they run, which is why end-to-end recall is 100%.

## 3. Audit-log tamper evidence

12 tampering attacks against a 300-event chain.

| Verifier | Attacks detected |
| --- | --- |
| Original | 66.7% (8/12) |
| Current (hash chain) | 75.0% (9/12) |
| Current + external anchor | 100.0% (12/12) |

Without an anchor, a chain cannot detect deletion of its newest or oldest rows or a full rewrite; undetectable without anchor: delete_newest_rows, delete_oldest_rows, rewrite_whole_chain.

Cost: write p50 2.803 ms (p99 3.943 ms), verifying 10,000 rows takes 87.7 ms.

## 4. Semantic cache safety

62 labelled question pairs (24 paraphrases that should hit, 38 near-identical questions that must not). Embedding model: gemini:models/gemini-embedding-001@768. Threshold 0.92.

| Policy | Wrong-hit rate (lower is better) | Legitimate-hit rate |
| --- | --- | --- |
| Similarity only | 63.2% (24/38), 95% CI 47.3-76.6% | 87.5% (21/24), 95% CI 69.0-95.7% |
| Similarity + constraint guards | 2.6% (1/38), 95% CI 0.5-13.5% | 54.2% (13/24), 95% CI 35.1-72.1% |

| Kind of difference | Pairs | Wrong hits: similarity only | Wrong hits: guarded | Mean similarity |
| --- | --- | --- | --- | --- |
| aggregate | 4 | 2 | 0 | 0.921 |
| comparison | 4 | 3 | 0 | 0.937 |
| date | 4 | 4 | 0 | 0.953 |
| entity | 5 | 3 | 0 | 0.93 |
| entity_lowercase | 3 | 0 | 0 | 0.894 |
| negation | 4 | 2 | 0 | 0.918 |
| number | 4 | 4 | 0 | 0.97 |
| ranking | 3 | 2 | 0 | 0.927 |
| relative_time | 3 | 3 | 0 | 0.981 |
| subject | 4 | 1 | 1 | 0.888 |

The real cache class agreed with the analytical policy on all but 14 of 62 pairs.

## 5. Schema retrieval

### Pool: dev (81 tables, 200 questions)

- All gold tables retrieved (product default: top 3 plus foreign-key closure): 99.5% (199/200), 95% CI 97.2-99.9%
- Multi-table questions: 100.0% (105/105), 95% CI 96.5-100.0%
- Mean tables in prompt: 4.84 (5.98% of the pool)
- Prompt tokens: 3930 for the full schema vs 192.4 retrieved (95.1% smaller, gemini-api)

## Pending suites

Not yet run to completion: spider, ablation, latency.

## Limitations

- All end-to-end results use SQLite and the 21-table demo database; PostgreSQL and MySQL are untested.
- The held-out security set stops being held-out once its failures are fixed; the frozen first run is reported alongside.
- Spider questions are a sample, and the model is non-deterministic; intervals reflect sampling, not model variance.
- PII probes are synthetic values; real data may contain formats not represented.
