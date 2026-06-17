#!/usr/bin/env python3
"""
Parameter Sweep — Calibración automática de las 6 constantes λ
==============================================================
Modelo analítico con cascada completa: drenaje lineal → primeros
colapsos G_vuln → feedback P_vital/gap-expec → aceleración no lineal.

Uso:
  python3 sweep_lambda.py [--quick] [--full] [--hyp H1|H2|H3]
"""

import argparse
import csv
import itertools
import time
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

LAMBDA_LEVELS = {
    "quick": {
        # Rangos expandidos: el modelo analítico subestima la velocidad
        # de colapso por ~2-3× vs el ABM real (no captura dEs-events,
        # A_load accumulation, ni cascada gap-expec completa).
        "lambda-dano": [0.20, 0.50, 0.80],
        "lambda-prod": [0.20, 0.40, 0.60],
        "lambda-rec": [0.02, 0.06, 0.12],
        "lambda-fric": [0.08, 0.20, 0.35],
        "lambda-aprend": [0.005, 0.015, 0.030],
        "lambda-acople": [0.003, 0.008, 0.018],
    },
    "full": {
        "lambda-dano": [0.15, 0.30, 0.50, 0.70, 1.00],
        "lambda-prod": [0.15, 0.30, 0.50, 0.70, 1.00],
        "lambda-rec": [0.02, 0.05, 0.10, 0.18, 0.25],
        "lambda-fric": [0.05, 0.12, 0.22, 0.35, 0.50],
        "lambda-aprend": [0.003, 0.008, 0.015, 0.025, 0.040],
        "lambda-acople": [0.002, 0.005, 0.010, 0.020, 0.035],
    },
}

HYPOTHESIS_CONFIG = {
    "H1": {
        "dEs": 4.5,
        "dEpMu": 1.5,
        "mInit": 100,
        "initial-pvital": 0.60,
        "ticks": 500,
        "pass_condition": lambda m: m["rigid-frac"] > 0.08,
        "metrics": ["rigid-frac", "avg-M_E", "me-per-cycle", "ticks-to-zero"],
    },
    "H2": {
        "dEs": 3.0,
        "dEpMu": 2.0,
        "mInit": 50,
        "initial-pvital": 0.25,
        "ticks": 500,
        "pass_condition": lambda m: (
            m["rigid-frac"] > 0.03 and m["meta-anchor-strength"] < 0.20
        ),
        "metrics": ["rigid-frac", "meta-anchor-strength", "P_vital", "me-per-cycle"],
    },
    "H3": {
        "dEs": 2.5,
        "dEpMu": 2.0,
        "mInit": 80,
        "initial-pvital": 0.60,
        "ticks": 500,
        "pass_condition": lambda m: m["rigid-frac"] > 0.03,
        "metrics": ["rigid-frac", "avg-M_E", "me-per-cycle", "P_vital"],
    },
}

# ═══════════════════════════════════════════════════════════════
# MODELO ANALÍTICO CON CASCADA COMPLETA
# ═══════════════════════════════════════════════════════════════


def simulate_analytical(hypothesis, lambda_params):
    """
    Simula la cascada completa en 3 fases:
    Fase 1 — Drenaje lineal de M_E hasta que G_vuln colapsan.
    Fase 2 — Primeros colapsos disparan rigid-frac > 0.
    Fase 3 — Feedback: rigid-frac → P_vital↓ → gap-expec↑ → daño↑ → más rigid.
    """
    hyp = HYPOTHESIS_CONFIG[hypothesis]
    ld = lambda_params.get("lambda-dano", 0.30)
    lp = lambda_params.get("lambda-prod", 0.50)
    lr = lambda_params.get("lambda-rec", 0.05)
    lf = lambda_params.get("lambda-fric", 0.15)
    la = lambda_params.get("lambda-aprend", 0.01)
    lc = lambda_params.get("lambda-acople", 0.005)

    dEs, dEp, mInit = hyp["dEs"], hyp["dEpMu"], hyp["mInit"]
    init_pvital = hyp.get("initial-pvital", 0.60)
    g_vuln = 0.15
    horizon = hyp["ticks"]
    cycle_len = 6

    # ─── Estado base ───
    dEs_eff = dEs * 1.15
    hp_eff = min(1.0, 0.25 * (dEs_eff / max(0.3, dEp)))
    dep_eff_base = dEp * (1 - lc * 100 * 0.0)
    gap_frec_base = dEs_eff - dep_eff_base

    # ─── Funciones de daño/producción ───
    def gap_frec_fn(rigid_f):
        dep_now = dEp * (1 - lc * 100 * rigid_f)
        # λ_fric: fricción asimétrica añade extra-stress cuando hay rigid
        #   Rigid externaliza costo → no-rigid reciben fricción ∝ gap × rigid_f
        extra_stress = lf * gap_frec_base * rigid_f * 0.5
        return dEs_eff - dep_now + extra_stress

    def damage_fn(gap, m_omega, rigid_f, defeat_ct, aload):
        dmg_gap = max(0, gap) * 0.5 * 0.3
        shield = 1 / (1 + m_omega * lc * 10)
        p_vital = max(0.05, init_pvital - rigid_f * 0.7)
        gap_expec = 0.0
        if p_vital < 0.6:
            gap_expec = 0.7 * (1 - p_vital) * 0.85 * (1 + defeat_ct * 0.05)
        dmg_expec = gap_expec * 0.7 * 0.7
        # λ_aprend: A_load acumulada drena M_E adicional
        aload_drain = aload * ld * 0.006
        return (dmg_gap + dmg_expec) * ld * 2.0 * shield + aload_drain

    def prod_fn(gap):
        prod = min(dEp * lp, 2.5)
        net = prod - lp * 0.4
        eff = max(0.25, 1 - gap / max(1, dEp * 2.5))
        return net * eff

    def iso_net_fn():
        drain = ld * 0.15
        gain = 0.975 * lr * (1 - hp_eff)
        return gain - drain

    # ─── Simulación time-stepped ───
    me_now = mInit
    m_omega = 3.0
    rigid_f = 0.0
    defeat_ct = 0
    aload = 0.0
    ticks = 0
    first_collapse = None
    me_cycle_initial = None

    while ticks < 10000:
        gap = gap_frec_fn(rigid_f)
        damage = damage_fn(gap, m_omega, rigid_f, defeat_ct, aload)
        production = prod_fn(gap)
        regen = dEp * lp * lr * 0.5 * max(0.1, 1 - rigid_f)
        iso_net = iso_net_fn()

        me_cycle = 5 * iso_net + production + regen - damage
        if me_cycle_initial is None:
            me_cycle_initial = me_cycle

        me_now += me_cycle
        ticks += cycle_len
        defeat_ct += 1

        # λ_aprend: A_load se acumula con cada ciclo tight
        aload += max(0, gap) * 0.5 * la

        # Primer colapso: G_vuln agentes llegan a M_E=0
        if me_now <= 0:
            if first_collapse is None:
                first_collapse = ticks
                rigid_f = g_vuln * 0.5
            else:
                cascade_add = g_vuln * 0.3 * (1 + (ticks - first_collapse) / 200)
                rigid_f = min(1.0, rigid_f + cascade_add)
            me_now = mInit * max(0.05, 0.3 - rigid_f * 0.2)

        if rigid_f > 0.05:
            m_omega = max(1.0, 3.0 - rigid_f * 3.0)

        if rigid_f > 0.8 or (first_collapse and ticks - first_collapse > horizon * 2):
            break

    # ─── Métricas finales ───
    if first_collapse and first_collapse < horizon:
        cascade_time = max(1, horizon - first_collapse)
        rigid_final = min(1.0, g_vuln * (1 + cascade_time / 60))
    elif me_now <= 10:
        rigid_final = g_vuln * 0.2
    else:
        rigid_final = 0.0

    meta_anchor = max(0.03, init_pvital * 0.7 - rigid_final * 0.65)
    p_vital = max(0.05, init_pvital - rigid_final * 0.8)

    return {
        "rigid-frac": rigid_final,
        "avg-M_E": max(0, me_now),
        "avg-M_Omega": max(1, 3.0 - rigid_final * 2.5),
        "meta-anchor-strength": meta_anchor,
        "P_vital": p_vital,
        "me-per-cycle": me_cycle_initial or 0,
        "ticks-to-zero": first_collapse or 99999,
    }


# ═══════════════════════════════════════════════════════════════
# SWEEP ENGINE
# ═══════════════════════════════════════════════════════════════


def run_sweep(mode="quick", hypotheses=None):
    if hypotheses is None:
        hypotheses = ["H1", "H2", "H3"]

    levels = LAMBDA_LEVELS[mode]
    param_names = list(levels.keys())
    param_values = [levels[p] for p in param_names]

    total_runs = 1
    for v in param_values:
        total_runs *= len(v)
    total_runs *= len(hypotheses)

    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║  SWEEP λ — Modelo analítico con cascada completa   ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  Modo: {mode} ({total_runs} runs) | Hipótesis: {', '.join(hypotheses)}")
    print(f"╚══════════════════════════════════════════════════════╝\n")

    results = []
    run_id = 0
    start = time.time()

    for combo in itertools.product(*param_values):
        lp_dict = dict(zip(param_names, combo))
        for hyp in hypotheses:
            run_id += 1
            m = simulate_analytical(hyp, lp_dict)
            passed = HYPOTHESIS_CONFIG[hyp]["pass_condition"](m)

            row = {"run": run_id, "hypothesis": hyp, **lp_dict, "pass": passed}
            for k in HYPOTHESIS_CONFIG[hyp]["metrics"]:
                row[f"metric_{k}"] = m.get(k, 0)
            results.append(row)

            elapsed = time.time() - start
            rate = run_id / max(0.01, elapsed)
            eta = (total_runs - run_id) / max(0.01, rate)
            s = "✓" if passed else "✗"
            print(
                f"  [{run_id}/{total_runs}] {s} {hyp} "
                f"λd={lp_dict['lambda-dano']:.2f} λp={lp_dict['lambda-prod']:.2f} "
                f"λr={lp_dict['lambda-rec']:.2f} | ETA: {eta / 60:.0f}min"
            )

    return results


# ═══════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════


def generate_report(results, path="sweep_report"):
    passing = [r for r in results if r["pass"]]
    p_h1 = [r for r in passing if r["hypothesis"] == "H1"]
    p_h2 = [r for r in passing if r["hypothesis"] == "H2"]
    p_h3 = [r for r in passing if r["hypothesis"] == "H3"]

    # Combinaciones donde pasan las 3
    combo_sets = {}
    for r in results:
        key = tuple(r[p] for p in LAMBDA_LEVELS["quick"])
        combo_sets.setdefault(key, set()).add((r["hypothesis"], r["pass"]))
    all3 = {
        k
        for k, v in combo_sets.items()
        if all(any(h == hh and pp for h, pp in v) for hh in ["H1", "H2", "H3"])
    }

    # Rangos
    def rng(param):
        vals = [r[param] for r in passing]
        return (f"{min(vals):.3f}", f"{max(vals):.3f}") if vals else ("N/A", "N/A")

    ranges = {p: rng(p) for p in LAMBDA_LEVELS["quick"]}

    report = f"""# Reporte de Calibración λ — Cascada Completa

**Fecha:** {datetime.now().strftime("%Y-%m-%d %H:%M")}
**Total:** {len(results)} runs | **Pasan:** {len(passing)} ({100 * len(passing) / max(1, len(results)):.0f}%)
**Las 3 juntas:** {len(all3)} combinaciones

## Rangos λ en región válida

| λ | Min | Max | Default |
|---|-----|-----|---------|
"""
    for p in LAMBDA_LEVELS["quick"]:
        report += f"| {p} | {ranges[p][0]} | {ranges[p][1]} | {LAMBDA_LEVELS['quick'][p][0]:.3f} |\n"

    report += f"""
## Por hipótesis

| Hip | Total | Pasan | % |
|-----|-------|-------|---|
| H1  | {sum(1 for r in results if r["hypothesis"] == "H1")} | {len(p_h1)} | {100 * len(p_h1) // max(1, sum(1 for r in results if r["hypothesis"] == "H1"))} |
| H2  | {sum(1 for r in results if r["hypothesis"] == "H2")} | {len(p_h2)} | {100 * len(p_h2) // max(1, sum(1 for r in results if r["hypothesis"] == "H2"))} |
| H3  | {sum(1 for r in results if r["hypothesis"] == "H3")} | {len(p_h3)} | {100 * len(p_h3) // max(1, sum(1 for r in results if r["hypothesis"] == "H3"))} |

## Top combinaciones (las 3 hipótesis)
"""
    if all3:
        report += "| λ_daño | λ_prod | λ_rec | λ_fric | λ_aprend | λ_acople |\n"
        report += "|--------|--------|-------|--------|----------|----------|\n"
        for c in sorted(all3)[:15]:
            report += f"| {c[0]:.2f} | {c[1]:.2f} | {c[2]:.2f} | {c[3]:.2f} | {c[4]:.3f} | {c[5]:.3f} |\n"
    else:
        report += "*Ninguna combinación pasa las 3.*\n"
        report += "- Probar `--full` para más niveles\n"
        report += "- Las condiciones pueden ser demasiado estrictas para H2/H3\n"

    with open(f"{path}.md", "w") as f:
        f.write(report)

    # CSV
    if results:
        fieldnames = (
            ["run", "hypothesis"]
            + list(LAMBDA_LEVELS["quick"])
            + [f"metric_{m}" for m in HYPOTHESIS_CONFIG["H1"]["metrics"]]
            + ["pass"]
        )
        with open(f"{path}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)

    return report


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════


def main():
    p = argparse.ArgumentParser(description="Sweep λ — ABM Trauma Ecosocial")
    p.add_argument("--full", action="store_true")
    p.add_argument(
        "--hyp", nargs="+", choices=["H1", "H2", "H3"], default=["H1", "H2", "H3"]
    )
    p.add_argument("--output", default="sweep_report")
    args = p.parse_args()

    mode = "full" if args.full else "quick"
    results = run_sweep(mode, args.hyp)
    report = generate_report(results, args.output)
    print(f"\n{'=' * 60}")
    print(f"Reporte: {args.output}.md  |  CSV: {args.output}.csv")
    print(f"{'=' * 60}\n{report}")


if __name__ == "__main__":
    main()
