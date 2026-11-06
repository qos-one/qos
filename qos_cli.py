"""qos CLI on a machine with QOS installed (scripts/qos) — the code lives in qos/cli.py (the same one installed by pip install qos-sdk)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qos.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
