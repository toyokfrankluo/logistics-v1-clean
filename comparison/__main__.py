"""Print synthetic comparison JSON: python -B -m comparison (no file writes)."""
import json
from .fixtures import corpus
from .runner import compare

print(json.dumps([compare(case) for case in corpus()], ensure_ascii=False, indent=2, default=str))
