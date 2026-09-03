# Blind image benchmark

Authorized ceiling: $2.00, with no account top-ups and no retries unless a
provider returned an inaccessible result. The benchmark uses one real slide
prompt and one real YouTube-thumbnail prompt from `prompts.json`.

## Candidate economics

The historical comparison column assumes the same image count as $1,050 of
Gemini Pro 1K/2K requests at $0.134 each. It is directional; the blind quality
ranking and accepted-output retry rate decide the actual winner.

| Candidate | Cost per output | Reduction vs $0.134 | Same-count historical cost |
|---|---:|---:|---:|
| GPT Image 2 low, measured average | $0.0057 | 95.8% | $44 |
| FLUX.2 Dev | $0.0154 | 88.5% | $121 |
| FLUX.2 Pro | $0.0300 | 77.6% | $235 |
| Grok Imagine 2 low 1K | $0.0400 | 70.1% | $313 |
| Qwen Image 2 | $0.0400 | 70.1% | $313 |
| GPT Image 2 medium, measured average | $0.0421 | 68.6% | $330 |
| Gemini Flash through Together | $0.0500 | 62.7% | $392 |
| Direct Gemini Flash 1K | $0.0670 | 50.0% | $525 |
| Historical Gemini Pro 1K/2K control | $0.1340 | baseline | $1,050 |

## Completed calls

- GPT Image 2 low and medium: slide plus thumbnail. All four outputs rendered
  the required headline text correctly. Exact total cost: $0.095510.
- Grok Imagine 2 low 1K: slide plus thumbnail. Both delivered outputs rendered
  the required text correctly. Exact delivered-output cost: $0.080000.
- One earlier Grok slide generation succeeded but its temporary URL returned
  HTTP 403 before download. That request cost $0.040000 and is counted as one
  retry/failure in the benchmark total.
- Total spent so far: $0.215510.
- Together candidates are pending a one-day benchmark key. Existing account
  credit is sufficient; no funding or top-up is required.

Private model-to-label mappings and generated PNGs are intentionally ignored by
Git. They remain local until the blind ranking is complete.
