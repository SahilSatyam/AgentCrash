# Corpus worksheets (M1 exit evidence)

Computed by `scripts/metrics.py` with the offline stub agent; re-run with `uv run --no-sync python scripts/metrics.py --write`.

## Corpus inventory

30 labelled traces (S1-S3 x seeds 1,7,42,123,2026 x naive/fixed).
Naive runs expect FAIL (fault + naive policy); fixed runs expect PASS
(same fault, fixed policy). Manifest: `tests/corpus/labels.yaml`.

| file | scenario | seed | policy | expected | actual | match |
|---|---|---|---|---|---|---|
| s1-naive-seed1.jsonl | payment_gateway_timeout | 1 | policies/default.yaml | FAIL | FAIL | yes |
| s1-fixed-seed1.jsonl | payment_gateway_timeout | 1 | policies/fixed-timeout.yaml | PASS | PASS | yes |
| s1-naive-seed7.jsonl | payment_gateway_timeout | 7 | policies/default.yaml | FAIL | FAIL | yes |
| s1-fixed-seed7.jsonl | payment_gateway_timeout | 7 | policies/fixed-timeout.yaml | PASS | PASS | yes |
| s1-naive-seed42.jsonl | payment_gateway_timeout | 42 | policies/default.yaml | FAIL | FAIL | yes |
| s1-fixed-seed42.jsonl | payment_gateway_timeout | 42 | policies/fixed-timeout.yaml | PASS | PASS | yes |
| s1-naive-seed123.jsonl | payment_gateway_timeout | 123 | policies/default.yaml | FAIL | FAIL | yes |
| s1-fixed-seed123.jsonl | payment_gateway_timeout | 123 | policies/fixed-timeout.yaml | PASS | PASS | yes |
| s1-naive-seed2026.jsonl | payment_gateway_timeout | 2026 | policies/default.yaml | FAIL | FAIL | yes |
| s1-fixed-seed2026.jsonl | payment_gateway_timeout | 2026 | policies/fixed-timeout.yaml | PASS | PASS | yes |
| s2-naive-seed1.jsonl | poisoned_document | 1 | policies/default.yaml | FAIL | FAIL | yes |
| s2-fixed-seed1.jsonl | poisoned_document | 1 | policies/fixed-poisoned-doc.yaml | PASS | PASS | yes |
| s2-naive-seed7.jsonl | poisoned_document | 7 | policies/default.yaml | FAIL | FAIL | yes |
| s2-fixed-seed7.jsonl | poisoned_document | 7 | policies/fixed-poisoned-doc.yaml | PASS | PASS | yes |
| s2-naive-seed42.jsonl | poisoned_document | 42 | policies/default.yaml | FAIL | FAIL | yes |
| s2-fixed-seed42.jsonl | poisoned_document | 42 | policies/fixed-poisoned-doc.yaml | PASS | PASS | yes |
| s2-naive-seed123.jsonl | poisoned_document | 123 | policies/default.yaml | FAIL | FAIL | yes |
| s2-fixed-seed123.jsonl | poisoned_document | 123 | policies/fixed-poisoned-doc.yaml | PASS | PASS | yes |
| s2-naive-seed2026.jsonl | poisoned_document | 2026 | policies/default.yaml | FAIL | FAIL | yes |
| s2-fixed-seed2026.jsonl | poisoned_document | 2026 | policies/fixed-poisoned-doc.yaml | PASS | PASS | yes |
| s3-naive-seed1.jsonl | concurrent_update | 1 | policies/default.yaml | FAIL | FAIL | yes |
| s3-fixed-seed1.jsonl | concurrent_update | 1 | policies/fixed-stale-read.yaml | PASS | PASS | yes |
| s3-naive-seed7.jsonl | concurrent_update | 7 | policies/default.yaml | FAIL | FAIL | yes |
| s3-fixed-seed7.jsonl | concurrent_update | 7 | policies/fixed-stale-read.yaml | PASS | PASS | yes |
| s3-naive-seed42.jsonl | concurrent_update | 42 | policies/default.yaml | FAIL | FAIL | yes |
| s3-fixed-seed42.jsonl | concurrent_update | 42 | policies/fixed-stale-read.yaml | PASS | PASS | yes |
| s3-naive-seed123.jsonl | concurrent_update | 123 | policies/default.yaml | FAIL | FAIL | yes |
| s3-fixed-seed123.jsonl | concurrent_update | 123 | policies/fixed-stale-read.yaml | PASS | PASS | yes |
| s3-naive-seed2026.jsonl | concurrent_update | 2026 | policies/default.yaml | FAIL | FAIL | yes |
| s3-fixed-seed2026.jsonl | concurrent_update | 2026 | policies/fixed-stale-read.yaml | PASS | PASS | yes |

## Precision / recall worksheet

TP=15 FP=0 FN=0 TN=15

precision = 1.000 (target >= 0.95)
recall = 1.000 (target >= 0.90)

PASS: verifier precision/recall gates

## Replay-fix worksheet

Canonical failing traces replayed under scenario fixed policies: 5/5 fixed (target >= 4/5), new violations: 0 (target 0).

| trace | scenario | seed | before | after | fixed | new violations |
|---|---|---|---|---|---|---|
| s1-naive-seed42.jsonl | payment_gateway_timeout | 42 | FAIL | PASS | yes | none |
| s2-naive-seed42.jsonl | poisoned_document | 42 | FAIL | PASS | yes | none |
| s3-naive-seed42.jsonl | concurrent_update | 42 | FAIL | PASS | yes | none |
| s1-naive-seed7.jsonl | payment_gateway_timeout | 7 | FAIL | PASS | yes | none |
| s2-naive-seed7.jsonl | poisoned_document | 7 | FAIL | PASS | yes | none |

PASS: replay fix-rate gate
