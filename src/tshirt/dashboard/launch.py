"""`tshirt-dash` — launch the Streamlit dashboard.

Wraps `streamlit run` on app.py so there's a clean console command. Streamlit
opens the dashboard in your browser automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli as stcli

APP = Path(__file__).with_name("app.py")


def main() -> int:
    sys.argv = ["streamlit", "run", str(APP)]
    return stcli.main()  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
