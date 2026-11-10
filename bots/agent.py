"""Mine-Void bot entry point. Minescript runs this automatically when the
client joins a world (see `autorun` in client/launch.py). The logic lives
in minevoid/brain.py so it can be hot-reloaded."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minevoid.brain import Agent  # noqa: E402

Agent().run()
