"""Makes the ota_sim checkout next to this repo (../ota_sim, or wherever
OTA_SIM_SRC points) importable when ota_sim isn't pip-installed - the same
place fmlink_web and fmlink_ui look for it. Import before `import ota_sim`.
"""
import os
import sys
from pathlib import Path

_SRC = Path(os.environ.get('OTA_SIM_SRC') or Path(__file__).resolve().parents[2] / 'ota_sim')

if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.append(str(_SRC))
