"""Export the FastAPI OpenAPI spec to contracts/openapi/openapi.v1.json.

Run from anywhere: ``python backend/scripts/export_openapi.py``.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts" / "openapi" / "openapi.v1.json"


def main() -> None:
    from backend.app.main import app

    spec = app.openapi()
    spec["info"]["version"] = "v1"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(spec, indent=2, default=str), encoding="utf-8"
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
