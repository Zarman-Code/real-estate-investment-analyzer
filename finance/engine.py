"""Moteur financier : assemble les modules 1 à 4 et les flux. Fonction pure : DealInputs -> résultats.

Aucun LLM, aucun accès réseau, aucune donnée du deal en dur. Utilisé tel quel par financial_analysis,
max_price, et (phase 6) les sensibilités et scénarios.
"""
from __future__ import annotations

from dataclasses import replace

from finance import cost as C
from finance import profitability as P
from finance import revenue as R
from finance.cashflow import annualise, debt_schedule, equity_multiple, irr, levered_flows, to_quarters
from finance.inputs import DealInputs
from finance.timeline import build_timeline, month_start, quarter_label


def run_model(inp: DealInputs) -> dict:
    tl = build_timeline(inp)
    n, nq = tl.n_months, tl.n_quarters

    # ---------------------------------------------------------------- flux mensuels
    rent_current = R.current_rent_monthly(inp, tl)
    rent_new = R.new_lease_rent_monthly(inp, tl)
    works = C.works_monthly(inp, tl)
    holding = C.holding_monthly(inp, tl)
    nonrec = C.non_recoverable_monthly(inp, tl)
    marketing = C.marketing_monthly(inp, tl)
    operating = [rc + rn - w - h - nr - mk for rc, rn, w, h, nr, mk
                 in zip(rent_current, rent_new, works, holding, nonrec, marketing)]

    # ---------------------------------------------------------------- périodes T0..TN
    acq_fees = C.acquisition_fees(inp)
    unlevered_ex_exit = [-(inp.price + acq_fees)] + to_quarters(operating, nq)
    ev = P.exit_value(inp, tl)
    unlevered = unlevered_ex_exit[:-1] + [unlevered_ex_exit[-1] + ev["net"]]

    debt = debt_schedule(unlevered_ex_exit, ev["net"], inp.ltv, inp.ltv_basis, inp.price,
                         inp.interest_rate, inp.arrangement_fee_pct)
    equity = levered_flows(unlevered_ex_exit, ev["net"], debt)

    # ---------------------------------------------------------------- coût total (module 3)
    capex, fees_c = C.capex_total(inp), C.fees_contingency_total(inp)
    interest_total = sum(debt.interest)
    total_cost = (inp.price + acq_fees + capex + fees_c + sum(holding) + sum(marketing)
                  + interest_total + debt.arrangement_fee)
    # revenus perçus pendant l'opération : affichés à part, NON déduits du coût total
    income_during = sum(rent_current) + sum(rent_new) - sum(nonrec)

    # ---------------------------------------------------------------- rentabilité (module 4)
    noi = ev["noi_stabilised"]
    yoc = P.yield_on_cost(noi, total_cost)
    yoc_eff = P.yield_on_cost(R.noi_effective(inp), total_cost)
    margin_eur, margin_pct = P.margin(ev["net"], total_cost)
    u_irr, l_irr = annualise(irr(unlevered)), annualise(irr(equity))

    peak, cum = 0.0, 0.0
    for f in equity:
        cum += f
        peak = min(peak, cum)

    periods = []
    for k in range(nq + 1):
        label = "T0" if k == 0 else quarter_label(tl.origin, k)
        date_ = inp.acquisition_date if k == 0 else month_start(tl.origin, 3 * k)
        periods.append({"period": label, "date": date_.isoformat(), "unlevered": unlevered[k],
                        "debt_draw": debt.draws[k], "interest": debt.interest[k], "debt_balance": debt.balance[k],
                        "equity": equity[k]})

    # identité de contrôle : gain des fonds propres = gain du projet - coût de la dette (intérêts + commission)
    profit_gap = sum(equity) - (sum(unlevered) - interest_total - debt.arrangement_fee)

    return {
        "timeline": {
            "acquisition": inp.acquisition_date.isoformat(),
            "works_start": month_start(tl.origin, tl.works_start).isoformat(),
            "delivery": month_start(tl.origin, tl.works_end).isoformat(),
            "lease_start": month_start(tl.origin, tl.lease_start).isoformat(),
            "rent_free_months": inp.rent_free_months,
            "exit": inp.exit_date.isoformat(),
            "months": n, "quarters": nq, "let_at_exit": ev["let_at_exit"],
        },
        "acquisition": {
            "price": inp.price, "acquisition_fees": acq_fees, "acquisition_cost": inp.price + acq_fees,
            "price_per_sqm_before": inp.price / inp.surface_before,
            "price_per_sqm_after": inp.price / inp.surface_after,
        },
        "revenue": {
            "current_leases_income": sum(rent_current),
            "relet_rent_eur_sqm_year": inp.relet_rent,
            "let_rent_annual_headline": R.let_rent_annual(inp),
            "let_rent_annual_effective": R.effective_rent_annual(inp),
            "noi_stabilised": noi, "noi_effective": R.noi_effective(inp),
            "new_lease_income_before_exit": sum(rent_new),
        },
        "cost": {
            "acquisition_price": inp.price, "acquisition_fees": acq_fees, "capex": capex,
            "fees_contingency": fees_c, "holding_costs": sum(holding), "marketing_fees": sum(marketing),
            "interest": interest_total, "arrangement_fee": debt.arrangement_fee,
            "total_cost": total_cost, "total_cost_per_sqm_after": total_cost / inp.surface_after,
            "income_during_operation": income_during,
        },
        "exit": {
            "gross_value": ev["gross"], "deductions": ev["deductions"], "deductions_total": ev["deductions_total"],
            "sale_costs": ev["sale_costs"], "net_value": ev["net"],
            "gross_value_per_sqm": ev["gross"] / inp.surface_after, "exit_yield": inp.exit_yield,
        },
        "profitability": {
            "yield_on_cost": yoc, "yield_on_cost_effective": yoc_eff, "exit_yield": inp.exit_yield,
            "spread_bps": P.spread_bps(yoc, inp.exit_yield), "margin": margin_eur, "margin_on_cost": margin_pct,
        },
        "returns": {
            "unlevered_irr": u_irr, "levered_irr": l_irr, "equity_multiple": equity_multiple(equity),
            "equity_profit": sum(equity), "peak_equity": -peak,
            "max_debt": debt.max_balance, "debt_repaid_at_exit": debt.repayment,
            "ltv_at_exit": debt.repayment / ev["gross"] if ev["gross"] else None,
            "interest_rate": inp.interest_rate,
        },
        "cashflows": periods,
        "checks": {
            "profit_identity": abs(profit_gap) < 1e-6 * max(1.0, abs(sum(unlevered))),
            "debt_balance_identity": abs(debt.repayment - sum(debt.draws) - interest_total) < 1e-6 * max(1.0, debt.repayment),
            "equity_shortfall_at_exit": equity[-1] < 0,
        },
    }


def with_price(inp: DealInputs, price: float) -> DealInputs:
    return replace(inp, price=price)
