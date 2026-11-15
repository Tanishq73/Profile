#!/data/data/com.termux/files/usr/bin/bash
# preopen-brief on Android (Termux). Run from the project folder:  bash scheduling/termux-setup.sh
#
# NOT verified on a device by the author. Known rough edges:
#  * numpy/pandas have no PyPI wheels for Android: they are installed from Termux packages.
#  * yfinance depends on curl_cffi, which may fail to build on Termux. If it does, the
#    brief still runs: the two US lines show "n/a (yahoo: yfinance: ModuleNotFoundError)".
#  * Android may still delay cron in Doze: also exempt Termux from battery optimisation
#    (Settings > Apps > Termux > Battery > Unrestricted).
set -euo pipefail

pkg update -y
pkg install -y python cronie termux-services tur-repo
pkg install -y python-numpy python-pandas          # prebuilt; python-pandas comes from tur-repo

# Use the system pandas/numpy, so install the remaining deps without a venv.
pip install httpx==0.28.1 PyYAML==6.0.3 python-dotenv==1.2.4 tzdata
pip install yfinance==1.8.0 || echo "!! yfinance failed to install - US lines will be n/a (see notes above)"

mkdir -p data logs
[ -f .env ] || cp .env.example .env

# Keep the CPU awake so cron fires with the screen off, and start crond now and on boot.
termux-wake-lock
sv-enable crond || true
sv up crond || true

# Install the schedule (IST via CRON_TZ; cronie honours it). Uses system python, not .venv.
sed -e "s|^PREOPEN=.*|PREOPEN=$PWD|" -e "s|\.venv/bin/python|python|g" scheduling/crontab.txt | crontab -
crontab -l

echo
echo "Done. Edit .env, then test:  python -m preopen_brief.jobs.morning --dry-run --force"
echo "After a reboot: open Termux once and run 'termux-wake-lock' (or install Termux:Boot to automate it)."
