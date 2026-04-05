"""Pure-Python LP solver — Big-M simplex method.

Provides ``linprog()`` with a scipy-compatible interface for solving
the battery scheduling LP without any external dependencies.

The implementation uses the full-tableau Big-M simplex method with
Bland's rule for pivot selection (prevents cycling).  For the typical
battery scheduling problem (~120 variables, ~150 constraints) it
converges in < 200 ms on commodity hardware.

Usage
-----
::

    from .lp_solver import linprog

    result = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds)
    if result.success:
        x = result.x
        cost = result.fun
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ── Numerical tolerances ────────────────────────────────────────────
_PIVOT_TOL = 1e-10   # minimum acceptable pivot element
_OPT_TOL = 1e-9      # optimality tolerance for reduced costs
_FEAS_TOL = 1e-6     # feasibility tolerance for artificial check
_BIG_M = 1e7         # penalty for artificial variables


@dataclass
class LPResult:
    """Optimisation result — mirrors ``scipy.optimize.OptimizeResult``."""

    x: list[float]
    fun: float
    success: bool
    message: str = ""


def linprog(
    c: list[float],
    A_ub: list[list[float]] | None = None,
    b_ub: list[float] | None = None,
    A_eq: list[list[float]] | None = None,
    b_eq: list[float] | None = None,
    bounds: list[tuple[float, float | None]] | None = None,
    max_iter: int = 20000,
) -> LPResult:
    """Minimise ``c @ x`` subject to linear constraints.

    Parameters
    ----------
    c : list[float]
        Objective coefficients (length *n*).
    A_ub, b_ub : optional
        Inequality constraints ``A_ub @ x ≤ b_ub``.
    A_eq, b_eq : optional
        Equality constraints ``A_eq @ x = b_eq``.
    bounds : optional
        ``(lo, hi)`` per variable.  *lo* must be 0 or ``None``
        (default).  *hi* = ``None`` means unbounded above.
    max_iter : int
        Maximum simplex pivots (default 20 000).

    Returns
    -------
    LPResult
        ``.x`` — solution vector, ``.fun`` — objective value,
        ``.success`` — ``True`` if optimal solution found.
    """
    n_orig = len(c)

    # ── 1. Convert finite upper bounds to ≤ constraints ──────────
    ub_rows: list[list[float]] = []
    ub_rhs: list[float] = []
    if bounds:
        for j in range(n_orig):
            _lo, hi = bounds[j]
            if hi is not None:
                row = [0.0] * n_orig
                row[j] = 1.0
                ub_rows.append(row)
                ub_rhs.append(float(hi))

    # ── 2. Merge all inequality constraints ──────────────────────
    all_ub_A: list[list[float]] = []
    all_ub_b: list[float] = []
    if A_ub is not None and b_ub is not None:
        for i in range(len(b_ub)):
            all_ub_A.append(list(A_ub[i]))
            all_ub_b.append(float(b_ub[i]))
    all_ub_A.extend(ub_rows)
    all_ub_b.extend(ub_rhs)
    m_ub = len(all_ub_A)

    # ── 3. Process equality constraints ──────────────────────────
    all_eq_A: list[list[float]] = []
    all_eq_b: list[float] = []
    if A_eq is not None and b_eq is not None:
        for i in range(len(b_eq)):
            all_eq_A.append(list(A_eq[i]))
            all_eq_b.append(float(b_eq[i]))
    m_eq = len(all_eq_A)

    # Ensure b ≥ 0 for equalities (negate row if needed)
    for i in range(m_eq):
        if all_eq_b[i] < 0:
            all_eq_A[i] = [-v for v in all_eq_A[i]]
            all_eq_b[i] = -all_eq_b[i]

    # ── 4. Dimensions ────────────────────────────────────────────
    #   Rows:    equalities (m_eq) then inequalities (m_ub)
    #   Columns: original (n_orig) | slack (m_ub) | artificial (m_eq)
    n_slack = m_ub
    n_art = m_eq
    m = m_eq + m_ub
    n_total = n_orig + n_slack + n_art
    ncols = n_total + 1  # last column is RHS

    if m == 0:
        # No constraints — all variables at their lower bound (0)
        return LPResult(x=[0.0] * n_orig, fun=0.0, success=True)

    # ── 5. Build tableau ─────────────────────────────────────────
    # Row layout: [constraint coefficients ... | RHS]
    # Last row is the objective.
    tab: list[list[float]] = [[0.0] * ncols for _ in range(m + 1)]
    basis: list[int] = [0] * m

    # Equality rows → artificial variables in basis
    for i in range(m_eq):
        for j in range(n_orig):
            tab[i][j] = all_eq_A[i][j]
        art_col = n_orig + n_slack + i
        tab[i][art_col] = 1.0
        tab[i][-1] = all_eq_b[i]
        basis[i] = art_col

    # Inequality rows → slack variables in basis
    for i in range(m_ub):
        ri = m_eq + i
        for j in range(n_orig):
            tab[ri][j] = all_ub_A[i][j]
        slack_col = n_orig + i
        tab[ri][slack_col] = 1.0
        tab[ri][-1] = all_ub_b[i]
        basis[ri] = slack_col

    # Objective row: original costs + Big-M for artificials
    obj = tab[m]
    for j in range(n_orig):
        obj[j] = c[j]
    for k in range(n_art):
        obj[n_orig + n_slack + k] = _BIG_M

    # Reduce objective for basic artificial variables:
    #   reduced_cost[j] = c_j − c_B · B⁻¹ · a_j
    # Since artificials are basic with cost M:
    for i in range(m_eq):
        for j in range(ncols):
            obj[j] -= _BIG_M * tab[i][j]

    # ── 6. Simplex pivots ────────────────────────────────────────
    for _iter in range(max_iter):
        # Bland's rule: pick first variable with negative reduced cost
        pivot_col = -1
        for j in range(n_total):
            if obj[j] < -_OPT_TOL:
                pivot_col = j
                break
        if pivot_col == -1:
            break  # optimal

        # Min-ratio test to find leaving variable
        pivot_row = -1
        min_ratio = float("inf")
        for i in range(m):
            aij = tab[i][pivot_col]
            if aij > _PIVOT_TOL:
                ratio = tab[i][-1] / aij
                if ratio < min_ratio - _PIVOT_TOL:
                    min_ratio = ratio
                    pivot_row = i

        if pivot_row == -1:
            return LPResult(
                x=[], fun=0.0, success=False, message="LP unbounded"
            )

        # Pivot operation
        pv = tab[pivot_row][pivot_col]
        inv_pv = 1.0 / pv
        pr = tab[pivot_row]
        for j in range(ncols):
            pr[j] *= inv_pv

        for i in range(m + 1):
            if i != pivot_row:
                factor = tab[i][pivot_col]
                if abs(factor) > 1e-15:
                    ri = tab[i]
                    for j in range(ncols):
                        ri[j] -= factor * pr[j]

        basis[pivot_row] = pivot_col
    else:
        return LPResult(
            x=[], fun=0.0, success=False,
            message=f"Simplex did not converge in {max_iter} iterations",
        )

    # ── 7. Feasibility check ─────────────────────────────────────
    for i in range(m):
        if basis[i] >= n_orig + n_slack and tab[i][-1] > _FEAS_TOL:
            return LPResult(
                x=[], fun=0.0, success=False,
                message="LP infeasible (artificial variable in basis)",
            )

    # ── 8. Extract solution ──────────────────────────────────────
    x = [0.0] * n_orig
    for i in range(m):
        col = basis[i]
        if col < n_orig:
            x[col] = max(0.0, tab[i][-1])  # clamp tiny negatives

    fun = sum(c[j] * x[j] for j in range(n_orig))
    return LPResult(x=x, fun=fun, success=True, message="Optimal")
