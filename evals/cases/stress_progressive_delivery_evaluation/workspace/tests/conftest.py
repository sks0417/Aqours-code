from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def base_flags():
    return {
        "new-checkout": {
            "enabled": True,
            "off_variation": "off",
            "default_variation": "off",
            "salt": "checkout-v2",
            "variations": {"off": False, "on": True, "preview": "preview"},
            "targets": {"users": {"alice": "on"}, "tenants": {"staff": "preview"}},
            "rules": [{
                "id": "modern-pro", "priority": 10,
                "conditions": [
                    {"attribute": "plan", "operator": "eq", "value": "pro"},
                    {"attribute": "app_version", "operator": "semver_gte", "value": "2.0.0"},
                ],
                "variation": "on",
            }],
        }
    }
