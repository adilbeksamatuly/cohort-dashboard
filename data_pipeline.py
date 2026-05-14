"""
Fetches data from BigQuery and computes cohort analytics.
Uses Supabase as reliable persistent storage (replaces parquet cache).
Outputs JSON files consumed by the dashboard.

Usage:
  python data_pipeline.py               # incremental (default)
  python data_pipeline.py --full-reload # redownload all BQ rows and overwrite Supabase
"""
import json
import os
import sys
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

PROJECT = "hopeful-list-429812-f3"
DATASET = "performance_analytics"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── BigQuery ──────────────────────────────────────────────────────────────────
print("Connecting to BigQuery...")
BQ_KEY_JSON = os.environ.get("BQ_KEY_JSON")
if BQ_KEY_JSON:
    creds = service_account.Credentials.from_service_account_info(json.loads(BQ_KEY_JSON))
else:
    KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bq_key.json")
    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
client = bigquery.Client(project=PROJECT, credentials=creds)

# ── 1. Incremental BQ → Supabase ─────────────────────────────────────────────
FULL_RELOAD = "--full-reload" in sys.argv

payments_ref = client.get_table(f"{PROJECT}.{DATASET}.all_payments_daily")
bq_total     = payments_ref.num_rows
sb_result    = sb.table("payments").select("*", count="exact").limit(1).execute()
sb_count     = sb_result.count or 0

print(f"BQ: {bq_total:,} rows | Supabase: {sb_count:,} rows")

if FULL_RELOAD:
    print(f"  Full reload — downloading all {bq_total:,} rows from BQ...")
    start_idx  = 0
    n_download = bq_total
else:
    OVERLAP    = 10_000
    start_idx  = max(0, sb_count - OVERLAP)
    n_download = bq_total - start_idx

if n_download <= 0:
    print("  Supabase up to date.")
else:
    if not FULL_RELOAD:
        print(f"  Downloading {n_download:,} rows from BQ (includes {OVERLAP:,}-row overlap)...")
    df_new = client.list_rows(payments_ref, start_index=start_idx, max_results=n_download).to_dataframe()

    # Convert types for JSON serialisation
    df_new = df_new.copy()
    df_new["acquisition_date"]      = df_new["acquisition_date"].astype(str)
    df_new["transaction_timestamp"] = pd.to_datetime(df_new["transaction_timestamp"], utc=True).astype(str)
    df_new["settle_timestamp"]      = pd.to_datetime(df_new["settle_timestamp"],      utc=True).astype(str)
    df_new["gross_amount_in_reporting_currency"] = df_new["gross_amount_in_reporting_currency"].astype(int)

    # Deduplicate by order_id within the downloaded batch
    df_new = df_new.drop_duplicates(subset=["order_id"], keep="last")

    def upsert_batch(batch, retries=5):
        for attempt in range(retries):
            try:
                sb.table("payments").upsert(batch, on_conflict="order_id").execute()
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt
                print(f"\n  Timeout, retry {attempt+1}/{retries} in {wait}s...")
                time.sleep(wait)

    # Upsert in batches of 200 rows with retry on timeout
    BATCH = 200
    total = len(df_new)
    for i in range(0, total, BATCH):
        upsert_batch(df_new.iloc[i:i+BATCH].to_dict("records"))
        print(f"  Upserted {min(i+BATCH, total):,}/{total:,}", end="\r")
    print(f"\n  Supabase updated: ~{sb_count + max(0, bq_total - sb_count):,} rows total")

# ── 2. Load all payments from Supabase ───────────────────────────────────────
print("Loading all payments from Supabase...")
PAGE = 1000
rows = []
offset = 0
while True:
    res = sb.table("payments").select("*").order("order_id").range(offset, offset + PAGE - 1).execute()
    rows.extend(res.data)
    if len(res.data) < PAGE:
        break
    offset += PAGE
    if offset % 50000 == 0:
        print(f"  Loaded {offset:,} rows so far...", end="\r")
df = pd.DataFrame(rows)
print(f"  Loaded {len(df):,} rows")

# ── 3. Load spends from BQ (tiny table, always fresh) ────────────────────────
print("Loading spends table...")
spends_ref = client.get_table(f"{PROJECT}.{DATASET}.utm_spends_agg")
df_spend   = client.list_rows(spends_ref).to_dataframe()
print(f"  Loaded {len(df_spend):,} rows")

# ── 4. Prepare payments ───────────────────────────────────────────────────────
df["acquisition_date"]      = pd.to_datetime(df["acquisition_date"])
df["transaction_timestamp"] = pd.to_datetime(df["transaction_timestamp"], utc=True, format="ISO8601")
df["txn_month"]             = df["transaction_timestamp"].dt.to_period("M")
df["cohort_month"]          = df["acquisition_date"].dt.to_period("M")
df["months_since_acq"]      = (df["txn_month"] - df["cohort_month"]).apply(
    lambda x: x.n if hasattr(x, "n") else 0
)
df = df[df["months_since_acq"] >= 0]

# amounts in cents → dollars
df["net_amount_in_reporting_currency"]   = df["net_amount_in_reporting_currency"] / 100
df["gross_amount_in_reporting_currency"] = df["gross_amount_in_reporting_currency"] / 100

# ── 5. Cohort revenue matrices ───────────────────────────────────────────────
print("Computing cohort revenue matrices (net + gross)...")

def make_cohort_pivot(col):
    cr = (df.groupby(["cohort_month", "months_since_acq"])[col]
            .sum().reset_index())
    cr["cohort_month"] = cr["cohort_month"].astype(str)
    p = cr.pivot(index="cohort_month", columns="months_since_acq", values=col).fillna(0)
    return p.sort_index()

pivot       = make_cohort_pivot("net_amount_in_reporting_currency")
pivot_gross = make_cohort_pivot("gross_amount_in_reporting_currency")
cumulative       = pivot.cumsum(axis=1)
cumulative_gross = pivot_gross.cumsum(axis=1)

# ── 6. New users per cohort ──────────────────────────────────────────────────
first_txns = df[df["transaction_type"] == "first"]
new_users_per_cohort = (
    first_txns.groupby("cohort_month")["customer_account_id"]
    .nunique().reset_index()
)
new_users_per_cohort["cohort_month"] = new_users_per_cohort["cohort_month"].astype(str)
new_users_per_cohort.columns = ["cohort_month", "new_users"]

# ── 7. Monthly spend ─────────────────────────────────────────────────────────
df_spend["date"]  = pd.to_datetime(df_spend["date"])
df_spend["month"] = df_spend["date"].dt.to_period("M")
monthly_spend = (
    df_spend.groupby(["month", "attribution_source"])["spend"]
    .sum().reset_index()
)
monthly_spend["month"] = monthly_spend["month"].astype(str)
total_monthly_spend = df_spend.groupby("month")["spend"].sum().reset_index()
total_monthly_spend["month"] = total_monthly_spend["month"].astype(str)

# ── 8. CAC per cohort ────────────────────────────────────────────────────────
cac_df = new_users_per_cohort.merge(
    total_monthly_spend, left_on="cohort_month", right_on="month", how="left"
)
cac_df["cac"] = (cac_df["spend"] / cac_df["new_users"]).round(2).fillna(0)

# ── 9. Payback period ────────────────────────────────────────────────────────
print("Computing payback periods...")
payback_rows = []
for cohort in cumulative.index:
    row     = cumulative.loc[cohort]
    nu_row  = new_users_per_cohort[new_users_per_cohort["cohort_month"] == cohort]
    cac_row = cac_df[cac_df["cohort_month"] == cohort]
    if nu_row.empty or cac_row.empty:
        continue
    nu  = int(nu_row.iloc[0]["new_users"])
    cac = float(cac_row.iloc[0]["cac"])
    if nu == 0 or cac == 0:
        continue
    rev_per_user  = row / nu
    payback_month = next((int(m) for m in sorted(rev_per_user.index) if rev_per_user[m] >= cac), None)
    payback_rows.append({
        "cohort_month":  cohort,
        "new_users":     nu,
        "cac":           round(cac, 2),
        "payback_month": payback_month,
        "ltv_month_6":   round(float(rev_per_user.get(6, rev_per_user.iloc[-1])) if len(rev_per_user) > 0 else 0, 2),
    })

# ── 10. Channel breakdown ────────────────────────────────────────────────────
channel_rev = (
    df.groupby("attributed_channel")["net_amount_in_reporting_currency"]
    .sum().reset_index()
    .rename(columns={"net_amount_in_reporting_currency": "revenue"})
    .sort_values("revenue", ascending=False)
)
channel_spend = (
    df_spend.groupby("attribution_source")["spend"]
    .sum().reset_index()
    .rename(columns={"attribution_source": "attributed_channel"})
)
channel_summary = channel_rev.merge(channel_spend, on="attributed_channel", how="left").fillna(0)
channel_summary["roas"]    = (channel_summary["revenue"] / channel_summary["spend"]).replace([float("inf")], 0).round(2)
channel_summary["revenue"] = channel_summary["revenue"].round(2)
channel_summary["spend"]   = channel_summary["spend"].round(2)

# ── 11. Monthly overview ─────────────────────────────────────────────────────
monthly_revenue = (
    df.groupby("txn_month")["net_amount_in_reporting_currency"]
    .sum().reset_index()
)
monthly_revenue["txn_month"] = monthly_revenue["txn_month"].astype(str)
monthly_revenue.columns = ["month", "revenue"]

# ── 12. Save JSON ─────────────────────────────────────────────────────────────
def to_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, default=str)
    print(f"  Saved {path}")

print("Saving data files...")

to_json({
    "cohorts": pivot.index.tolist(), "max_months": int(pivot.columns.max()),
    "matrix":     {c: {int(m): round(float(v), 2) for m, v in r.items() if v > 0} for c, r in pivot.iterrows()},
    "cumulative": {c: {int(m): round(float(v), 2) for m, v in r.items()} for c, r in cumulative.iterrows()},
}, f"{OUT_DIR}/cohort_revenue.json")

to_json({
    "cohorts": pivot_gross.index.tolist(), "max_months": int(pivot_gross.columns.max()),
    "matrix":     {c: {int(m): round(float(v), 2) for m, v in r.items() if v > 0} for c, r in pivot_gross.iterrows()},
    "cumulative": {c: {int(m): round(float(v), 2) for m, v in r.items()} for c, r in cumulative_gross.iterrows()},
}, f"{OUT_DIR}/cohort_gross_revenue.json")

to_json(new_users_per_cohort.to_dict("records"), f"{OUT_DIR}/new_users.json")
to_json(monthly_spend.to_dict("records"),        f"{OUT_DIR}/monthly_spend.json")
to_json(payback_rows,                            f"{OUT_DIR}/payback.json")
to_json(channel_summary.to_dict("records"),      f"{OUT_DIR}/channel_summary.json")
to_json(monthly_revenue.to_dict("records"),      f"{OUT_DIR}/monthly_revenue.json")
to_json(cac_df[["cohort_month", "new_users", "spend", "cac"]].to_dict("records"), f"{OUT_DIR}/cac.json")

print("\nDone!")
print(f"  Cohorts:    {len(pivot)} months")
print(f"  Revenue:    ${df['net_amount_in_reporting_currency'].sum():,.0f}")
print(f"  Spend:      ${df_spend['spend'].sum():,.0f}")
print(f"  New users:  {len(first_txns['customer_account_id'].unique()):,}")
print(f"  Channels:   {df['attributed_channel'].nunique()}")
