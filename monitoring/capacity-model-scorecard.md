# Capacity Model Scorecard

**Generated**: 2026-03-03 13:52:00

## Model Selection Summary

| Metric | Model | Train R² | Holdout MAPE | Holdout RMSE | Holdout R² | Confidence | Valid Range |
|--------|-------|----------|-------------|-------------|-----------|------------|-------------|
| api_p99 | linear | 0.3658 | 0.6% | 0.006114 | -396.6304 | low | 18,000 – 96,000 |
| cpu | saturating_exp | 0.9807 | 3.1% | 0.121 | -2.4657 | low | 18,000 – 96,000 |
| etcd_db_size | log_linear | 0.8961 | 15.2% | 5.422e+07 | -1.3792 | low | 18,000 – 104,000 |
| etcd_p99 | piecewise_linear | 0.6381 | 16.0% | 0.007675 | -593.0033 | low | 18,000 – 96,000 |
| memory | power_law | 0.9389 | 1.4% | 9.285e+07 | -0.2930 | low | 18,000 – 96,000 |
| wal_fsync | power_law | 0.5071 | 18.6% | 0.001297 | 0.0151 | low | 18,000 – 104,000 |

## Per-Metric Details

### api_p99

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 96,000 objects

**Selected model**: linear
- Equation: `y = -8.474116e-08 * x + 9.864594e-01`
- Training R²: 0.3658
- Residual std: 0.003111

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.5810 | 1.0% | 0.009801 |
| power_law | 0.4779 | 0.7% | 0.00669 |
| log_linear | 0.4774 | 0.7% | 0.006689 |
| sqrt | 0.4215 | 0.7% | 0.006389 |
| linear | 0.3658 | 0.6% | 0.006114 |
| saturating_exp | 0.0000 | 1.0% | 0.009758 |

### cpu

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 96,000 objects

**Selected model**: saturating_exp
- Equation: `y = 3.941337e+00 * (1 - e^(-3.492491e-05*x)) + -9.581304e-01`
- Training R²: 0.9807
- Residual std: 0.1001

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| saturating_exp | 0.9807 | 3.1% | 0.121 |
| log_linear | 0.9771 | 3.4% | 0.1295 |
| piecewise_linear | 0.9712 | 3.6% | 0.1327 |
| sqrt | 0.9501 | 7.1% | 0.2382 |
| power_law | 0.9425 | 7.2% | 0.2421 |
| linear | 0.9094 | 11.4% | 0.3639 |

### etcd_db_size

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 104,000 objects

**Selected model**: log_linear
- Equation: `y = 1.737650e+08 * ln(x) + -1.637314e+09`
- Training R²: 0.8961
- Residual std: 2.659e+07

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.9368 | 20.6% | 7.921e+07 |
| saturating_exp | 0.9315 | 20.2% | 7.775e+07 |
| power_law | 0.9302 | 21.4% | 8.157e+07 |
| linear | 0.9285 | 23.6% | 8.83e+07 |
| sqrt | 0.9251 | 18.1% | 7.006e+07 |
| log_linear | 0.8961 | 15.2% | 5.422e+07 |

### etcd_p99

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 96,000 objects

**Selected model**: piecewise_linear
- Equation: `y = -2.948389e-07*x + 9.347026e-02 (x<=76000), -5.601497e-07*x + 1.136339e-01 (x>76000)`
- Training R²: 0.6381
- Residual std: 0.00731

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.6381 | 16.0% | 0.007675 |
| linear | 0.6270 | 24.8% | 0.01177 |
| sqrt | 0.6158 | 29.5% | 0.01396 |
| log_linear | 0.5967 | 33.8% | 0.01597 |
| power_law | 0.5872 | 35.5% | 0.01679 |
| saturating_exp | -0.0000 | 56.4% | 0.02667 |

### memory

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 96,000 objects

**Selected model**: power_law
- Equation: `y = 2.739965e+07 * x^0.4756`
- Training R²: 0.9389
- Residual std: 3.258e+08

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| saturating_exp | 0.9625 | 6.0% | 3.996e+08 |
| log_linear | 0.9623 | 1.5% | 1.01e+08 |
| piecewise_linear | 0.9505 | 1.8% | 1.169e+08 |
| power_law | 0.9389 | 1.4% | 9.285e+07 |
| sqrt | 0.9371 | 2.4% | 1.615e+08 |
| linear | 0.8999 | 5.5% | 3.695e+08 |

### wal_fsync

- **Fit date**: 2026-03-03
- **Confidence**: low
- **Valid range**: 18,000 – 104,000 objects

**Selected model**: power_law
- Equation: `y = 3.114058e-01 * x^-0.3341`
- Training R²: 0.5071
- Residual std: 0.001344

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.6653 | — | — |
| linear | 0.6342 | — | — |
| sqrt | 0.5971 | 21.6% | 0.00151 |
| log_linear | 0.5463 | 19.3% | 0.00135 |
| power_law | 0.5071 | 18.6% | 0.001297 |
| saturating_exp | 0.0000 | 21.6% | 0.001585 |

## Change Log

| Date | Version | Change |
|------|---------|--------|
| 2026-03-03 | 1.0 | Initial model selection with holdout validation |
