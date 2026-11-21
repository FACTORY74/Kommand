"""Development launcher for Kommand Central."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
sys.path.insert(0, str(ROOT / "kommand-common" / "src"))

from kommand_central.main import main  # noqa: E402


if __name__ == "__main__":
    main()
