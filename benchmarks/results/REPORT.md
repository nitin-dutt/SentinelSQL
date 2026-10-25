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

### Attacks submitted as natural language (LLM in the loop)

55 attack prompts and 39 benign prompts through the full pipeline with the live model. The baseline is the same model with an instructions-only guardrail, a read-only database, no policy engine and no masking.

|  | Harmful outcome |
| --- | --- |
| SentinelSQL | 0.0% (0/55), 95% CI 0.0-6.5% |
| Prompt-only baseline | 0.0% (0/55), 95% CI 0.0-6.5% |

How the attacks ended: 13 stopped by the intent filter, 26 sent to human review, 4 the model refused or produced unparseable output, 12 executed as harmless masked reads.

Benign prompts: false-block rate 0.0% (0/35), 95% CI 0.0-9.9%; answered 94.3% (33/35), 95% CI 81.4-98.4%; 2 errors where the model referenced a column that does not exist; sensitive-table questions sent to review 100.0% (4/4), 95% CI 51.0-100.0%; protected data leaked: 0.

Note: the baseline is also at zero here, so these English prompts do not show protection beyond the model's own behaviour. The gateway's value is demonstrated by the SQL-level attacks above (what a tricked model or an MCP agent can submit).

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
| Similarity only | 21.1% (8/38), 95% CI 11.1-36.4% | 33.3% (8/24), 95% CI 18.0-53.3% |
| Similarity + constraint guards | 0.0% (0/38), 95% CI 0.0-9.2% | 29.2% (7/24), 95% CI 14.9-49.2% |

| Kind of difference | Pairs | Wrong hits: similarity only | Wrong hits: guarded | Mean similarity |
| --- | --- | --- | --- | --- |
| aggregate | 4 | 0 | 0 | 0.834 |
| comparison | 4 | 2 | 0 | 0.905 |
| date | 4 | 2 | 0 | 0.897 |
| entity | 5 | 0 | 0 | 0.859 |
| entity_lowercase | 3 | 0 | 0 | 0.841 |
| negation | 4 | 0 | 0 | 0.839 |
| number | 4 | 2 | 0 | 0.927 |
| ranking | 3 | 0 | 0 | 0.865 |
| relative_time | 3 | 2 | 0 | 0.921 |
| subject | 4 | 0 | 0 | 0.778 |

The real cache class agreed with the analytical policy on all but 0 of 62 pairs.

## 5. Schema retrieval

### Pool: dev (81 tables, 200 questions)

- All gold tables retrieved (product default: top 3 plus foreign-key closure): 100.0% (200/200), 95% CI 98.1-100.0%
- Multi-table questions: 100.0% (105/105), 95% CI 96.5-100.0%
- Mean tables in prompt: 4.82 (5.95% of the pool)
- Prompt tokens: 3930 for the full schema vs 198.1 retrieved (94.96% smaller, gemini-api)

## 6. Text-to-SQL accuracy (Spider dev, execution accuracy)

200 questions sampled across difficulty tiers. Scoring executes the generated and the gold query and compares result sets (column order and row order ignored unless the gold has ORDER BY). Official hardness classifier ported and verified against the published split.

| Arm | Accuracy (unweighted) | Reweighted to dev mix | easy | medium | hard | extra |
| --- | --- | --- | --- | --- | --- | --- |
| Model alone (no gateway) | 87.5% (175/200) | 90.26% | 100.0% (50/50) | 94.0% (47/50) | 88.0% (44/50) | 68.0% (34/50) |
| Through gateway, as shipped | 82.5% (165/200) | 85.47% | 94.0% (47/50) | 90.0% (45/50) | 86.0% (43/50) | 60.0% (30/50) |
| Through gateway, row cap ignored | 85.5% (171/200) | 87.97% | 94.0% (47/50) | 92.0% (46/50) | 90.0% (45/50) | 66.0% (33/50) |

Paired comparison, model alone vs gateway: raw-only correct 17, gateway-only correct 7, McNemar p = 0.06391.

Gateway outcomes: {'executed': 196, 'held_for_review': 4}. Where the gateway lost a correct answer: {'held_for_review': 2, 'different_sql_from_two_stage_prompt': 10, 'row_limit_applied': 5}.

Hold-out sample (fresh questions that exclude the 200 above; the 200 were used to tune the prompt): n=100 (complete). Model alone 84.0% (84/100), 95% CI 75.6-89.9%; gateway 80.0% (80/100), 95% CI 71.1-86.7%. If the status says paused, the hardest tier is missing and the figure is optimistic.

## 8. Latency

30 cold requests, 30 answered. Medians and 95th percentiles in milliseconds; speedup is against the measured cold median.

| Path | p50 ms | p95 ms | Speedup |
| --- | --- | --- | --- |
| Cold request, end to end (cache off, live model) | 3449.3 | 11091.5 | 1x |
|   of which model calls | 3438.5 | 11080.19 |  |
|   of which SentinelSQL code (validate, execute, mask) | 11.05 | 21.565 |  |
| Exact-match cache hit (HTTP) | 3.306 | 3.792 | 1043.3x |
| Similarity cache hit (HTTP, live embedding) | 639.42 | 733.098 | 5.4x |

Median time per stage (ms): {'intent_node': 2200.5, 'sql_generation_node': 1255.6, 'validation_node': 2.1, 'execution_node': 1.6}

## Pending suites

Not yet run to completion: ablation.

## 9. Few-shot examples and the repair step (Spider, paired with the baseline)

| Comparison | n | Baseline | Variant | Points | Fixed / broken | McNemar p | Repairs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Hold-out + few-shot | 100 | 80.0% (80/100) | 80.0% (80/100) | +0.0 | 1 / 1 | 1.0 | 0 |
| Hold-out + repair | 100 | 80.0% (80/100) | 80.0% (80/100) | +0.0 | 0 / 0 | 1.0 | 0 |
| Hold-out + both | 100 | 80.0% (80/100) | 80.0% (80/100) | +0.0 | 1 / 1 | 1.0 | 0 |
| Main + few-shot | 200 | 82.5% (165/200) | 81.0% (162/200) | -1.5 | 3 / 6 | 0.5078 | 0 |
| Main + repair | 200 | 82.5% (165/200) | 82.5% (165/200) | +0.0 | 0 / 0 | 1.0 | 1 |

Both ideas were specified before the hold-out results were seen. Neither changes accuracy.

## Limitations

- All end-to-end results use SQLite and the 21-table demo database; PostgreSQL and MySQL are untested.
- The held-out security set stops being held-out once its failures are fixed; the frozen first run is reported alongside.
- Spider questions are a sample, and the model is non-deterministic; intervals reflect sampling, not model variance.
- PII probes are synthetic values; real data may contain formats not represented.
