import numpy as np
import pandas as pd

MAX_CDF_POINTS = 120


def to_numeric_clean(series):
    return pd.to_numeric(series, errors="coerce").dropna().to_numpy()


def cumulative_distribution_full(values):
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    return unique_vals, np.cumsum(counts) / values.size * 100.0


def downsample_cdf(xs, ys, max_points=MAX_CDF_POINTS):
    if xs.size <= max_points:
        return xs, ys
    idx = np.unique(np.linspace(0, xs.size - 1, max_points).astype(int))
    return xs[idx], ys[idx]


def cumulative_distribution(values, max_points=MAX_CDF_POINTS):
    return downsample_cdf(*cumulative_distribution_full(values), max_points)
