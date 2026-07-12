"""Download the four static registry CSVs into the data directory.

Runnable standalone (``python -m app.registry.download``) and used by the
startup bootstrap when ``WL_REGISTRY_AUTO_DOWNLOAD`` is enabled. These are
static files updated a few times a year, so a one-off download is well within
fair use.

Dataset: https://www.data.gv.at/datasets/cfba4373-a654-3e0b-80f8-348738169f95
Licence: CC BY 4.0 — Datenquelle: Stadt Wien - data.wien.gv.at
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.core.config import Settings
from app.core.http import USER_AGENT

logger = logging.getLogger(__name__)

CSV_FILENAMES = (
    "wienerlinien-ogd-haltestellen.csv",
    "wienerlinien-ogd-haltepunkte.csv",
    "wienerlinien-ogd-linien.csv",
    "wienerlinien-ogd-fahrwegverlaeufe.csv",
)
_BASE_URL = "https://www.wienerlinien.at/ogd_realtime/doku/ogd"


def missing_csvs(data_dir: Path) -> list[str]:
    return [name for name in CSV_FILENAMES if not (data_dir / name).is_file()]


def _write_atomic(target: Path, content: bytes) -> None:
    # Write-then-rename so a crashed download never leaves a truncated CSV
    # that the loader would happily half-parse on the next start.
    tmp_path = target.with_suffix(target.suffix + ".part")
    tmp_path.write_bytes(content)
    tmp_path.replace(target)


async def download_registry_csvs(data_dir: Path, *, force: bool = False) -> list[str]:
    """Fetch missing CSVs into ``data_dir``; returns the filenames downloaded."""
    await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
    targets = CSV_FILENAMES if force else missing_csvs(data_dir)
    if not targets:
        return []

    downloaded: list[str] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
    ) as client:
        for name in targets:
            url = f"{_BASE_URL}/{name}"
            logger.info("downloading registry CSV", extra={"url": url})
            response = await client.get(url)
            response.raise_for_status()
            await asyncio.to_thread(_write_atomic, data_dir / name, response.content)
            downloaded.append(name)
    return downloaded


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings()
    downloaded = asyncio.run(download_registry_csvs(Path(settings.data_dir), force=True))
    logger.info("downloaded %d file(s) into %s", len(downloaded), settings.data_dir)


if __name__ == "__main__":
    main()
