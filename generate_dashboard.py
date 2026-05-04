"""Generates a self-contained index.html dashboard from pre-computed JSON data."""
import json, os
from datetime import date, datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT  = os.path.join(BASE, "index.html")

def load(name):
    with open(f"{DATA}/{name}") as f: return json.load(f)

cohort_data       = load("cohort_revenue.json")
cohort_data_gross = load("cohort_gross_revenue.json")
new_users      = {r['cohort_month']: r['new_users'] for r in load("new_users.json")}
payback        = load("payback.json")
channel_sum    = [r for r in load("channel_summary.json") if r['spend'] > 0]
monthly_rev    = load("monthly_revenue.json")
monthly_spend  = load("monthly_spend.json")
cac_data       = load("cac.json")

# ── Derived ───────────────────────────────────────────────────────────────────
cohorts     = cohort_data['cohorts']
cumulative  = cohort_data['cumulative']

# monthly spend totals by month
spend_by_month = {}
for r in monthly_spend:
    m = r['month']
    spend_by_month[m] = spend_by_month.get(m, 0) + r['spend']

# align revenue + spend to same months
all_months_set = sorted(set(r['month'] for r in monthly_rev) | set(spend_by_month.keys()))
rev_map = {r['month']: r['revenue'] for r in monthly_rev}

chart_months  = all_months_set
chart_revenue = [round(rev_map.get(m, 0), 2)         for m in chart_months]
chart_spend   = [round(spend_by_month.get(m, 0), 2)  for m in chart_months]
chart_roas    = [round(chart_revenue[i]/chart_spend[i] * 100, 1) if chart_spend[i] > 0 else 0
                 for i in range(len(chart_months))]

# payback chart
pb_months  = [r['cohort_month'] for r in payback]
pb_periods = [r['payback_month'] if r['payback_month'] is not None else None for r in payback]
pb_cac     = [r['cac']          for r in payback]
pb_ltv6    = [r['ltv_month_6']  for r in payback]
pb_users   = [r['new_users']    for r in payback]

# channel chart
ch_labels  = [r['attributed_channel'].capitalize() for r in channel_sum]
ch_rev     = [round(r['revenue'], 0) for r in channel_sum]
ch_spend   = [round(r['spend'],   0) for r in channel_sum]
ch_roas    = [round(r['roas'] * 100, 1) for r in channel_sum]

# KPIs
total_rev        = sum(chart_revenue)
total_spend      = sum(chart_spend)
total_users      = sum(new_users.values())
avg_cac          = round(sum(r['cac'] for r in payback) / len(payback), 2) if payback else 0
valid_pb         = [r['payback_month'] for r in payback if r['payback_month'] is not None]
avg_pb           = round(sum(valid_pb) / len(valid_pb), 1) if valid_pb else "N/A"
overall_roas_pct = round(total_rev / total_spend * 100, 1) if total_spend else 0

# Total Gross Revenue (sum all gross cohort values at their last available month)
total_gross_rev = sum(
    max(cohort_data_gross['cumulative'].get(c, {0: 0}).values(), default=0)
    for c in cohort_data_gross['cohorts']
)

# ── Cohort Heatmap helpers ────────────────────────────────────────────────────
MAX_MONTHS = 23

# Minimum % of CAC that must be recovered by each month for Net Revenue Heatmap
THRESHOLDS = {0: 55, 1: 73, 2: 82, 3: 91, 4: 95, 5: 98}

today = date.today()
CURRENT_YEAR_MONTH = (today.year, today.month)
UPDATED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def month_has_occurred(cohort_str, offset):
    """True if cohort_month + offset months <= current month."""
    y, m = int(cohort_str[:4]), int(cohort_str[5:7])
    total = m - 1 + offset
    target = (y + total // 12, total % 12 + 1)
    return target <= CURRENT_YEAR_MONTH

def heat_color(pct):
    """white → yellow → green based on % CAC recovered."""
    if pct <= 0:   return "255,255,255"
    if pct < 0.5:
        return f"255,255,{int(255 * (1 - pct * 2))}"
    if pct < 1.0:
        p = (pct - 0.5) * 2
        return f"{int(255*(1-p))},230,0"
    p = min((pct - 1.0) / 1.0, 1.0)
    return f"{int(34+(0-34)*p)},{int(197+(120-197)*p)},{int(94+(30-94)*p)}"

def _last_occurred_value(cum, cohort, nu=None):
    """Returns the highest cumulative value for months that have already occurred."""
    best = 0
    for m in range(MAX_MONTHS + 1):
        if not month_has_occurred(cohort, m): break
        v = cum.get(str(m), cum.get(m, 0))
        if v > best: best = v
    return round(best / nu, 2) if nu else round(best, 2)

def _cells_per_user(cd, cohort, cac_val):
    """Cumulative revenue per user per month, colored by % of CAC recovered."""
    cum   = cd['cumulative'].get(cohort, {})
    nu    = new_users.get(cohort, 1)
    cells = []
    for m in range(MAX_MONTHS + 1):
        if not month_has_occurred(cohort, m):
            cells.append(None); continue
        rev_pu = round(cum.get(str(m), cum.get(m, 0)) / nu, 2) if nu else 0
        pct    = rev_pu / cac_val if cac_val > 0 else 0
        cells.append({'value': rev_pu, 'pct': round(pct * 100), 'color': heat_color(pct)})
    return cells

def build_heatmap_rows_cac(cd):
    """Net revenue per user, color = % of CAC recovered."""
    rows = []
    for cohort in cd['cohorts']:
        nu      = new_users.get(cohort, 1)
        cac_val = next((r['cac'] for r in payback if r['cohort_month'] == cohort), 0)
        cum     = cd['cumulative'].get(cohort, {})
        rows.append({
            'cohort': cohort, 'users': nu,
            'meta': f'${cac_val:.2f}', 'meta_label': 'CAC',
            'total': _last_occurred_value(cum, cohort, nu),
            'total_pct': round(_last_occurred_value(cum, cohort, nu) / cac_val * 100) if cac_val else 0,
            'cells': _cells_per_user(cd, cohort, cac_val),
        })
    return rows

def build_heatmap_rows_gross_per_user(cd):
    """Gross revenue per user, color = % of CAC recovered (same scale as net)."""
    rows = []
    for cohort in cd['cohorts']:
        nu      = new_users.get(cohort, 1)
        cac_val = next((r['cac'] for r in payback if r['cohort_month'] == cohort), 0)
        cum     = cd['cumulative'].get(cohort, {})
        rows.append({
            'cohort': cohort, 'users': nu,
            'meta': f'${cac_val:.2f}', 'meta_label': 'CAC',
            'total': _last_occurred_value(cum, cohort, nu),
            'total_pct': round(_last_occurred_value(cum, cohort, nu) / cac_val * 100) if cac_val else 0,
            'cells': _cells_per_user(cd, cohort, cac_val),
        })
    return rows

def build_heatmap_rows_total_gross(cd):
    """Total gross revenue (all users), color = % of total cohort spend recovered."""
    spend_map = {r['cohort_month']: r['spend'] for r in cac_data}
    rows = []
    for cohort in cd['cohorts']:
        cum         = cd['cumulative'].get(cohort, {})
        nu          = new_users.get(cohort, 1)
        total_spend = spend_map.get(cohort, 0)
        cells = []
        for m in range(MAX_MONTHS + 1):
            if not month_has_occurred(cohort, m):
                cells.append(None); continue
            total_rev = round(cum.get(str(m), cum.get(m, 0)), 2)
            pct       = total_rev / total_spend if total_spend > 0 else 0
            cells.append({'value': total_rev, 'pct': round(pct * 100), 'color': heat_color(pct)})
        spend_label  = f'${total_spend/1000:.1f}K' if total_spend >= 1000 else f'${total_spend:.0f}'
        cohort_total = _last_occurred_value(cum, cohort)   # no /nu → absolute total
        cohort_pct   = round(cohort_total / total_spend * 100) if total_spend else 0
        rows.append({
            'cohort': cohort, 'users': nu,
            'meta': spend_label, 'meta_label': 'Total Spend',
            'total': cohort_total, 'total_pct': cohort_pct,
            'cells': cells,
        })
    return rows

def build_heatmap_rows_total_net(cd):
    """Total net revenue (all users), color = % of total cohort spend recovered."""
    spend_map = {r['cohort_month']: r['spend'] for r in cac_data}
    rows = []
    for cohort in cd['cohorts']:
        cum         = cd['cumulative'].get(cohort, {})
        nu          = new_users.get(cohort, 1)
        total_spend = spend_map.get(cohort, 0)
        cells = []
        for m in range(MAX_MONTHS + 1):
            if not month_has_occurred(cohort, m):
                cells.append(None); continue
            total_rev = round(cum.get(str(m), cum.get(m, 0)), 2)
            pct       = total_rev / total_spend if total_spend > 0 else 0
            cells.append({'value': total_rev, 'pct': round(pct * 100), 'color': heat_color(pct)})
        spend_label  = f'${total_spend/1000:.1f}K' if total_spend >= 1000 else f'${total_spend:.0f}'
        cohort_total = _last_occurred_value(cum, cohort)
        cohort_pct   = round(cohort_total / total_spend * 100) if total_spend else 0
        rows.append({
            'cohort': cohort, 'users': nu,
            'meta': spend_label, 'meta_label': 'Total Spend',
            'total': cohort_total, 'total_pct': cohort_pct,
            'cells': cells,
        })
    return rows

heatmap_rows_net         = build_heatmap_rows_cac(cohort_data)
heatmap_rows_gross       = build_heatmap_rows_gross_per_user(cohort_data_gross)
heatmap_rows_total_gross = build_heatmap_rows_total_gross(cohort_data_gross)
heatmap_rows_total_net   = build_heatmap_rows_total_net(cohort_data)

# ── LTV curves (cumulative revenue per user by cohort) ───────────────────────
ltv_datasets = []
colors = [
    '#6366f1','#3b82f6','#06b6d4','#10b981','#84cc16',
    '#f59e0b','#ef4444','#ec4899','#8b5cf6','#14b8a6',
    '#f97316','#64748b','#a855f7','#0ea5e9','#22c55e','#e11d48'
]
for i, cohort in enumerate(cohorts[-10:]):  # last 10 cohorts for clarity
    cum = cumulative.get(cohort, {})
    nu  = new_users.get(cohort, 1)
    pts = []
    for m in range(MAX_MONTHS + 1):
        v = cum.get(str(m), cum.get(m, 0))
        pts.append(round(v / nu, 2) if nu else 0)
    ltv_datasets.append({'label': cohort, 'data': pts, 'color': colors[i % len(colors)]})

# ── Serialize for JS ──────────────────────────────────────────────────────────
j = lambda x: json.dumps(x)

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cohort Analytics Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}}
  .header{{background:#1e293b;border-bottom:1px solid #334155;padding:16px 20px;display:flex;align-items:center;gap:12px;justify-content:space-between}}
  .header h1{{font-size:18px;font-weight:700;color:#f8fafc}}
  .header .badge{{background:#3b82f6;color:#fff;font-size:11px;font-weight:600;padding:3px 8px;border-radius:20px;white-space:nowrap}}
  .container{{max-width:1600px;margin:0 auto;padding:16px 20px}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(7,1fr);gap:12px;margin-bottom:24px}}
  .kpi{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px}}
  .kpi .label{{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}}
  .kpi .value{{font-size:22px;font-weight:700;color:#f8fafc}}
  .kpi .sub{{font-size:11px;color:#64748b;margin-top:4px}}
  .kpi.green .value{{color:#22c55e}}
  .kpi.blue  .value{{color:#3b82f6}}
  .kpi.amber .value{{color:#f59e0b}}
  .charts-row{{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}}
  .card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px}}
  .card h2{{font-size:14px;font-weight:600;color:#f8fafc;margin-bottom:16px}}
  .card .subtitle{{font-size:11px;color:#64748b;margin-top:-12px;margin-bottom:16px}}
  .chart-wrap{{position:relative}}

  /* Heatmap */
  .heatmap-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  table.heatmap{{border-collapse:collapse;font-size:11px;width:100%}}
  table.heatmap th{{background:#0f172a;color:#64748b;font-weight:600;padding:7px 8px;text-align:center;white-space:nowrap;position:sticky;top:0;z-index:1}}
  table.heatmap th.left{{text-align:left}}
  table.heatmap td{{padding:5px 6px;text-align:center;border:1px solid #1e293b;min-width:62px}}
  table.heatmap td.meta{{text-align:left;color:#94a3b8;white-space:nowrap;font-weight:500;min-width:80px;background:#1e293b;position:sticky;left:0;z-index:1}}
  table.heatmap td.meta span{{display:block;font-size:10px;color:#475569}}
  .pct-label{{font-size:10px;opacity:.8}}
  .charts-bottom{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}}

  /* ── Mobile ── */
  @media(max-width:767px){{
    .header{{padding:12px 16px}}
    .header h1{{font-size:15px}}
    .container{{padding:12px 16px}}
    .kpi-grid{{grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:16px}}
    .kpi{{padding:12px}}
    .kpi .value{{font-size:18px}}
    .kpi .label{{font-size:9px}}
    .charts-row{{grid-template-columns:1fr;gap:12px;margin-bottom:12px}}
    .charts-bottom{{grid-template-columns:1fr;gap:12px}}
    .card{{padding:14px;border-radius:10px}}
    .card h2{{font-size:13px;margin-bottom:12px}}
    .card .subtitle{{font-size:10px;margin-bottom:12px}}
    table.heatmap{{font-size:10px}}
    table.heatmap th{{padding:5px 6px;font-size:10px}}
    table.heatmap td{{padding:4px 4px;min-width:52px}}
    table.heatmap td.meta{{min-width:68px;font-size:10px}}
    .pct-label{{font-size:9px}}
  }}

  /* ── Tablet ── */
  @media(min-width:768px) and (max-width:1199px){{
    .kpi-grid{{grid-template-columns:repeat(4,1fr)}}
    .charts-row{{grid-template-columns:1fr}}
  }}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:12px">
    <h1>Cohort Analytics Dashboard</h1>
    <span class="badge">Live BQ</span>
  </div>
  <div style="text-align:right">
    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;font-weight:600">Last updated</div>
    <div style="font-size:13px;color:#94a3b8;font-weight:500;margin-top:2px">{UPDATED_AT}</div>
  </div>
</div>

<div class="container">

  <!-- KPI Cards -->
  <div class="kpi-grid">
    <div class="kpi">
      <div class="label">Total Gross Revenue</div>
      <div class="value">${total_gross_rev/1e6:.2f}M</div>
      <div class="sub">Authorized, USD</div>
    </div>
    <div class="kpi">
      <div class="label">Total Net Revenue</div>
      <div class="value">${total_rev/1e6:.2f}M</div>
      <div class="sub">After fees, USD</div>
    </div>
    <div class="kpi">
      <div class="label">Total Spend</div>
      <div class="value">${total_spend/1e6:.2f}M</div>
      <div class="sub">Marketing</div>
    </div>
    <div class="kpi {'green' if overall_roas_pct > 150 else 'amber'}">
      <div class="label">Overall ROAS</div>
      <div class="value">{overall_roas_pct}%</div>
      <div class="sub">Revenue / Spend</div>
    </div>
    <div class="kpi blue">
      <div class="label">Total New Users</div>
      <div class="value">{total_users:,}</div>
      <div class="sub">Paid acquisition</div>
    </div>
    <div class="kpi">
      <div class="label">Avg CAC</div>
      <div class="value">${avg_cac}</div>
      <div class="sub">Cost per new user</div>
    </div>
    <div class="kpi green">
      <div class="label">Avg Payback</div>
      <div class="value">{avg_pb} mo</div>
      <div class="sub">Months to recover CAC</div>
    </div>
  </div>

  <!-- Revenue vs Spend -->
  <div class="charts-row">
    <div class="card">
      <h2>Monthly Revenue vs Marketing Spend</h2>
      <p class="subtitle">Net revenue (USD) and paid marketing spend by month</p>
      <div class="chart-wrap"><canvas id="revSpendChart" height="120"></canvas></div>
    </div>
    <div class="card">
      <h2>ROAS by Channel</h2>
      <p class="subtitle">Revenue / Spend ratio per acquisition channel</p>
      <div class="chart-wrap"><canvas id="channelChart" height="120"></canvas></div>
    </div>
  </div>

  <!-- Cohort Heatmaps -->
"""

def fmt_value(v, is_total):
    """Format cell value: totals in K/M, per-user in dollars."""
    if is_total:
        if v >= 1_000_000: return f'${v/1_000_000:.2f}M'
        if v >= 1_000:     return f'${v/1_000:.1f}K'
        return f'${v:.0f}'
    return f'${v:.2f}'

def render_heatmap_table(rows, max_months, is_total=False, thresholds=None):
    meta_label = rows[0]['meta_label'] if rows else 'CAC'
    th_bg = '#0f172a'
    tr_bg = '#162032'  # slightly lighter for threshold row
    out  = '    <div class="heatmap-wrap">\n'
    out += '      <table class="heatmap">\n        <thead>\n'
    # ── Row 1: column labels ──────────────────────────────────────────────────
    out += '        <tr>\n'
    out += f'          <th class="left">Cohort</th><th class="left">Users</th><th class="left">{meta_label}</th>\n'
    out += '          <th style="border-left:2px solid #475569">Total</th>\n'
    out += ''.join(f'          <th>M{m}</th>\n' for m in range(max_months + 1))
    out += '        </tr>\n'
    # ── Row 2: threshold row (inside thead so it sticks with headers) ─────────
    if thresholds:
        S = 'position:sticky;top:33px;z-index:1;'
        out += '        <tr style="border-top:2px solid #ef4444;border-bottom:2px solid #ef4444">\n'
        out += f'          <th class="left" style="{S}background:{tr_bg};color:#ef4444;font-size:10px;font-weight:700;letter-spacing:.04em">MIN %</th>\n'
        out += f'          <th style="{S}background:{tr_bg}"></th>\n'
        out += f'          <th style="{S}background:{tr_bg}"></th>\n'
        out += f'          <th style="{S}background:{tr_bg};border-left:2px solid #475569"></th>\n'
        for m in range(max_months + 1):
            t = thresholds.get(m)
            if t is not None:
                out += f'          <th style="{S}background:{tr_bg};color:#ef4444;font-size:12px;font-weight:700">{t}%</th>\n'
            else:
                out += f'          <th style="{S}background:{tr_bg}"></th>\n'
        out += '        </tr>\n'
    out += '        </thead>\n        <tbody>\n'
    for row in rows:
        out += '        <tr>\n'
        out += f'          <td class="meta">{row["cohort"]}</td>\n'
        out += f'          <td class="meta"><span>{row["users"]:,}</span></td>\n'
        out += f'          <td class="meta"><span>{row["meta"]}</span></td>\n'
        # Total column
        tv   = row['total']
        tpct = row['total_pct']
        ttxt = '#1e293b' if tpct < 60 else '#fff'
        tcol = heat_color(tpct / 100)
        if tv > 0:
            out += f'          <td style="background:rgb({tcol});color:{ttxt};border-left:2px solid #475569"><b>{fmt_value(tv, is_total)}</b><br><span class="pct-label">{tpct}%</span></td>\n'
        else:
            out += '          <td style="background:#0f172a;border-left:2px solid #475569">—</td>\n'
        for m, cell in enumerate(row['cells']):
            if cell is None:
                out += '          <td style="background:#0f172a"></td>\n'
                continue
            col = cell['color']
            val = cell['value']
            pct = cell['pct']
            txt = '#1e293b' if pct < 60 else '#fff'
            if thresholds and thresholds.get(m) is not None and pct < thresholds[m]:
                col = '239,68,68'
                txt = '#fff'
            if val > 0:
                out += f'          <td style="background:rgb({col});color:{txt}"><b>{fmt_value(val, is_total)}</b><br><span class="pct-label">{pct}%</span></td>\n'
            else:
                out += '          <td style="background:#0f172a;color:#334155">—</td>\n'
        out += '        </tr>\n'
    out += '        </tbody>\n      </table>\n    </div>\n'
    return out

HTML += f"""
  <div class="card" style="margin-bottom:20px">
    <h2>Total Authorized Revenue Heatmap — Cumulative Gross Revenue (All Users) vs Total Spend</h2>
    <p class="subtitle">Total gross revenue from entire cohort. Column = total marketing spend for that cohort. Color = % of spend recovered (white → green = 0% → 100%+)</p>
{render_heatmap_table(heatmap_rows_total_gross, MAX_MONTHS, is_total=True)}  </div>

  <div class="card" style="margin-bottom:20px">
    <h2>Total Net Revenue Heatmap — Cumulative Net Revenue (All Users) vs Total Spend</h2>
    <p class="subtitle">Total net revenue (after fees & chargebacks) from entire cohort. Column = total marketing spend for that cohort. Color = % of spend recovered (white → green = 0% → 100%+)</p>
{render_heatmap_table(heatmap_rows_total_net, MAX_MONTHS, is_total=True)}  </div>

  <div class="card" style="margin-bottom:20px">
    <h2>Authorized Revenue Heatmap — Cumulative Gross Revenue per User vs CAC</h2>
    <p class="subtitle">Gross authorized amount per acquired user. Color = % of CAC recovered (white → green = 0% → 100%+)</p>
{render_heatmap_table(heatmap_rows_gross, MAX_MONTHS)}  </div>

  <div class="card">
    <h2>Net Revenue Heatmap — Cumulative Net Revenue per User vs CAC</h2>
    <p class="subtitle">Net revenue (after fees & chargebacks) per acquired user. Color = % of CAC recovered. <span style="color:#ef4444;font-weight:600">MIN % row</span> = minimum threshold — red cell means below threshold.</p>
{render_heatmap_table(heatmap_rows_net, MAX_MONTHS, thresholds=THRESHOLDS)}  </div>

  <!-- LTV Curves + Payback -->
  <div class="charts-bottom">
    <div class="card">
      <h2>Cumulative LTV per User by Cohort</h2>
      <p class="subtitle">How revenue per acquired user accumulates over months (last 10 cohorts)</p>
      <div class="chart-wrap"><canvas id="ltvChart" height="200"></canvas></div>
    </div>
    <div class="card">
      <h2>Payback Period & CAC by Cohort</h2>
      <p class="subtitle">Months to recover CAC and cost per acquisition trend</p>
      <div class="chart-wrap"><canvas id="paybackChart" height="200"></canvas></div>
    </div>
  </div>

  <!-- Channel Revenue vs Spend table -->
  <div class="card" style="margin-top:24px">
    <h2>Channel Performance Summary</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px">
      <thead>
        <tr style="color:#64748b;border-bottom:1px solid #334155">
          <th style="text-align:left;padding:10px 0">Channel</th>
          <th style="text-align:right;padding:10px">Revenue</th>
          <th style="text-align:right;padding:10px">Spend</th>
          <th style="text-align:right;padding:10px">ROAS</th>
          <th style="text-align:right;padding:10px">Rev Share</th>
        </tr>
      </thead>
      <tbody>
"""
total_rev_all = sum(r['revenue'] for r in load("channel_summary.json"))
for r in load("channel_summary.json"):
    roas_pct = round(r['roas'] * 100, 1)
    roas_col = '#22c55e' if r['roas'] > 1.3 else ('#f59e0b' if r['roas'] > 0 else '#64748b')
    share = round(r['revenue'] / total_rev_all * 100, 1) if total_rev_all else 0
    HTML += f"""
        <tr style="border-bottom:1px solid #1e293b">
          <td style="padding:12px 0;font-weight:600;color:#f8fafc">{r['attributed_channel'].capitalize()}</td>
          <td style="text-align:right;padding:12px">${r['revenue']/1e6:.2f}M</td>
          <td style="text-align:right;padding:12px">${r['spend']/1e6:.2f}M</td>
          <td style="text-align:right;padding:12px;color:{roas_col};font-weight:600">{roas_pct}%</td>
          <td style="text-align:right;padding:12px">{share}%</td>
        </tr>"""

HTML += f"""
      </tbody>
    </table>
  </div>

  <div style="text-align:center;color:#334155;font-size:12px;margin:32px 0 16px">
    Data sourced from BigQuery · performance_analytics · Updated daily at 00:00 UTC
  </div>
</div>

<script>
const chartMonths  = {j(chart_months)};
const chartRevenue = {j(chart_revenue)};
const chartSpend   = {j(chart_spend)};
const chartRoas    = {j(chart_roas)};
const pbMonths     = {j(pb_months)};
const pbPeriods    = {j(pb_periods)};
const pbCac        = {j(pb_cac)};
const chLabels     = {j(ch_labels)};
const chRev        = {j(ch_rev)};
const chSpend      = {j(ch_spend)};
const chRoas       = {j(ch_roas)};
const ltvDatasets  = {j(ltv_datasets)};
const maxMonths    = {MAX_MONTHS};

// ── Revenue vs Spend chart ────────────────────────────────────────────────────
new Chart(document.getElementById('revSpendChart'), {{
  type: 'bar',
  data: {{
    labels: chartMonths,
    datasets: [
      {{
        label: 'Net Revenue',
        data: chartRevenue,
        backgroundColor: 'rgba(99,102,241,0.8)',
        order: 2,
        yAxisID: 'y',
      }},
      {{
        label: 'Marketing Spend',
        data: chartSpend,
        backgroundColor: 'rgba(239,68,68,0.7)',
        order: 2,
        yAxisID: 'y',
      }},
      {{
        label: 'ROAS',
        data: chartRoas,
        type: 'line',
        borderColor: '#f59e0b',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 4,
        order: 1,
        yAxisID: 'y2',
      }}
    ]
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x:  {{ ticks: {{ color: '#64748b' }}, grid: {{ color: '#1e293b' }} }},
      y:  {{ ticks: {{ color: '#64748b', callback: v => '$' + (v/1000).toFixed(0) + 'K' }}, grid: {{ color: '#1e293b' }}, position: 'left' }},
      y2: {{ ticks: {{ color: '#f59e0b', callback: v => v + '%' }}, grid: {{ display: false }}, position: 'right' }}
    }}
  }}
}});

// ── Channel ROAS bar ──────────────────────────────────────────────────────────
new Chart(document.getElementById('channelChart'), {{
  type: 'bar',
  data: {{
    labels: chLabels,
    datasets: [
      {{ label: 'Revenue', data: chRev, backgroundColor: 'rgba(99,102,241,0.8)' }},
      {{ label: 'Spend',   data: chSpend, backgroundColor: 'rgba(239,68,68,0.7)' }},
    ]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#64748b', callback: v => '$' + (v/1000).toFixed(0) + 'K' }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }}
    }}
  }}
}});

// ── LTV curves ────────────────────────────────────────────────────────────────
const ltvLabels = Array.from({{length: maxMonths + 1}}, (_,i) => 'M' + i);
new Chart(document.getElementById('ltvChart'), {{
  type: 'line',
  data: {{
    labels: ltvLabels,
    datasets: ltvDatasets.map(d => ({{
      label: d.label,
      data: d.data,
      borderColor: d.color,
      backgroundColor: 'transparent',
      borderWidth: 2,
      pointRadius: 2,
      tension: 0.3
    }}))
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8', boxWidth: 12 }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#64748b' }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#64748b', callback: v => '$' + v }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});

// ── Payback + CAC ─────────────────────────────────────────────────────────────
new Chart(document.getElementById('paybackChart'), {{
  type: 'bar',
  data: {{
    labels: pbMonths,
    datasets: [
      {{
        label: 'CAC ($)',
        data: pbCac,
        backgroundColor: 'rgba(239,68,68,0.7)',
        yAxisID: 'y',
        order: 2,
      }},
      {{
        label: 'Payback (months)',
        data: pbPeriods,
        type: 'line',
        borderColor: '#22c55e',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 4,
        yAxisID: 'y2',
        order: 1,
      }}
    ]
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x:  {{ ticks: {{ color: '#64748b' }}, grid: {{ color: '#1e293b' }} }},
      y:  {{ ticks: {{ color: '#64748b', callback: v => '$' + v }}, grid: {{ color: '#1e293b' }}, position: 'left' }},
      y2: {{ ticks: {{ color: '#22c55e', callback: v => v + ' mo' }}, grid: {{ display: false }}, position: 'right',
              min: 0, max: 6 }}
    }}
  }}
}});
</script>
</body>
</html>"""

with open(OUT, 'w') as f:
    f.write(HTML)
print(f"Dashboard written to {OUT}  ({len(HTML)//1024}KB)")
