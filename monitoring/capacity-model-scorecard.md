# Capacity Model Scorecard

**Generated**: 2026-02-28 16:31:29

## Model Selection Summary

| Metric | Model | Train R² | Holdout MAPE | Holdout RMSE | Holdout R² | Confidence | Valid Range |
|--------|-------|----------|-------------|-------------|-----------|------------|-------------|
| apiserverErrorRate | piecewise_linear | 0.2915 | — | — | — | low | 6,513 – 48,035 |
| apiserverInflightRequests | log_linear | 0.1180 | — | — | — | low | 6,513 – 48,035 |
| apiserverLatencyP50 | log_linear | 0.1445 | — | — | — | low | 6,513 – 48,035 |
| apiserverLatencyP99 | log_linear | 0.6009 | — | — | — | low | 6,513 – 48,035 |
| apiserverRequestRate | piecewise_linear | 0.4352 | — | — | — | low | 6,513 – 48,035 |
| crossplaneCPU | piecewise_linear | 0.9255 | — | — | — | low | 6,513 – 48,035 |
| crossplaneMemory | piecewise_linear | 0.9822 | — | — | — | low | 6,513 – 48,035 |
| etcdLatencyP50 | piecewise_linear | 0.8440 | — | — | — | low | 6,513 – 48,035 |
| etcdLatencyP99 | log_linear | 0.4149 | — | — | — | low | 6,513 – 48,035 |

## Per-Metric Details

### apiserverErrorRate

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: piecewise_linear
- Equation: `y = -9.090909e-03*x + 5.931136e+01 (x<=6524), -4.388551e-15*x + -1.247145e-10 (x>6524)`
- Training R²: 0.2915
- Residual std: 0.04095

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.2915 | — | — |
| log_linear | 0.0798 | — | — |
| linear | 0.0481 | — | — |

### apiserverInflightRequests

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: log_linear
- Equation: `y = 3.702490e+01 * ln(x) + -3.047945e+02`
- Training R²: 0.1180
- Residual std: 66.86

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| log_linear | 0.1180 | — | — |
| power_law | 0.0930 | — | — |
| piecewise_linear | 0.0773 | — | — |
| linear | 0.0749 | — | — |

### apiserverLatencyP50

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: log_linear
- Equation: `y = 1.758197e-01 * ln(x) + -1.493390e+00`
- Training R²: 0.1445
- Residual std: 0.2826

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.7222 | — | — |
| log_linear | 0.1445 | — | — |
| power_law | 0.0983 | — | — |
| linear | 0.0627 | — | — |

### apiserverLatencyP99

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: log_linear
- Equation: `y = 1.010954e+00 * ln(x) + -8.310636e+00`
- Training R²: 0.6009
- Residual std: 0.5443

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.8297 | — | — |
| log_linear | 0.6009 | — | — |
| power_law | 0.5184 | — | — |
| linear | 0.4453 | — | — |

### apiserverRequestRate

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: piecewise_linear
- Equation: `y = 1.553926e-02*x + -1.918795e+01 (x<=16208), -2.099710e-03*x + 2.667044e+02 (x>16208)`
- Training R²: 0.4352
- Residual std: 55.63

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.4352 | — | — |
| log_linear | 0.1745 | — | — |
| power_law | 0.1489 | — | — |
| linear | 0.0825 | — | — |

### crossplaneCPU

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: piecewise_linear
- Equation: `y = 8.765912e-05*x + -4.105265e-01 (x<=15827), 8.712951e-06*x + 8.389544e-01 (x>15827)`
- Training R²: 0.9255
- Residual std: 0.1054

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.9255 | — | — |
| log_linear | 0.8516 | — | — |
| power_law | 0.7608 | — | — |
| linear | 0.6977 | — | — |

### crossplaneMemory

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: piecewise_linear
- Equation: `y = 1.056432e+05*x + -1.131568e+07 (x<=26361), 1.854968e+04*x + 2.284543e+09 (x>26361)`
- Training R²: 0.9822
- Residual std: 1.228e+08

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.9822 | — | — |
| log_linear | 0.9816 | — | — |
| power_law | 0.9335 | — | — |
| linear | 0.8951 | — | — |

### etcdLatencyP50

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: piecewise_linear
- Equation: `y = 8.980524e-07*x + -9.097797e-04 (x<=21370), -1.783597e-07*x + 2.209306e-02 (x>21370)`
- Training R²: 0.8440
- Residual std: 0.001831

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.8440 | — | — |
| log_linear | 0.6050 | — | — |
| power_law | 0.5230 | — | — |
| linear | 0.3992 | — | — |

### etcdLatencyP99

- **Fit date**: 2026-02-28
- **Confidence**: low
- **Valid range**: 6,513 – 48,035 objects

**Selected model**: log_linear
- Equation: `y = 1.232885e-01 * ln(x) + -1.033989e+00`
- Training R²: 0.4149
- Residual std: 0.09671

**All candidates evaluated**:

| Model | Train R² | Holdout MAPE | Holdout RMSE |
|-------|----------|-------------|-------------|
| piecewise_linear | 0.7834 | — | — |
| log_linear | 0.4149 | — | — |
| power_law | 0.3278 | — | — |
| linear | 0.2622 | — | — |

## Change Log

| Date | Version | Change |
|------|---------|--------|
| 2026-02-28 | 1.0 | Initial model selection with holdout validation |
