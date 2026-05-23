"""Yahoo Finance data loader for RRG charts."""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from plugins_google_drive_data import GoogleDriveDataPlugin

logger = logging.getLogger(__name__)


INDEX_TICKER_CANDIDATES = {
    "NIFTY 50": ["^NSEI"],
    "NIFTY": ["^NSEI"],
    "NIFTY BANK": ["^NSEBANK"],
    "NIFTY FINANCIAL SERVICES": ["NIFTY_FIN_SERVICE.NS", "^CNXFIN"],
    "NIFTY IT": ["^CNXIT"],
    "NIFTY FMCG": ["^CNXFMCG"],
    "NIFTY PHARMA": ["^CNXPHARMA", "NIFTYPHARMA.NS"],
    "NIFTY HEALTHCARE": ["NIFTY_HEALTHCARE.NS", "^CNXHEALTHCARE"],
    "NIFTY AUTO": ["^CNXAUTO"],
    "NIFTY METAL": ["^CNXMETAL"],
    "NIFTY ENERGY": ["^CNXENERGY"],
    "NIFTY REALTY": ["^CNXREALTY"],
    "NIFTY PSU BANK": ["^CNXPSUBANK", "NIFTY_PSU_BANK.NS"],
    "NIFTY INFRASTRUCTURE": ["NIFTY_INFRA.NS", "^CNXINFRA"],
}


class YFinanceLoader:
    timeframes = {"daily": "1d", "weekly": "1d", "monthly": "1d"}

    def __init__(self, config: Optional[dict] = None, tf: Optional[str] = "daily", end_date: Optional[datetime] = None, period: int = 160):
        self.closed = False
        self.tf = tf or "daily"
        self.end_date = end_date or datetime.now()
        self.period = period
        self.cache_plugin = GoogleDriveDataPlugin()

    def _to_yf_ticker_candidates(self, symbol: str) -> list[str]:
        raw = (symbol or "").strip()
        if raw.upper() in INDEX_TICKER_CANDIDATES:
            return INDEX_TICKER_CANDIDATES[raw.upper()]
        if raw.endswith("-EQ"):
            return [f"{raw[:-3]}.NS"]
        return [raw]


    def _merge_incremental(self, cached_df: Optional[pd.DataFrame], fresh_df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if cached_df is None or cached_df.empty:
            return fresh_df
        if fresh_df is None or fresh_df.empty:
            return cached_df
        merged = pd.concat([cached_df, fresh_df])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        return merged

    def get(self, symbol: str, token: Optional[str] = None) -> Optional[pd.DataFrame]:
        try:
            if self.tf == "daily":
                days_back = self.period + 80
            elif self.tf == "weekly":
                days_back = (self.period + 20) * 7
            else:
                days_back = (self.period + 20) * 30

            cached_df = self.cache_plugin.load_cached(symbol, self.tf)
            if cached_df is not None and len(cached_df) > 1:
                start_date = max(cached_df.index.max().to_pydatetime() - timedelta(days=10), self.end_date - timedelta(days=days_back))
            else:
                start_date = self.end_date - timedelta(days=days_back)
            df = None
            used_ticker = None
            for ticker in self._to_yf_ticker_candidates(symbol):
                candidate_df = yf.download(
                    ticker,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=(self.end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                    interval=self.timeframes.get(self.tf, "1d"),
                    progress=False,
                    auto_adjust=False,
                    threads=False,
                )
                if candidate_df is not None and not candidate_df.empty:
                    df = candidate_df
                    used_ticker = ticker
                    break

            if df is None or df.empty:
                logger.warning("No data returned for %s (tried: %s)", symbol, self._to_yf_ticker_candidates(symbol))
                return None

            df = df.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]].copy()
            df.index = pd.to_datetime(df.index)
            df.sort_index(inplace=True)

            if self.tf == "weekly":
                df = df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
            elif self.tf == "monthly":
                df = df.resample("ME").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()

            merged_df = self._merge_incremental(cached_df, df)
            if merged_df is None or merged_df.empty:
                return None
            self.cache_plugin.save_cached(symbol, self.tf, merged_df)
            logger.debug("Loaded %s rows for %s via %s", len(merged_df), symbol, used_ticker)
            return merged_df.tail(self.period)
        except Exception as e:
            logger.error("Error fetching data for %s: %s", symbol, e)
            return None

    def get_storage_status(self) -> dict:
        return self.cache_plugin.get_storage_status()

    def login_google_drive(self, client_secrets_path: str, folder_id: str):
        return self.cache_plugin.interactive_login(client_secrets_path, folder_id)

    def close(self):
        self.closed = True
