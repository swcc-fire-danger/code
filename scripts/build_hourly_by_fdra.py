"""
build_hourly_by_fdra.py — one download per FDRA with the last year of hourly observations for its stations.

Reads (from FEMS_DATA_DIR, default "data"):
  history_hourly_weather.csv, history_hourly_nfdr.csv  (obs-history release; the pipeline keeps 1 year)
  r3_fdra.csv
Writes:
  data/hourly_by_fdra/<fdra-slug>.zip, each with two CSVs:
     "<FDRA> - hourly weather.csv"  every observed hour for the FDRA's stations
     "<FDRA> - hourly NFDRS (fuel model X).csv"  hourly NFDRS for the FDRA's own fuel model
The obs-daily workflow saves these to the "hourly-by-fdra" release; the website build copies them into the site,
so people download from the website, not from GitHub.
"""
import io
import os
import re
import zipfile

import pandas as pd

DATA_DIR = os.environ.get("FEMS_DATA_DIR", "data")
OUT_DIR = os.path.join(DATA_DIR, "hourly_by_fdra")
DROP = ["zoom_level", "hex", "pull_date", "masked_observation_time", "display_hour", "display_date"]


def norm_id(v):
    v = str(v).strip()
    if v.endswith(".0") and v[:-2].isdigit():
        v = v[:-2]
    return v.lstrip("0") or "0"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def read_filtered(path, sids, time_col, extra_filter=None):
    """Read a big CSV in chunks, keeping only the FDRA stations' observed rows."""
    parts = []
    for ch in pd.read_csv(path, dtype={"station_id": str}, chunksize=500_000, low_memory=False):
        ch["sid"] = ch["station_id"].map(norm_id)
        ch = ch[ch["sid"].isin(sids)]
        if extra_filter is not None:
            ch = extra_filter(ch)
        parts.append(ch)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if df.empty:
        return df
    if "pull_date" in df:
        df = df.sort_values("pull_date")
    keys = ["sid", time_col] + (["fuel_model"] if "fuel_model" in df else [])
    return df.drop_duplicates(keys, keep="last")


def main():
    fdra = pd.read_csv(os.path.join(DATA_DIR, "r3_fdra.csv"), dtype=str).dropna(subset=["FDRA_ZONE"])
    fdra = fdra[fdra["FDRA_ZONE"].str.strip() != ""]
    fdra["sig"] = fdra["SIG_RAWS"].map(lambda s: [norm_id(x) for x in str(s).split(",") if x.strip()])
    fdra["fm"] = fdra["FM_Long"].fillna("Y").str.strip().replace("", "Y")
    sids = {s for lst in fdra["sig"] for s in lst}
    fms = set(fdra["fm"])

    wx_path, nf_path = os.path.join(DATA_DIR, "history_hourly_weather.csv"), os.path.join(DATA_DIR, "history_hourly_nfdr.csv")
    wx = read_filtered(wx_path, sids, "observation_time_lst",
                       lambda c: c[c["observation_type"] == "O"]) if os.path.exists(wx_path) else pd.DataFrame()
    nf = read_filtered(nf_path, sids, "observation_time_lst",
                       lambda c: c[(c["nfdr_type"] == "O") & (c["fuel_model"].isin(fms))]) if os.path.exists(nf_path) else pd.DataFrame()
    print(f"Hourly weather rows: {len(wx)}, hourly NFDRS rows: {len(nf)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    for _, r in fdra.iterrows():
        key, name = r["FDRA_ZONE"].strip(), f'{r["Zone"].strip()} {r["FDRA"].strip()} FDRA'
        w = wx[wx["sid"].isin(r["sig"])] if len(wx) else wx
        n = nf[(nf["sid"].isin(r["sig"])) & (nf["fuel_model"] == r["fm"])] if len(nf) else nf
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if len(w):
                w = w.sort_values(["station_name", "observation_time_lst"]).drop(columns=["sid", *[c for c in DROP if c in w]])
                z.writestr(f"{name} - hourly weather.csv", w.to_csv(index=False))
            if len(n):
                n = n.sort_values(["station_name", "observation_time_lst"]).drop(columns=["sid", *[c for c in DROP if c in n]])
                z.writestr(f"{name} - hourly NFDRS (fuel model {r['fm']}).csv", n.to_csv(index=False))
            z.writestr("README.txt",
                       f"{name}\nStations: {', '.join(r['sig'])}\nFuel model: {r['fm']}\n"
                       "Observed hourly data from FEMS (Fire Environment Mapping System), the last 12 months.\n"
                       "Times are local (observation_time_lst).\n")
        with open(os.path.join(OUT_DIR, slug(key) + ".zip"), "wb") as f:
            f.write(buf.getvalue())
    total = sum(os.path.getsize(os.path.join(OUT_DIR, f)) for f in os.listdir(OUT_DIR))
    print(f"Wrote {len(fdra)} FDRA files to {OUT_DIR} ({total / 1e6:.1f} MB in all)")


if __name__ == "__main__":
    main()
