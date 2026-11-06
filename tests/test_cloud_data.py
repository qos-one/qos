"""Step 35: QOS Cloud datasets used with catalog.bars(source="cloud:…") (E2E against the real Cloud is in cloud/tests)"""
import hashlib

import pandas as pd
import pytest

from qos.cloud import data as cd
from test_cloud_connector import FakeCloud, signed_in


def _dataset(fake, c, tmp_path, slug="fx-1m", ver="1", sku="data.fx-1m"):
    from qos.sdk import catalog
    src = tmp_path / "src"
    cat = catalog.open_catalog(src)
    idx = pd.date_range("2026-10-01 00:01", periods=600, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": 1.1, "high": 1.1002, "low": 1.0998, "close": 1.1001}, index=idx)
    cat.write_data([catalog.make_fx_instrument("EURUSD")])
    cat.write_data(catalog.frame_to_bars(df, "EUR/USD", "1m"))
    dest = c.dir / "datasets" / slug / ver
    dest.parent.mkdir(parents=True)
    src.rename(dest)
    files = [{"path": p.relative_to(dest).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size}
             for p in sorted(dest.rglob("*")) if p.is_file()]
    tok = fake.sign({"schema": "qos-manifest/1", "name": slug, "version": ver, "kind": "dataset", "files": files}, "qos-man+jwt")
    (dest / "QOS-MANIFEST.jws").write_text(tok)
    fake.grants.append({"sku": sku, "kind": "data", "exp": None})
    c.sync()
    cd._record(c, slug, ver, {"sku": sku, "title": "FX"}, {"files": files}, dest, keep=1)
    return dest


def test_bars_from_cloud_dataset(tmp_path, monkeypatch):
    from qos.cloud import client as cc
    from qos.sdk import catalog
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    monkeypatch.setattr(cc, "_default", c)
    _dataset(fake, c, tmp_path)
    one = catalog.bars("EUR/USD", "1m", source="cloud:fx-1m")
    five = catalog.bars("EUR/USD", "5m", source="cloud:fx-1m")
    assert len(one) == 600 and len(five) == 120
    assert cd.check("fx-1m")["ok"]

    fake.grants = [g for g in fake.grants if g["sku"] != "data.fx-1m"]
    c.sync()
    with pytest.raises(cd.DataAccessError, match="needs data.fx-1m"):
        catalog.bars("EUR/USD", "1m", source="cloud:fx-1m")
    fake.role = "admin"
    c.sync()
    assert len(catalog.bars("EUR/USD", "1m", source="cloud:fx-1m")) == 600
    fake.role = "trader"
    fake.grants.append({"sku": "data.fx-1m", "kind": "data", "exp": None})
    fake.items = [{"seq": 1, "kind": "dataset", "ref": "fx-1m@1", "reason": "bad ticks"}]
    c.sync()
    with pytest.raises(cd.DataAccessError, match="withdrawn"):
        catalog.bars("EUR/USD", "1m", source="cloud:fx-1m")
    with pytest.raises(cd.DataAccessError, match="qos data pull other"):
        catalog.bars("EUR/USD", "1m", source="cloud:other")


def test_tampered_manifest_rejected(tmp_path, monkeypatch):
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    dest = _dataset(fake, c, tmp_path)
    other = FakeCloud()
    (dest / "QOS-MANIFEST.jws").write_text(other.sign({"schema": "qos-manifest/1", "name": "fx-1m", "version": "1",
                                                       "kind": "dataset", "files": []}, "qos-man+jwt"))
    with pytest.raises(cd.DataAccessError, match="signature"):
        cd.resolve("fx-1m", c)


def test_pack_selects_bars_and_instruments(tmp_path):
    import tarfile
    cat = tmp_path / "cat"
    for n in ("EURUSD.IDEALPRO-1-MINUTE-MID-EXTERNAL", "EURUSD.IDEALPRO-5-MINUTE-MID-EXTERNAL", "USDJPY.IDEALPRO-1-MINUTE-MID-EXTERNAL"):
        (cat / "data" / "bar" / n).mkdir(parents=True)
        (cat / "data" / "bar" / n / "a.parquet").write_bytes(b"x")
    for i in ("EURUSD.IDEALPRO", "USDJPY.IDEALPRO"):
        (cat / "data" / "currency_pair" / i).mkdir(parents=True)
        (cat / "data" / "currency_pair" / i / "p.parquet").write_bytes(b"y")
    (cat / "_manifest.json").write_text("{}")
    info = cd.pack(cat, tmp_path / "o.tar.gz", ["EURUSD*1-MINUTE*"])
    names = tarfile.open(tmp_path / "o.tar.gz").getnames()
    assert info["instruments"] == ["EURUSD.IDEALPRO"] and len(names) == 2
    assert "data/currency_pair/EURUSD.IDEALPRO/p.parquet" in names and not any("_manifest" in n for n in names)
    with pytest.raises(ValueError):
        cd.pack(cat, tmp_path / "x.tar.gz", ["NOPE*"])


def test_cli_data_list_offline(tmp_path, monkeypatch, capsys):
    from qos import cli
    from qos.cloud import client as cc
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    _dataset(fake, c, tmp_path)
    fake.revoked = True
    monkeypatch.setattr(cc, "_default", c)
    assert cli.main(["data", "list"]) == 0
    out = capsys.readouterr().out
    assert "fx-1m" in out and "have 1" in out
