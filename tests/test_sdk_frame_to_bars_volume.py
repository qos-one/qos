"""catalog.bars reads a `qos data export` file (it has a volume column) under pandas 3 copy-on-write"""
import pyarrow as pa
import pyarrow.parquet as pq

from qos.sdk import catalog
from qos.sdk.testing import synthetic_bars


def _export_file(tmp_path, with_volume=True):
    df = catalog.bars_to_frame(synthetic_bars("EUR/USD.IDEALPRO", 5, n=200))
    if not with_volume:
        df = df[["open", "high", "low", "close"]]
    t = pa.Table.from_pandas(df)
    t = t.replace_schema_metadata({**(t.schema.metadata or {}), b"qos": b'{"instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5}'})
    path = tmp_path / "eurusd_5m.parquet"
    pq.write_table(t, path)
    return path


def test_export_file_with_volume(tmp_path):
    assert len(catalog.bars("EUR/USD", "5m", source=_export_file(tmp_path))) == 200


def test_export_file_without_volume(tmp_path):
    assert len(catalog.bars("EUR/USD", "5m", source=_export_file(tmp_path, with_volume=False))) == 200
