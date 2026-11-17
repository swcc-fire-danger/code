"""
climate_cards.py — data for the FDRA Climate Cards and the all-FDRA historical percentile table.

Called by build_web_data.py (same "today", observed and forecast days as the rest of the website).
Writes WEB_OUT_DIR/climate.json.

Percentile table (rows Min, 10th ... 99th, Max) per FDRA, from the history period files in the fdra-climo release:
  * NFDRS indices and fuel moisture: r3_fdra_percentile_historical.csv (the same percentiles the website rates
    with; steps 1-100, so the "Min" row is the 1st percentile).
  * SFDI / VPD / HDWI: r3_fdra_percentile_extra.csv (the same numbers the website already rates with).
  * Weather: history_<start>_<end>_daily_weather.csv (one-time "Weather History" workflow); same rule as NFDR.
Rows are "danger" percentiles: for columns where a LOWER value means more fire danger (fuel moisture, GSI, RH),
the row "90th" shows the value only 10% of days were at or below (so every row reads the same way).

Recent and forecast rows: average of the past 7 observed days, today, and the average of today + the next 6
forecast days (the website's "7-Day Forecast"). Each value also gets its danger percentile ("rank").
"""
import glob
import json
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd

ROW_PCTS = [0, 10, 20, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95, 97, 98, 99, 100]
ROW_LABELS = ["Min" if p == 0 else "Max" if p == 100 else f"{p}th" for p in ROW_PCTS]

# key, label, group, raw history field (NFDR) or weather field, decimals, lower value = more danger
COLS = [
    ("erc", "ERC", "NFDRS Indices", "erc", 1, False),
    ("bi", "BI", "NFDRS Indices", "bi", 1, False),
    ("ic", "IC", "NFDRS Indices", "ignition_component_max", 1, False),
    ("sc", "SC", "NFDRS Indices", "spread_component_max", 1, False),
    ("sfdi", "SFDI", "NFDRS Indices", None, 0, False),
    ("hdwi", "HDWI", "Other Indices", None, 0, False),
    ("vpd", "VPD", "Other Indices", None, 0, False),
    ("kbdi", "KBDI", "Other Indices", "kbdi", 0, False),
    ("gsi", "GSI", "Other Indices", "gsi", 2, True),
    ("fm1", "1Hr", "Fuel Moisture", "one_hr_tl_fuel_moisture_min", 1, True),
    ("fm10", "10Hr", "Fuel Moisture", "ten_hr_tl_fuel_moisture_min", 1, True),
    ("fm100", "100Hr", "Fuel Moisture", "hun_hr_tl_fuel_moisture_min", 1, True),
    ("fm1000", "1000Hr", "Fuel Moisture", "thou_hr_tl_fuel_moisture_min", 1, True),
    ("herb", "Herb", "Fuel Moisture", "herbaceous_lfi_fuel_moisture", 0, True),
    ("woody", "Woody", "Fuel Moisture", "woody_lfi_fuel_moisture", 0, True),
    ("tmax", "Max T", "Weather", "tmax", 0, False),
    ("tmin", "Min T", "Weather", "tmin", 0, False),
    ("rhmax", "Max RH", "Weather", "rhmax", 0, True),
    ("rhmin", "Min RH", "Weather", "rhmin", 0, True),
    ("wind", "Max Wind", "Weather", "wind", 0, False),
    ("gust", "Max Gust", "Weather", "gust", 0, False),
    ("solar", "Solar Rad", "Weather", "solar", 0, False),
]
WX_KEYS = {"tmax", "tmin", "rhmax", "rhmin", "wind", "gust", "solar"}
EXTRA = {"sfdi": "SFDI", "vpd": "VPD", "hdwi": "HDWI"}
HIST = {"erc": "AvgERC", "bi": "AvgBI", "ic": "AvgIC", "sc": "AvgSC", "kbdi": "AvgKBDI", "gsi": "AvgGSI",
        "fm1": "Avg1HrFM", "fm10": "Avg10HrFM", "fm100": "Avg100HrFM", "fm1000": "Avg1000HrFM",
        "herb": "AvgHerbFM", "woody": "AvgWoodFM"}
# recent / forecast values: column -> source column in the daily obs/forecast files
NFDR_SRC = {"erc": "Max ERC", "bi": "Max BI", "ic": "Max IC", "sc": "Max SC", "kbdi": "KBDI", "gsi": "GSI",
            "fm1": "Min 1 hr FM", "fm10": "Min 10 hr FM", "fm100": "Min 100 hr FM", "fm1000": "Min 1000 hr FM",
            "herb": "Herb FM", "woody": "Woody FM"}
WX_SRC = {"tmax": "Max Temp F", "tmin": "Min Temp F", "rhmax": "Max RH pct", "rhmin": "Min RH pct",
          "wind": "Max Wind mph", "gust": "Max Wind Gust mph", "solar": "Max SR Wm2"}


def newest(data_dir, pattern):
    """The history file with the latest end year, e.g. history_2005_2025_daily_weather.csv over ..._2024_..."""
    files = glob.glob(os.path.join(data_dir, pattern))
    def years(f):
        m = re.search(r"history_(\d{4})_(\d{4})_", os.path.basename(f))
        return (int(m.group(2)), -int(m.group(1))) if m else (0, 0)
    files = [f for f in files if years(f) != (0, 0)]
    return max(files, key=years) if files else None


def period_of(path):
    m = re.search(r"history_(\d{4})_(\d{4})_", os.path.basename(path or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def rnd(x, places):
    if x is None or pd.isna(x):
        return None
    v = round(float(x) + (1e-9 if x >= 0 else -1e-9), places)
    return int(v) if places == 0 else v


def vpd_pa(tmax_f, rhmin):
    tc = (tmax_f - 32) * 5 / 9
    es = 6.112 * np.exp((17.67 * tc) / (tc + 243.5))
    return (es - (rhmin / 100) * es) * 100


def complete_daily(sub, field, sig):
    """FDRA daily average on days when every FDRA station reported (the percentile-history rule)."""
    s = sub.dropna(subset=[field])
    if s.empty:
        return pd.Series(dtype=float)
    n = s.groupby("summary_date")["sid"].nunique()
    daily = s.groupby("summary_date")[field].mean()
    return daily[n[n == len(set(sig))].index]


def danger_rank(v, steps, inv):
    """Danger percentile (0-100) of value v; steps = values at percentiles 0..100 (ascending)."""
    if v is None or steps is None or pd.isna(v):
        return None
    arr = np.asarray(steps, dtype=float)
    pos = int(np.searchsorted(arr, float(v), side="right")) - 1     # highest percentile at or below v
    p = max(0, min(100, pos))
    if inv:   # lower = more danger: the share of days at or BELOW this value, flipped
        pos_l = int(np.searchsorted(arr, float(v), side="left"))
        p = 100 - max(0, min(100, pos_l))
    return p


def build(out_dir, data_dir, fdra_rows, nfdr, fc, wxd, fwx, today, obs_days, future, climo=None, extra=None, hist=None):
    raw_path = newest(data_dir, "history_*_daily_erc_bi.csv")
    wx_path = newest(data_dir, "history_*_daily_weather.csv")
    whist = pd.read_csv(wx_path, dtype={"sid": str}) if wx_path else None
    if whist is not None:
        whist["sid"] = whist["sid"].map(lambda v: str(v).strip().lstrip("0") or "0")
    per = period_of(raw_path) or period_of(wx_path)
    wper = period_of(wx_path)
    if wper and per and wper != per:
        print(f"NOTE: weather history is {wper[0]}-{wper[1]} but NFDR history is {per[0]}-{per[1]}; "
              f"run the Weather History workflow with the same years. Weather percentiles left blank.")
        whist = None

    past = list(obs_days[-7:])
    nxt = ([today] if today else []) + list(future[:6])
    fdras = {}
    for r in fdra_rows:
        key, sig, fm = r["key"], r["sig"], r["fm"]
        table, steps, days = {}, {}, {}
        hh = hist[hist["FDRA_ZONE"].str.strip() == key].sort_values("Percentiles") if hist is not None else None
        wsub = whist[whist["sid"].isin(sig)] if whist is not None else None
        for k, _, _, field, dec, inv in COLS:
            daily = None
            if k in EXTRA:
                if extra is not None:
                    e = extra[extra["FDRA_ZONE"].str.strip() == key].sort_values("Percentiles")
                    if len(e) == 101:
                        steps[k] = e[EXTRA[k]].astype(float).tolist()
            elif k in WX_KEYS:
                daily = complete_daily(wsub, field, sig) if wsub is not None and field in wsub else None
            elif hh is not None and len(hh) == 100 and HIST[k] in hh:
                v = hh[HIST[k]].astype(float).tolist()
                steps[k] = [v[0]] + v            # step 0 = the 1st percentile (the file starts at 1)
            if daily is not None and len(daily):
                steps[k] = np.percentile(daily.values, range(0, 101)).tolist()
                days[k] = int(len(daily))
            if k in steps:
                s = steps[k]
                table[k] = [rnd(s[100 - p] if inv else s[p], dec) for p in ROW_PCTS]

        def values(nf, wx, ds):
            if not ds:
                return None
            s_n = nf[(nf["sid"].isin(sig)) & (nf["fuel_model"] == fm) & (nf["summary_date"].isin(ds))]
            s_w = wx[(wx["sid"].isin(sig)) & (wx["summary_date"].isin(ds))] if len(wx) else wx
            d = pd.DataFrame(index=sorted(ds))
            for k, c in NFDR_SRC.items():
                d[k] = pd.to_numeric(s_n[c], errors="coerce").groupby(s_n["summary_date"]).mean() if c in s_n else np.nan
            for k, c in WX_SRC.items():
                d[k] = pd.to_numeric(s_w[c], errors="coerce").groupby(s_w["summary_date"]).mean() if len(s_w) and c in s_w else np.nan
            d["vpd"] = [vpd_pa(t, h) if pd.notna(t) and pd.notna(h) else np.nan for t, h in zip(d["tmax"], d["rhmin"])]
            a = d.mean(numeric_only=True)
            out = {k: a.get(k) for k in d.columns}
            out["sfdi"] = out["erc"] * out["bi"] if pd.notna(out.get("erc")) and pd.notna(out.get("bi")) else None
            out["hdwi"] = (out["vpd"] / 100) * (out["wind"] * 0.447) if pd.notna(out.get("vpd")) and pd.notna(out.get("wind")) else None
            vals, rank = {}, {}
            for k, _, _, _, dec, inv in COLS:
                v = out.get(k)
                vals[k] = rnd(v, dec) if v is not None and pd.notna(v) else None
                rank[k] = danger_rank(v, steps.get(k), inv)
            return {"v": vals, "rank": rank}

        recent = {"past7": values(nfdr, wxd, past), "today": values(fc, fwx, [today] if today else []),
                  "next7": values(fc, fwx, nxt)}
        for n in (3, 5):   # shorter averages for the card's 3 / 5 / 7-day choice
            recent[f"past{n}"] = values(nfdr, wxd, past[-n:])
            recent[f"next{n}"] = values(fc, fwx, nxt[:n])
        normal = None
        if climo is not None and today:
            md = today[5:]
            c = climo[(climo["FDRA_ZONE"].str.strip() == key) & (climo["month_day"] == md)]
            if len(c):
                c = c.iloc[0]
                normal = {"erc_avg": rnd(c.erc_avg, 1), "erc_max": rnd(c.erc_max, 1), "bi_avg": rnd(c.bi_avg, 1),
                          "bi_max": rnd(c.bi_max, 1), "years": int(c.years) if pd.notna(c.years) else None}
        fdras[key] = {"table": table, "days": days, "recent": recent, "normal": normal}

    doc = {
        "period": per, "weather_period": wper if whist is not None else None,
        "rows": ROW_LABELS, "pcts": ROW_PCTS,
        "cols": [{"k": k, "label": l, "group": g, "dec": d, "inv": inv} for k, l, g, _, d, inv in COLS],
        "dates": {"past7": [past[0], past[-1]] if past else None, "today": today,
                  "next7": [nxt[0], nxt[-1]] if nxt else None,
                  **{f"past{n}": [past[-n:][0], past[-1]] if past else None for n in (3, 5)},
                  **{f"next{n}": [nxt[0], nxt[:n][-1]] if nxt else None for n in (3, 5)}},
        "fdras": fdras,
    }
    path = os.path.join(out_dir, "climate.json")
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"), allow_nan=False)
    print(f"Wrote {path}: {len(fdras)} FDRAs, period {per}, weather history {'yes' if whist is not None else 'not yet'}")
