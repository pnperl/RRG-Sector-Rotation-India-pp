"""Google Drive cache plugin for market OHLCV data."""
import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class GoogleDriveDataPlugin:
    """Sync CSV files between local cache and Google Drive.

    Modes (automatic, no user interaction):
    1) Mounted drive directory mode via `GDRIVE_SYNC_DIR` (preferred)
    2) Google Drive API mode via service-account credentials
    3) Local cache-only fallback
    """

    def __init__(self) -> None:
        self.cache_dir = Path(os.getenv("DATA_CACHE_DIR", "data_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.drive_enabled = False
        self.drive = None
        self.folder_id = os.getenv("GDRIVE_FOLDER_ID", "").strip()
        self.master_cache_filename = "RRG_CACHE.csv"
        self.master_cache_local_path = self.cache_dir / self.master_cache_filename

        self.sync_dir = self._detect_sync_dir()
        if self.sync_dir is not None:
            self.sync_dir.mkdir(parents=True, exist_ok=True)
            self.drive_enabled = True
            logger.info("Google Drive mounted-dir mode enabled at: %s", self.sync_dir)
        else:
            self._init_drive()

        logger.info("Market data cache directory: %s", self.cache_dir.resolve())

    def _detect_sync_dir(self) -> Optional[Path]:
        configured = os.getenv("GDRIVE_SYNC_DIR", "").strip()
        candidates = [configured] if configured else [
            "/content/drive/MyDrive/RRGDataCache",
            str(Path.home() / "GoogleDrive" / "RRGDataCache"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return Path(c)
        return None

    def _init_drive(self) -> None:
        creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if not creds_file or not self.folder_id:
            logger.info("Google Drive plugin in local-cache mode (set GDRIVE_SYNC_DIR or GOOGLE_APPLICATION_CREDENTIALS + GDRIVE_FOLDER_ID to enable Drive sync).")
            return
        try:
            from pydrive2.auth import GoogleAuth
            from pydrive2.drive import GoogleDrive

            gauth = GoogleAuth(settings={
                "client_config_backend": "service",
                "service_config": {
                    "client_json_file_path": creds_file,
                },
                "save_credentials": False,
            })
            gauth.ServiceAuth()
            self.drive = GoogleDrive(gauth)
            self.drive_enabled = True
            self._ensure_master_cache_file()
        except Exception as e:
            logger.warning("Google Drive auth failed; using local cache only: %s", e)

    def _cache_filename(self, symbol: str, tf: str) -> str:
        safe = symbol.replace("/", "_").replace(" ", "_")
        return f"{safe}__{tf}.csv"

    def _cache_path(self, symbol: str, tf: str) -> Path:
        return self.cache_dir / self._cache_filename(symbol, tf)

    def _sync_path(self, filename: str) -> Optional[Path]:
        if self.sync_dir is None:
            return None
        return self.sync_dir / filename

    def load_cached(self, symbol: str, tf: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(symbol, tf)
        if not path.exists() and self.sync_dir is not None:
            sync_file = self._sync_path(path.name)
            if sync_file and sync_file.exists():
                import shutil
                shutil.copy2(sync_file, path)

        if not path.exists() and self.drive_enabled and self.sync_dir is None:
            self._sync_from_master_cache(symbol, tf, path)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df.sort_index()
        except Exception as e:
            logger.warning("Failed reading cached data %s: %s", path, e)
            return None

    def save_cached(self, symbol: str, tf: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, tf)
        df.sort_index().to_csv(path)

        if self.sync_dir is not None:
            sync_file = self._sync_path(path.name)
            if sync_file is not None:
                df.sort_index().to_csv(sync_file)

        if self.drive_enabled and self.sync_dir is None:
            self._update_master_cache(symbol, tf, df)

    def get_storage_status(self) -> dict:
        mode = "local"
        target = str(self.cache_dir.resolve())
        if self.sync_dir is not None:
            mode = "mounted_drive"
            target = str(self.sync_dir.resolve())
        elif self.drive_enabled and self.folder_id:
            mode = "drive_api"
            target = f"gdrive_folder_id:{self.folder_id}"
        return {"mode": mode, "target": target, "local_cache": str(self.cache_dir.resolve())}

    def interactive_login(self, client_secrets_path: str, folder_id: str) -> tuple[bool, str]:
        """Authenticate with Google Drive via OAuth (for Streamlit button flow)."""
        try:
            from pydrive2.auth import GoogleAuth
            from pydrive2.drive import GoogleDrive

            if not client_secrets_path or not Path(client_secrets_path).exists():
                return False, "client_secrets.json path is missing or invalid"
            if not folder_id:
                return False, "Google Drive Folder ID is required"

            gauth = GoogleAuth(settings={
                "client_config_backend": "file",
                "client_config_file": client_secrets_path,
                "save_credentials": True,
                "save_credentials_backend": "file",
                "save_credentials_file": str(self.cache_dir / "gdrive_user_credentials.json"),
                "get_refresh_token": True,
                "oauth_scope": ["https://www.googleapis.com/auth/drive"],
            })
            gauth.LocalWebserverAuth()
            self.drive = GoogleDrive(gauth)
            self.folder_id = folder_id.strip()
            self.drive_enabled = True
            self.sync_dir = None
            self._ensure_master_cache_file()
            return True, "Google Drive login successful"
        except Exception as e:
            logger.exception("Interactive Google Drive login failed")
            return False, f"Google Drive login failed: {e}"

    def _download_from_drive(self, filename: str, out_path: Path) -> None:
        try:
            q = f"'{self.folder_id}' in parents and title='{filename}' and trashed=false"
            files = self.drive.ListFile({'q': q}).GetList()
            if not files:
                return
            files[0].GetContentFile(str(out_path))
        except Exception as e:
            logger.warning("Drive download failed for %s: %s", filename, e)

    def _upload_to_drive(self, path: Path) -> None:
        try:
            q = f"'{self.folder_id}' in parents and title='{path.name}' and trashed=false"
            files = self.drive.ListFile({'q': q}).GetList()
            if files:
                f = files[0]
                f.SetContentFile(str(path))
                f.Upload()
            else:
                f = self.drive.CreateFile({'title': path.name, 'parents': [{'id': self.folder_id}]})
                f.SetContentFile(str(path))
                f.Upload()
        except Exception as e:
            logger.warning("Drive upload failed for %s: %s", path.name, e)

    def _sync_from_master_cache(self, symbol: str, tf: str, out_path: Path) -> None:
        master_df = self._download_master_cache()
        if master_df is None or master_df.empty:
            return
        filtered = master_df[
            (master_df["Symbol"] == symbol) &
            (master_df["Timeframe"] == tf)
        ].copy()
        if filtered.empty:
            return
        filtered["Date"] = pd.to_datetime(filtered["Date"])
        filtered.set_index("Date", inplace=True)
        filtered = filtered[["Open", "High", "Low", "Close", "Volume"]].sort_index()
        filtered.to_csv(out_path)

    def _update_master_cache(self, symbol: str, tf: str, df: pd.DataFrame) -> None:
        master_df = self._download_master_cache()
        if master_df is None:
            master_df = pd.DataFrame(columns=["Symbol", "Timeframe", "Date", "Open", "High", "Low", "Close", "Volume"])

        incoming = df.copy().reset_index().rename(columns={"index": "Date"})
        if "Date" not in incoming.columns:
            incoming.rename(columns={incoming.columns[0]: "Date"}, inplace=True)
        incoming["Date"] = pd.to_datetime(incoming["Date"]).dt.strftime("%Y-%m-%d")
        incoming["Symbol"] = symbol
        incoming["Timeframe"] = tf
        incoming = incoming[["Symbol", "Timeframe", "Date", "Open", "High", "Low", "Close", "Volume"]]

        master_df = master_df[~((master_df["Symbol"] == symbol) & (master_df["Timeframe"] == tf))]
        merged = pd.concat([master_df, incoming], ignore_index=True)
        merged.drop_duplicates(subset=["Symbol", "Timeframe", "Date"], keep="last", inplace=True)
        merged.sort_values(by=["Symbol", "Timeframe", "Date"], inplace=True)
        merged.to_csv(self.master_cache_local_path, index=False)
        self._upload_to_drive(self.master_cache_local_path)

    def _download_master_cache(self) -> Optional[pd.DataFrame]:
        self._ensure_master_cache_file()
        if not self.master_cache_local_path.exists():
            return None
        try:
            return pd.read_csv(self.master_cache_local_path)
        except Exception:
            return None

    def _ensure_master_cache_file(self) -> None:
        if self.sync_dir is not None:
            sync_file = self._sync_path(self.master_cache_filename)
            if sync_file is not None and sync_file.exists() and not self.master_cache_local_path.exists():
                import shutil
                shutil.copy2(sync_file, self.master_cache_local_path)
            if not self.master_cache_local_path.exists():
                pd.DataFrame(columns=["Symbol", "Timeframe", "Date", "Open", "High", "Low", "Close", "Volume"]).to_csv(self.master_cache_local_path, index=False)
                if sync_file is not None:
                    pd.read_csv(self.master_cache_local_path).to_csv(sync_file, index=False)
            return

        if self.drive_enabled and self.drive is not None and self.folder_id:
            self._download_from_drive(self.master_cache_filename, self.master_cache_local_path)
            if not self.master_cache_local_path.exists():
                pd.DataFrame(columns=["Symbol", "Timeframe", "Date", "Open", "High", "Low", "Close", "Volume"]).to_csv(self.master_cache_local_path, index=False)
                self._upload_to_drive(self.master_cache_local_path)
