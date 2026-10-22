"""Run the add-on as if under Odysseus (signed identities), with a core that
is never there: nothing in the tests reaches a real Odysseus or Google."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("ODYSSEUS_URL", "http://127.0.0.1:9")      # nothing listens there
os.environ.setdefault("ODYSSEUS_MOD_TOKEN", "odm_test-token")
os.environ.setdefault("ODYSSEUS_MOD_ID", "google")
os.environ.pop("GOOGLE_ADDON_API_BASE", None)
