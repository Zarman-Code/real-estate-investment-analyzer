"""Flux de trésorerie trimestriels, dette, TRI et multiple (plan, section 5.5).

Périodes : T0 = jour de l'acquisition, puis trimestres 1..N (flux datés en fin de trimestre).
Dette (crédit de restructuration) :
  - ltv_basis = "total_cost" : chaque décaissement net est financé à ltv par la dette, le reste par les fonds propres ;
  - ltv_basis = "price"      : la dette finance ltv x prix d'acquisition à T0, tout le reste en fonds propres ;
  - intérêts trimestriels = encours x taux / 4, CAPITALISÉS ; encours remboursé à la cession ;
  - commission d'arrangement payée à T0 par les fonds propres (évite une circularité).
Les excédents de trésorerie avant la cession (loyers > coûts) sont distribués aux fonds propres.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def npv(rate: float, flows: list[float]) -> float:
    return sum(cf / (1 + rate) ** k for k, cf in enumerate(flows))


def irr(flows: list[float], lo: float = -0.99, hi: float = 10.0, tol: float = 1e-12, max_iter: int = 500) -> float | None:
    """TRI par période (dichotomie). None si les flux ne changent pas de signe ou si la racine n'est pas encadrée."""
    if not (any(f < 0 for f in flows) and any(f > 0 for f in flows)):
        return None
    f_lo, f_hi = npv(lo, flows), npv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = npv(mid, flows)
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def annualise(quarterly_rate: float | None) -> float | None:
    return None if quarterly_rate is None else (1 + quarterly_rate) ** 4 - 1


def equity_multiple(flows: list[float]) -> float | None:
    out = -sum(f for f in flows if f < 0)
    return None if out == 0 else sum(f for f in flows if f > 0) / out


def to_quarters(monthly: list[float], n_quarters: int) -> list[float]:
    q = [0.0] * n_quarters
    for m, v in enumerate(monthly):
        q[m // 3] += v
    return q


@dataclass
class DebtSchedule:
    draws: list[float] = field(default_factory=list)       # par période (T0..TN)
    interest: list[float] = field(default_factory=list)
    balance: list[float] = field(default_factory=list)     # encours en fin de période
    repayment: float = 0.0
    arrangement_fee: float = 0.0

    @property
    def max_balance(self) -> float:
        return max(self.balance, default=0.0)


def debt_schedule(unlevered: list[float], exit_proceeds: float, ltv: float, basis: str, price: float,
                  rate: float, arrangement_fee_pct: float) -> DebtSchedule:
    """`unlevered` : flux d'exploitation et d'investissement par période, HORS produit de cession."""
    n = len(unlevered)
    draws = [0.0] * n
    if basis == "total_cost":
        for k, cf in enumerate(unlevered):
            if cf < 0:
                draws[k] = -cf * ltv
    elif basis == "price":
        draws[0] = price * ltv
    else:
        raise ValueError(f"ltv_basis inconnu : {basis}")
    interest, balance, bal = [0.0] * n, [0.0] * n, 0.0
    for k in range(n):
        if k > 0:
            interest[k] = bal * rate / 4
        bal += interest[k] + draws[k]
        balance[k] = bal
    fee = arrangement_fee_pct * sum(draws)
    return DebtSchedule(draws, interest, balance, repayment=bal, arrangement_fee=fee)


def levered_flows(unlevered: list[float], exit_proceeds: float, debt: DebtSchedule) -> list[float]:
    eq = [cf + d for cf, d in zip(unlevered, debt.draws)]
    eq[0] -= debt.arrangement_fee
    eq[-1] += exit_proceeds - debt.repayment
    return eq
