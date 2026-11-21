"""Development launcher for Kommand Source."""

from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parent / "src"
COMMON_ROOT = Path(__file__).resolve().parents[1] / "kommand-common" / "src"
sys.path.insert(0, str(SOURCE_ROOT))
sys.path.insert(0, str(COMMON_ROOT))

from kommand_source.main import main  # noqa: E402


if __name__ == "__main__":
    main()

