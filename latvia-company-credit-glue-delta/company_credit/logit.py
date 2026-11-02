"""Logistic regression by Newton's method, with p-values clustered on the company.

Written out in numpy rather than imported, so the model job needs nothing that Glue 5.0 does not
ship: the image carries numpy and pandas, and neither statsmodels nor scipy. A test fits the same
data with statsmodels and requires the coefficients and clustered standard errors to agree.

**Why clustered.** A company appears once a year for up to seventeen years, so its rows are not
independent draws; an ordinary standard error treats seventeen looks at one firm as seventeen
firms and comes out too small. The sandwich sums the score within each company first:

    V = H^-1 (sum over companies of s_g s_g') H^-1 * G/(G-1) * (N-1)/(N-K)

with H the information matrix, s_g a company's summed score, G companies, N rows, K terms --
the small-sample factor statsmodels applies by default.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class LogitResult:
    names: list
    params: np.ndarray
    bse: np.ndarray
    iterations: int
    log_likelihood: float
    n: int
    clusters: int

    @property
    def z(self):
        return self.params / self.bse

    @property
    def pvalues(self):
        return np.array([math.erfc(abs(z) / math.sqrt(2.0)) for z in self.z])

    def table(self):
        return pd.DataFrame({"term": self.names, "coef": self.params, "std_err": self.bse,
                             "z": self.z, "p_value": self.pvalues})

    def linear(self, X):
        return np.asarray(X, dtype=float) @ self.params


def sigmoid(eta):
    return 1.0 / (1.0 + np.exp(-np.asarray(eta, dtype=float)))


def fit(X, y, groups=None, names=None, max_iter=100, tol=1e-10):
    """Maximum likelihood logit. ``X`` must already hold the constant column."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, k = X.shape
    beta = np.zeros(k)
    rate = min(max(y.mean(), 1e-6), 1 - 1e-6)
    beta[0] = math.log(rate / (1 - rate))  # start at the base rate: column 0 is the constant
    for iteration in range(1, max_iter + 1):
        p = sigmoid(X @ beta)
        w = p * (1 - p)
        H = X.T @ (X * w[:, None])
        step = np.linalg.solve(H, X.T @ (y - p))
        beta += step
        if np.max(np.abs(step)) < tol:
            break
    else:
        raise RuntimeError(f"logit did not converge in {max_iter} iterations")
    p = sigmoid(X @ beta)
    H = X.T @ (X * (p * (1 - p))[:, None])
    H_inv = np.linalg.inv(H)
    if groups is None:
        cov, clusters = H_inv, n
    else:
        scores = pd.DataFrame(X * (y - p)[:, None]).groupby(np.asarray(groups)).sum().to_numpy()
        clusters = scores.shape[0]
        meat = scores.T @ scores
        cov = H_inv @ meat @ H_inv * clusters / (clusters - 1) * (n - 1) / (n - k)
    loglik = float(np.sum(y * np.log(np.clip(p, 1e-300, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-300, 1))))
    return LogitResult(names=list(names) if names is not None else [f"x{i}" for i in range(k)],
                       params=beta, bse=np.sqrt(np.diag(cov)), iterations=iteration,
                       log_likelihood=loglik, n=n, clusters=clusters)


def intercept_shift(eta, target_rate, tol=1e-12):
    """The constant to add to every log-odds so their mean probability equals ``target_rate``."""
    eta = np.asarray(eta, dtype=float)
    shift = 0.0
    for _ in range(100):
        p = sigmoid(eta + shift)
        gap = p.mean() - target_rate
        if abs(gap) < tol:
            break
        shift -= gap / max((p * (1 - p)).mean(), 1e-12)
    return shift
