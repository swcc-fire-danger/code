"""
build_weather_history.py — ONE-TIME pull of daily weather history from FEMS for the FDRA stations.

Used by the FDRA Climate Cards (weather percentiles: Max/Min Temp, Max/Min RH, Max Wind, Max Gust, Solar Rad).
Run it from GitHub Actions: "Weather History (one time)". Run it again only when the history period changes
(for example 2005-2025): set the years in the workflow inputs, like the "FDRA History" workflow.

Output (saved to the fdra-climo release):
  data/history_<START>_<END>_daily_weather.csv
      sid, summary_date, tmin, tmax, rhmin, rhmax, wind, gust, solar, precip   (observed days only)

Uses the same FEMS endpoint and secrets as the existing pipeline. Does not change any other file.
"""
import os
import time

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

ENDPOINT = "https://fems.fs2c.usda.gov/api/ext-climatology/graphql"  # PROD
DATA_DIR = os.environ.get("FEMS_DATA_DIR", "data")
START_YEAR = int(os.environ.get("CLIMO_START_YEAR", "2005"))
END_YEAR = int(os.environ.get("CLIMO_END_YEAR", "2024"))
OUT = os.path.join(DATA_DIR, f"history_{START_YEAR}_{END_YEAR}_daily_weather.csv")
STATION_BATCH = 40

AUTH = HTTPBasicAuth(os.environ["FEMS_USERNAME"], os.environ["FEMS_API_KEY"])
HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
           "User-Agent": "FEMS-NM-AZ-GitHubActions/1.0"}

Q_WX_MINMAX = """
query WxMinMax($startDate: Date!, $endDate: Date, $stationIds: String) {
  wxMinMax(startDate: $startDate, endDate: $endDate, stationIds: $stationIds, hasHistoricData: ALL) {
    data { station_id summary_date observation_type
           temperature_min temperature_max relative_humidity_min relative_humidity_max
           wind_speed_max peak_gust_speed_max solar_radiation_max daily_precipitation_total }
  }
}
"""
RENAME = {"temperature_min": "tmin", "temperature_max": "tmax", "relative_humidity_min": "rhmin",
          "relative_humidity_max": "rhmax", "wind_speed_max": "wind", "peak_gust_speed_max": "gust",
          "solar_radiation_max": "solar", "daily_precipitation_total": "precip"}


def norm_id(v):
    v = str(v).strip()
    if v.endswith(".0") and v[:-2].isdigit():
        v = v[:-2]
    return v.lstrip("0") or "0"


def gql(variables, tries=4):
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(ENDPOINT, headers=HEADERS, auth=AUTH,
                              json={"query": Q_WX_MINMAX, "variables": variables}, timeout=600)
            ct = r.headers.get("content-type", "")
            j = r.json() if "application/json" in ct else {"errors": [{"message": r.text[:300]}]}
            if j.get("errors"):
                raise RuntimeError(j["errors"])
            r.raise_for_status()
            return j["data"]["wxMinMax"]["data"] or []
        except Exception as e:
            print(f"  attempt {attempt} failed: {e}")
            if attempt == tries:
                raise
            time.sleep(15 * attempt)


def main():
    fdra = pd.read_csv(os.path.join(DATA_DIR, "r3_fdra.csv"), dtype=str).dropna(subset=["FDRA_ZONE"])
    fdra = fdra[fdra["FDRA_ZONE"].str.strip() != ""]
    stations = sorted({norm_id(x) for s in fdra["SIG_RAWS"].dropna() for x in str(s).split(",") if x.strip()})
    print(f"{len(stations)} FDRA stations, {START_YEAR}-{END_YEAR}")

    rows = []
    for year in range(START_YEAR, END_YEAR + 1):
        n = 0
        for i in range(0, len(stations), STATION_BATCH):
            part = stations[i:i + STATION_BATCH]
            data = gql({"startDate": f"{year}-01-01", "endDate": f"{year}-12-31", "stationIds": ",".join(part)})
            rows.extend(data)
            n += len(data)
            time.sleep(2)
        print(f"  {year}: {n} rows")

    wx = pd.DataFrame(rows)
    if wx.empty:
        raise RuntimeError("FEMS returned no weather history — nothing written")
    wx = wx[wx["observation_type"] == "O"].copy()
    wx["sid"] = wx["station_id"].map(norm_id)
    wx = wx.rename(columns=RENAME)
    for c in RENAME.values():
        wx[c] = pd.to_numeric(wx[c], errors="coerce")
    wx = wx.drop_duplicates(["sid", "summary_date"], keep="last").sort_values(["sid", "summary_date"])
    wx[["sid", "summary_date", *RENAME.values()]].to_csv(OUT, index=False)
    print(f"Wrote {OUT}: {len(wx)} station-days, {wx['sid'].nunique()} stations")


if __name__ == "__main__":
    main()
