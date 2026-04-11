# Waves Research Scripts

This folder contains research, backtesting, and experimental analysis scripts for the `cv_rank.waves` forecasting system.

These scripts are intentionally separate from the main product entrypoints:

- production training: `scripts/train_waves_v2.py`
- production prediction: `scripts/predict_upcoming_v2.py`

Use this folder for:

- historical backtests
- calibration experiments
- Stage 0 bakeoffs
- person-level show-up research
- feature audits and validation

Important:

- most scripts assume repo root is `/Users/nickita/cv-rank`
- most scripts read `.env` from repo root
- many scripts read derived artifacts under `results/`
- several scripts query the platform DB directly, so `PLATFORM_DATABASE_URL` must be set

Suggested starting points:

- `bakeoff_stage0_models.py`
  Source-of-truth comparison of Stage 0 signup forecasters on temporal splits.
- `backtest_horizon.py`
  Earlier horizon-aware attendance backtest.
- `validate_engagement_stats.py`
  Statistical validation of signup-timing engagement differences.
- `predict_single_person_show.py`
  Person-level show prediction research and backtests.

This folder is for experimentation and reproducibility, not the default shipped CLI path.
