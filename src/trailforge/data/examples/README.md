# Original offline examples

These original synthetic fixtures exercise bounded execution and evidence validation. Outputs are configured mocks, not bug inference or a model-quality benchmark. No source code is executed.

Run `trailforge run --fixture buggy-python --store runs.sqlite3`, then `trailforge inspect --store runs.sqlite3 --run-id YOUR_RUN_ID`. Run all cases with `trailforge evaluate --suite .`.
