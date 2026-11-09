/* Moteur financier : port FIDÈLE de finance/*.py (timeline, revenue, cost, profitability, cashflow,
 * engine, max_price, variations). Aucune donnée du deal ici : les entrées viennent de window.DATA.model_inputs.
 * Toute modification de finance/*.py doit être reportée ici : tests/test_js_engine.py compare les deux moteurs.
 * Dates : objets {y, m, d} ; chaînes ISO « AAAA-MM-JJ » en entrée et en sortie.
 */
(function (root) {
  "use strict";

  // ------------------------------------------------------------------ dates
  function parseDate(s) { const [y, m, d] = s.split("-").map(Number); return { y, m, d }; }
  function iso(dt) { return `${dt.y}-${String(dt.m).padStart(2, "0")}-${String(dt.d).padStart(2, "0")}`; }
  function cmp(a, b) { return a.y - b.y || a.m - b.m || a.d - b.d; }
  function daysIn(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
  function divmod(a, b) { const q = Math.floor(a / b); return [q, a - q * b]; }

  function monthIndex(o, d) { return (d.y - o.y) * 12 + (d.m - o.m); }
  function monthStart(o, m) { const [y, mo] = divmod(o.m - 1 + m, 12); return { y: o.y + y, m: mo + 1, d: 1 }; }
  function quarterLabel(o, k) { const d = monthStart(o, 3 * (k - 1)); return `${d.y}-T${Math.floor((d.m - 1) / 3) + 1}`; }
  function monthEndAfter(d, months) {
    const [y, m] = divmod(d.m - 1 + months, 12);
    const year = d.y + y, month = m + 1;
    return { y: year, m: month, d: daysIn(year, month) };
  }

  // ------------------------------------------------------------------ entrées
  function prepare(raw) {
    const inp = Object.assign({}, raw);
    for (const k of ["acquisition_date", "works_start", "exit_date"]) {
      if (typeof inp[k] === "string") inp[k] = parseDate(inp[k]);
    }
    inp.current_leases = (raw.current_leases || []).map(l => Object.assign({}, l,
      { end_date: typeof l.end_date === "string" ? parseDate(l.end_date) : l.end_date }));
    inp._int_fields = raw._int_fields || [];
    return inp;
  }
  function interestRate(inp) { return Math.max(inp.euribor, inp.euribor_floor) + inp.margin_bps / 10000; }

  // ------------------------------------------------------------------ timeline.py
  function buildTimeline(inp) {
    const origin = { y: inp.acquisition_date.y, m: inp.acquisition_date.m, d: 1 };
    const n = monthIndex(origin, inp.exit_date) + 1;
    const ws = monthIndex(origin, inp.works_start);
    const we = ws + inp.works_months;
    if (ws < 0) throw new Error("les travaux commencent avant l'acquisition");
    if (we > n) throw new Error("la cession intervient avant la fin des travaux");
    const ls = we + inp.letting_void_months;
    return { n_months: n, works_start: ws, works_end: we, lease_start: ls, rent_start: ls + inp.rent_free_months,
             origin, n_quarters: -Math.floor(-n / 3) };
  }
  function leaseMonthFraction(o, m, end) {
    const start = monthStart(o, m);
    const days = daysIn(start.y, start.m);
    if (cmp(end, start) < 0) return 0.0;
    if (cmp(end, { y: start.y, m: start.m, d: days }) >= 0) return 1.0;
    return end.d / days;
  }
  function activeFraction(m, start) { return Math.min(1.0, Math.max(0.0, m + 1 - start)); }
  function range(n) { return Array.from({ length: n }, (_, i) => i); }
  function sum(a) { let s = 0; for (const x of a) s += x; return s; }

  // ------------------------------------------------------------------ revenue.py
  function letRentAnnual(inp) { return inp.relet_rent * inp.surface_after * inp.stabilised_occupancy; }
  function effectiveRentAnnual(inp) { return letRentAnnual(inp) * (1 - inp.rent_free_months / inp.firm_lease_term_months); }
  function noiStabilised(inp) {
    const vacant = inp.surface_after * (1 - inp.stabilised_occupancy);
    return letRentAnnual(inp) * (1 - inp.non_recoverable_pct) - vacant * inp.vacant_holding_cost;
  }
  function noiEffective(inp) {
    const vacant = inp.surface_after * (1 - inp.stabilised_occupancy);
    return effectiveRentAnnual(inp) * (1 - inp.non_recoverable_pct) - vacant * inp.vacant_holding_cost;
  }
  function currentRentMonthly(inp, tl) {
    const out = new Array(tl.n_months).fill(0.0);
    for (let m = 0; m < Math.min(tl.works_start, tl.n_months); m++) {
      out[m] = sum(inp.current_leases.map(l =>
        l.surface_sqm * l.rent_eur_sqm_year / 12 * leaseMonthFraction(tl.origin, m, l.end_date)));
    }
    return out;
  }
  function leasedSurfaceBeforeWorks(inp, tl, m) {
    return sum(inp.current_leases.map(l => l.surface_sqm * leaseMonthFraction(tl.origin, m, l.end_date)));
  }
  function newLeaseRentMonthly(inp, tl) {
    const monthly = letRentAnnual(inp) / 12;
    return range(tl.n_months).map(m => monthly * activeFraction(m, tl.rent_start));
  }

  // ------------------------------------------------------------------ cost.py
  function acquisitionFees(inp) { return inp.price * inp.acq_fees_pct; }
  function capexTotal(inp) { return inp.capex_per_sqm * inp.surface_after; }
  function feesContingencyTotal(inp) { return capexTotal(inp) * inp.fees_contingency_pct; }
  function worksMonthly(inp, tl) {
    const per = (capexTotal(inp) + feesContingencyTotal(inp)) / inp.works_months;
    return range(tl.n_months).map(m => (m >= tl.works_start && m < tl.works_end) ? per : 0.0);
  }
  function vacantSurface(inp, tl, m) {
    if (m < tl.works_start) return Math.max(0.0, inp.surface_before - leasedSurfaceBeforeWorks(inp, tl, m));
    if (m < tl.works_end) return inp.surface_before;
    const letShare = inp.stabilised_occupancy * activeFraction(m, tl.lease_start);
    return inp.surface_after * (1 - letShare);
  }
  function holdingMonthly(inp, tl) { return range(tl.n_months).map(m => vacantSurface(inp, tl, m) * inp.vacant_holding_cost / 12); }
  function nonRecoverableMonthly(inp, tl) {
    const base = letRentAnnual(inp) * inp.non_recoverable_pct / 12;
    return range(tl.n_months).map(m => base * activeFraction(m, tl.lease_start));
  }
  function marketingMonthly(inp, tl) {
    const out = new Array(tl.n_months).fill(0.0);
    if (tl.lease_start < tl.n_months) out[tl.lease_start] = letRentAnnual(inp) * inp.marketing_fees_pct;
    return out;
  }

  // ------------------------------------------------------------------ profitability.py
  function exitDeductions(inp, tl) {
    const n = tl.n_months, monthly = letRentAnnual(inp) / 12;
    const voidAfter = Math.max(0, tl.lease_start - n);
    const rentFreeAfter = Math.max(0.0, tl.rent_start - Math.max(n, tl.lease_start));
    return {
      remaining_rent_free: rentFreeAfter * monthly,
      remaining_void_rent: voidAfter * monthly,
      remaining_void_holding: voidAfter * inp.surface_after * inp.vacant_holding_cost / 12,
      unspent_marketing: tl.lease_start >= n ? letRentAnnual(inp) * inp.marketing_fees_pct : 0.0,
    };
  }
  function exitValue(inp, tl) {
    const noi = noiStabilised(inp);
    if (inp.exit_yield <= 0) throw new Error("taux de sortie <= 0");
    const gross = noi / inp.exit_yield;
    const ded = exitDeductions(inp, tl);
    const dedTotal = sum(Object.values(ded));
    const sale = gross * inp.sale_costs_pct;
    return { noi_stabilised: noi, gross, deductions: ded, deductions_total: dedTotal, sale_costs: sale,
             net: gross - dedTotal - sale, let_at_exit: tl.lease_start < tl.n_months };
  }

  // ------------------------------------------------------------------ cashflow.py
  function npv(rate, flows) { let s = 0; flows.forEach((cf, k) => { s += cf / Math.pow(1 + rate, k); }); return s; }
  function irr(flows, lo = -0.99, hi = 10.0, tol = 1e-12, maxIter = 500) {
    if (!(flows.some(f => f < 0) && flows.some(f => f > 0))) return null;
    let fLo = npv(lo, flows); const fHi = npv(hi, flows);
    if (fLo * fHi > 0) return null;
    for (let i = 0; i < maxIter; i++) {
      const mid = (lo + hi) / 2, fMid = npv(mid, flows);
      if (Math.abs(fMid) < tol || (hi - lo) / 2 < tol) return mid;
      if (fLo * fMid < 0) hi = mid; else { lo = mid; fLo = fMid; }
    }
    return (lo + hi) / 2;
  }
  function annualise(q) { return q === null ? null : Math.pow(1 + q, 4) - 1; }
  function equityMultiple(flows) {
    const out = -sum(flows.filter(f => f < 0));
    return out === 0 ? null : sum(flows.filter(f => f > 0)) / out;
  }
  function toQuarters(monthly, nq) { const q = new Array(nq).fill(0.0); monthly.forEach((v, m) => { q[Math.floor(m / 3)] += v; }); return q; }
  function debtSchedule(unlev, ltv, basis, price, rate, feePct) {
    const n = unlev.length, draws = new Array(n).fill(0.0);
    if (basis === "total_cost") unlev.forEach((cf, k) => { if (cf < 0) draws[k] = -cf * ltv; });
    else if (basis === "price") draws[0] = price * ltv;
    else throw new Error("ltv_basis inconnu : " + basis);
    const interest = new Array(n).fill(0.0), balance = new Array(n).fill(0.0);
    let bal = 0.0;
    for (let k = 0; k < n; k++) {
      if (k > 0) interest[k] = bal * rate / 4;
      bal += interest[k] + draws[k];
      balance[k] = bal;
    }
    return { draws, interest, balance, repayment: bal, arrangement_fee: feePct * sum(draws),
             max_balance: balance.length ? Math.max(...balance) : 0.0 };
  }
  function leveredFlows(unlev, exitProceeds, debt) {
    const eq = unlev.map((cf, k) => cf + debt.draws[k]);
    eq[0] -= debt.arrangement_fee;
    eq[eq.length - 1] += exitProceeds - debt.repayment;
    return eq;
  }

  // ------------------------------------------------------------------ engine.py
  function runModel(raw) {
    const inp = raw._prepared ? raw : prepare(raw);
    const tl = buildTimeline(inp), n = tl.n_months, nq = tl.n_quarters;
    const rc = currentRentMonthly(inp, tl), rn = newLeaseRentMonthly(inp, tl), w = worksMonthly(inp, tl);
    const h = holdingMonthly(inp, tl), nr = nonRecoverableMonthly(inp, tl), mk = marketingMonthly(inp, tl);
    const operating = range(n).map(i => rc[i] + rn[i] - w[i] - h[i] - nr[i] - mk[i]);

    const acqFees = acquisitionFees(inp);
    const unlevExExit = [-(inp.price + acqFees)].concat(toQuarters(operating, nq));
    const ev = exitValue(inp, tl);
    const unlevered = unlevExExit.slice(0, -1).concat([unlevExExit[unlevExExit.length - 1] + ev.net]);
    const rate = interestRate(inp);
    const debt = debtSchedule(unlevExExit, inp.ltv, inp.ltv_basis, inp.price, rate, inp.arrangement_fee_pct);
    const equity = leveredFlows(unlevExExit, ev.net, debt);

    const capex = capexTotal(inp), feesC = feesContingencyTotal(inp), interestTotal = sum(debt.interest);
    const totalCost = inp.price + acqFees + capex + feesC + sum(h) + sum(mk) + interestTotal + debt.arrangement_fee;
    const incomeDuring = sum(rc) + sum(rn) - sum(nr);

    const noi = ev.noi_stabilised, yoc = noi / totalCost, yocEff = noiEffective(inp) / totalCost;
    const marginEur = ev.net - totalCost, marginPct = marginEur / totalCost;
    const uIrr = annualise(irr(unlevered)), lIrr = annualise(irr(equity));
    let peak = 0.0, cum = 0.0;
    for (const f of equity) { cum += f; peak = Math.min(peak, cum); }

    const periods = range(nq + 1).map(k => ({
      period: k === 0 ? "T0" : quarterLabel(tl.origin, k),
      date: k === 0 ? iso(inp.acquisition_date) : iso(monthStart(tl.origin, 3 * k)),
      unlevered: unlevered[k], debt_draw: debt.draws[k], interest: debt.interest[k],
      debt_balance: debt.balance[k], equity: equity[k],
    }));
    const profitGap = sum(equity) - (sum(unlevered) - interestTotal - debt.arrangement_fee);

    return {
      timeline: { acquisition: iso(inp.acquisition_date), works_start: iso(monthStart(tl.origin, tl.works_start)),
                  delivery: iso(monthStart(tl.origin, tl.works_end)), lease_start: iso(monthStart(tl.origin, tl.lease_start)),
                  rent_free_months: inp.rent_free_months, exit: iso(inp.exit_date), months: n, quarters: nq,
                  let_at_exit: ev.let_at_exit },
      acquisition: { price: inp.price, acquisition_fees: acqFees, acquisition_cost: inp.price + acqFees,
                     price_per_sqm_before: inp.price / inp.surface_before, price_per_sqm_after: inp.price / inp.surface_after },
      revenue: { current_leases_income: sum(rc), relet_rent_eur_sqm_year: inp.relet_rent,
                 let_rent_annual_headline: letRentAnnual(inp), let_rent_annual_effective: effectiveRentAnnual(inp),
                 noi_stabilised: noi, noi_effective: noiEffective(inp), new_lease_income_before_exit: sum(rn) },
      cost: { acquisition_price: inp.price, acquisition_fees: acqFees, capex, fees_contingency: feesC,
              holding_costs: sum(h), marketing_fees: sum(mk), interest: interestTotal, arrangement_fee: debt.arrangement_fee,
              total_cost: totalCost, total_cost_per_sqm_after: totalCost / inp.surface_after, income_during_operation: incomeDuring },
      exit: { gross_value: ev.gross, deductions: ev.deductions, deductions_total: ev.deductions_total, sale_costs: ev.sale_costs,
              net_value: ev.net, gross_value_per_sqm: ev.gross / inp.surface_after, exit_yield: inp.exit_yield },
      profitability: { yield_on_cost: yoc, yield_on_cost_effective: yocEff, exit_yield: inp.exit_yield,
                       spread_bps: (yoc - inp.exit_yield) * 10000, margin: marginEur, margin_on_cost: marginPct },
      returns: { unlevered_irr: uIrr, levered_irr: lIrr, equity_multiple: equityMultiple(equity), equity_profit: sum(equity),
                 peak_equity: -peak, max_debt: debt.max_balance, debt_repaid_at_exit: debt.repayment,
                 ltv_at_exit: ev.gross ? debt.repayment / ev.gross : null, interest_rate: rate },
      cashflows: periods,
      checks: {
        profit_identity: Math.abs(profitGap) < 1e-6 * Math.max(1.0, Math.abs(sum(unlevered))),
        debt_balance_identity: Math.abs(debt.repayment - sum(debt.draws) - interestTotal) < 1e-6 * Math.max(1.0, debt.repayment),
        equity_shortfall_at_exit: equity[equity.length - 1] < 0,
      },
    };
  }

  // ------------------------------------------------------------------ variations.py
  function applyChanges(raw, changes, value) {
    const inp = raw._prepared ? raw : prepare(raw);
    const out = {};
    for (const c of changes) {
      const v = (value === undefined || value === null) ? c.value : value;
      if (v === undefined || v === null) throw new Error("valeur manquante pour " + c.field);
      const current = (c.field in out) ? out[c.field] : inp[c.field];
      let nv;
      if (c.op === "mult") nv = current * Number(v);
      else if (c.op === "add") nv = current + v;
      else if (c.op === "set") nv = v;
      else if (c.op === "set_field") nv = inp[String(v)];
      else if (c.op === "add_months") nv = monthEndAfter(current, Math.round(v));
      else throw new Error("opération inconnue " + c.op);
      if (inp._int_fields.includes(c.field)) nv = roundHalfEven(nv);
      out[c.field] = nv;
    }
    return Object.assign({}, inp, out);
  }
  // Python round() arrondit au pair le plus proche (0,5 -> 0 ; 1,5 -> 2)
  function roundHalfEven(x) {
    const f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }

  // ------------------------------------------------------------------ max_price.py
  function withPrice(inp, price) { return Object.assign({}, inp, { price }); }
  function metricFn(inp, name) {
    return price => {
      const r = runModel(withPrice(inp, price));
      const v = name === "levered_irr" ? r.returns.levered_irr : r.profitability.margin_on_cost;
      return v === null ? -Infinity : v;
    };
  }
  function solveMaxPrice(inp, metric, target, tolEur = 1000.0) {
    const f = metricFn(inp, metric);
    let lo = 1.0, hi = inp.price * 3;
    if (f(lo) < target) return { metric, target, price: null, status: "objectif inatteignable même à un prix nul" };
    if (f(hi) >= target) return { metric, target, price: hi, status: "objectif atteint au-delà de la plage" };
    while (hi - lo > tolEur) {
      const mid = (lo + hi) / 2;
      if (f(mid) >= target) lo = mid; else hi = mid;
    }
    return { metric, target, price: lo, status: "ok", value_at_price: f(lo) };
  }
  function maxPrice(raw, targets) {
    const inp = raw._prepared ? raw : prepare(raw);
    const results = {};
    for (const [m, t] of Object.entries(targets)) results[m] = solveMaxPrice(inp, m, t);
    const keys = Object.keys(results), prices = keys.map(k => results[k].price).filter(p => p !== null);
    let binding = null;
    if (prices.length === keys.length) binding = keys.reduce((a, b) => (results[b].price < results[a].price ? b : a));
    const best = binding ? results[binding].price : null;
    return { by_target: results, max_price: best, binding_target: binding, asking_price: inp.price,
             gap_to_asking: best !== null ? best - inp.price : null,
             gap_to_asking_pct: best !== null ? best / inp.price - 1 : null };
  }

  function prepared(raw) { const p = prepare(raw); p._prepared = true; return p; }
  const api = { runModel, maxPrice, applyChanges, prepare: prepared, interestRate };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HeliosEngine = api;
})(typeof window !== "undefined" ? window : this);
