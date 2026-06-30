"""Entry point for `python -m libwatchduty` — dispatches to the watchduty CLI."""
from .cli import main
import sys

sys.exit(main())
