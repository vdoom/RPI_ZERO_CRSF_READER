import pathlib
import sys

# Make the repo root importable (protocol/, rpi_gateway/, jetson_bridge/)
# regardless of where pytest is invoked from.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
