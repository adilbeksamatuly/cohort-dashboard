"""
Fetches data from BigQuery and computes cohort analytics.
Outputs JSON files consumed by the dashboard.
"""
import json
import os
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

PROJECT    = "hopeful-list-429812-f3"
DATASET    = "performance_analytics"
OUT_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(OUT_DIR, "payments_cache.parquet")

os.makedirs(OUT_DIR, exist_ok=True)

print("Connecting to BigQuery...")
BQ_KEY_JSON = os.environ.get("BQ_KEY_JSON")
if BQ_KEY_JSON:
    info = json.loads(BQ_KEY_JSON)
    creds = service_account.Credentials.from_service_account_info(info)
else:
    KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bq_key.json")
    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
client = bigquery.Client(project=PROJECT, credentials=creds)

# ── 1. Load payments (incremental via parquet cache) ─────────────────────────
payments_ref = client.get_table(f"{PROJECT}.{DATASET}.all_payments_daily")
total_rows   = payments_ref.num_rows

if os.path.exists(CACHE_FILE):
    print("Loading cached payments...")
    df_cached    = pd.read_parquet(CACHE_FILE)
    cached_rows  = len(df_cached)
    new_count    = total_rows - cached_rows
    if new_count > 0:
        print(f"  Cache: {cached_rows:,} rows — downloading {new_count:,} new rows...")
        df_new = client.list_rows(
            payments_ref, start_index=cached_rows, max_results=new_count
        ).to_dataframe()
        df = pd.concat([df_cached, df_new], ignore_index=True)
    else:
        print(f"  Cache up to date ({cached_rows:,} rows), no new data.")
        df = df_cached
else:
    print(f"No cache — downloading all {total_rows:,} rows...")
    df = client.list_rows(payments_ref).to_dataframe()

df.to_parquet(CACHE_FILE, index=False)
print(f"  Cache saved: {len(df):,} rows")

# ── 2. Load spends ────────────────────────────────────────────────────────────
print("Loading spends table...")
spends_ref = client.get_table(f"{PROJECT}.{DATASET}.utm_spends_agg")
df_spend = client.list_rows(spends_ref).to_dataframe()
print(f"  Loaded {len(df_spend):,} rows")

# ── 3. Prepare payments ───────────────────────────────────────────────────────
df['acquisition_date']       = pd.to_datetime(df['acquisition_date'])
df['transaction_timestamp']  = pd.to_datetime(df['transaction_timestamp'], utc=True)
df['txn_month']              = df['transaction_timestamp'].dt.to_period('M')
df['cohort_month']           = df['acquisition_date'].dt.to_period('M')
df['months_since_acq']       = (df['txn_month'] - df['cohort_month']).apply(lambda x: x.n if hasattr(x,'n') else 0)
# drop negative (data anomalies)
df = df[df['months_since_acq'] >= 0]

# amounts are in cents — convert to dollars
df['net_amount_in_reporting_currency']   = df['net_amount_in_reporting_currency'] / 100
df['gross_amount_in_reporting_currency'] = df['gross_amount_in_reporting_currency'] / 100

# ── 4. Cohort revenue matrices ───────────────────────────────────────────────
print("Computing cohort revenue matrices (net + gross)...")

def make_cohort_pivot(col):
    cr = (
        df.groupby(['cohort_month', 'months_since_acq'])[col]
        .sum()
        .reset_index()
    )
    cr['cohort_month'] = cr['cohort_month'].astype(str)
    p = cr.pivot(index='cohort_month', columns='months_since_acq', values=col).fillna(0)
    return p.sort_index()

pivot       = make_cohort_pivot('net_amount_in_reporting_currency')
pivot_gross = make_cohort_pivot('gross_amount_in_reporting_currency')

# cumulative revenue per cohort
cumulative       = pivot.cumsum(axis=1)
cumulative_gross = pivot_gross.cumsum(axis=1)

# ── 5. New users per cohort ───────────────────────────────────────────────────
first_txns = df[df['transaction_type'] == 'first']
new_users_per_cohort = (
    first_txns.groupby('cohort_month')['customer_account_id']
    .nunique()
    .reset_index()
)
new_users_per_cohort['cohort_month'] = new_users_per_cohort['cohort_month'].astype(str)
new_users_per_cohort.columns = ['cohort_month', 'new_users']

# ── 6. Monthly spend ──────────────────────────────────────────────────────────
df_spend['date'] = pd.to_datetime(df_spend['date'])
df_spend['month'] = df_spend['date'].dt.to_period('M')
monthly_spend = (
    df_spend.groupby(['month', 'attribution_source'])['spend']
    .sum()
    .reset_index()
)
monthly_spend['month'] = monthly_spend['month'].astype(str)

total_monthly_spend = (
    df_spend.groupby('month')['spend'].sum().reset_index()
)
total_monthly_spend['month'] = total_monthly_spend['month'].astype(str)

# ── 7. CAC per cohort ─────────────────────────────────────────────────────────
cac_df = new_users_per_cohort.merge(total_monthly_spend, left_on='cohort_month', right_on='month', how='left')
cac_df['cac'] = (cac_df['spend'] / cac_df['new_users']).round(2)
cac_df['cac'] = cac_df['cac'].fillna(0)

# ── 8. Payback period (month when cumulative revenue per user >= CAC) ─────────
print("Computing payback periods...")
payback_rows = []
for cohort in cumulative.index:
    row = cumulative.loc[cohort]
    nu_row = new_users_per_cohort[new_users_per_cohort['cohort_month'] == cohort]
    cac_row = cac_df[cac_df['cohort_month'] == cohort]
    if nu_row.empty or cac_row.empty:
        continue
    nu   = int(nu_row.iloc[0]['new_users'])
    cac  = float(cac_row.iloc[0]['cac']) if not cac_row.empty else 0
    if nu == 0 or cac == 0:
        continue
    rev_per_user = row / nu
    payback_month = None
    for m in sorted(rev_per_user.index):
        if rev_per_user[m] >= cac:
            payback_month = int(m)
            break
    payback_rows.append({
        'cohort_month': cohort,
        'new_users': nu,
        'cac': round(cac, 2),
        'payback_month': payback_month,
        'ltv_month_6': round(float(rev_per_user.get(6, rev_per_user.iloc[-1])) if len(rev_per_user) > 0 else 0, 2),
    })

# ── 9. Channel breakdown ──────────────────────────────────────────────────────
channel_rev = (
    df.groupby('attributed_channel')['net_amount_in_reporting_currency']
    .sum()
    .reset_index()
    .rename(columns={'net_amount_in_reporting_currency': 'revenue'})
    .sort_values('revenue', ascending=False)
)

channel_spend = (
    df_spend.groupby('attribution_source')['spend']
    .sum()
    .reset_index()
    .rename(columns={'attribution_source': 'attributed_channel'})
)

channel_summary = channel_rev.merge(channel_spend, on='attributed_channel', how='left').fillna(0)
channel_summary['roas'] = (channel_summary['revenue'] / channel_summary['spend']).replace([float('inf')], 0).round(2)
channel_summary['revenue'] = channel_summary['revenue'].round(2)
channel_summary['spend'] = channel_summary['spend'].round(2)

# ── 10. Monthly overview ──────────────────────────────────────────────────────
monthly_revenue = (
    df.groupby('txn_month')['net_amount_in_reporting_currency']
    .sum()
    .reset_index()
)
monthly_revenue['txn_month'] = monthly_revenue['txn_month'].astype(str)
monthly_revenue.columns = ['month', 'revenue']

# ── 11. Save JSON ─────────────────────────────────────────────────────────────
def to_json(obj, path):
    with open(path, 'w') as f:
        json.dump(obj, f, default=str)
    print(f"  Saved {path}")

print("Saving data files...")

# Cohort net revenue matrix
to_json({
    'cohorts': pivot.index.tolist(),
    'max_months': int(pivot.columns.max()),
    'matrix': {cohort: {int(m): round(float(v), 2) for m, v in row.items() if v > 0}
               for cohort, row in pivot.iterrows()},
    'cumulative': {cohort: {int(m): round(float(v), 2) for m, v in row.items()}
                   for cohort, row in cumulative.iterrows()},
}, f"{OUT_DIR}/cohort_revenue.json")

# Cohort gross (authorized) revenue matrix
to_json({
    'cohorts': pivot_gross.index.tolist(),
    'max_months': int(pivot_gross.columns.max()),
    'matrix': {cohort: {int(m): round(float(v), 2) for m, v in row.items() if v > 0}
               for cohort, row in pivot_gross.iterrows()},
    'cumulative': {cohort: {int(m): round(float(v), 2) for m, v in row.items()}
                   for cohort, row in cumulative_gross.iterrows()},
}, f"{OUT_DIR}/cohort_gross_revenue.json")

# New users per cohort
to_json(new_users_per_cohort.to_dict('records'), f"{OUT_DIR}/new_users.json")

# Monthly spend by channel
to_json(monthly_spend.to_dict('records'), f"{OUT_DIR}/monthly_spend.json")

# Payback periods
to_json(payback_rows, f"{OUT_DIR}/payback.json")

# Channel summary
to_json(channel_summary.to_dict('records'), f"{OUT_DIR}/channel_summary.json")

# Monthly revenue
to_json(monthly_revenue.to_dict('records'), f"{OUT_DIR}/monthly_revenue.json")

# CAC
to_json(cac_df[['cohort_month', 'new_users', 'spend', 'cac']].to_dict('records'), f"{OUT_DIR}/cac.json")

print("\nDone! All data files saved.")
print(f"\nSummary:")
print(f"  Cohorts: {len(pivot)} months")
print(f"  Total revenue: ${df['net_amount_in_reporting_currency'].sum():,.0f}")
print(f"  Total spend: ${df_spend['spend'].sum():,.0f}")
print(f"  Total new users: {len(first_txns['customer_account_id'].unique()):,}")
print(f"  Channels: {df['attributed_channel'].nunique()}")
