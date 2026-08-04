#!/usr/bin/env python3
"""Generate deterministic 360-row classification and regression example datasets."""

from __future__ import annotations

import csv
import hashlib
import math
import random
from pathlib import Path


_EXAMPLES_ROOT = Path(__file__).resolve().parents[1]
_DATA_ROOT = _EXAMPLES_ROOT / "data"
_ROW_COUNT = 360
_SEED = 20260801


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _classification_rows(rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    plans = ("basic", "standard", "premium")
    regions = ("north", "east", "south", "west")
    for index in range(_ROW_COUNT):
        age = rng.randint(20, 72)
        tenure_months = rng.randint(1, 84)
        plan_type = rng.choices(plans, weights=(0.36, 0.44, 0.20), k=1)[0]
        region = rng.choice(regions)
        auto_pay = int(rng.random() < 0.62)
        support_tickets = min(8, int(rng.expovariate(0.75)))
        last_login_days = min(60, int(rng.expovariate(0.11)))
        base_spend = {"basic": 48.0, "standard": 86.0, "premium": 142.0}[plan_type]
        monthly_spend = max(18.0, base_spend + rng.gauss(0.0, 15.0))

        score = (
            -0.92
            + 0.095 * (last_login_days - 8)
            + 0.48 * support_tickets
            - 0.026 * tenure_months
            + 0.42 * (plan_type == "basic")
            - 0.55 * (plan_type == "premium")
            - 0.62 * auto_pay
            + 0.017 * max(monthly_spend - 100.0, 0.0)
            + 0.24 * (region == "west")
            + rng.gauss(0.0, 0.28)
        )
        probability = 1.0 / (1.0 + math.exp(-score))
        churned = int(rng.random() < probability)

        rows.append(
            {
                "age": age,
                "monthly_spend": "" if index % 47 == 0 else f"{monthly_spend:.2f}",
                "tenure_months": tenure_months,
                "support_tickets": support_tickets,
                "plan_type": "" if index % 71 == 0 else plan_type,
                "region": region,
                "auto_pay": auto_pay,
                "last_login_days": "" if index % 53 == 0 else last_login_days,
                "churned": churned,
            }
        )
    return rows


def _regression_rows(rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    districts = ("central", "riverside", "university", "suburban")
    district_adjustment = {
        "central": 1250.0,
        "riverside": 720.0,
        "university": 430.0,
        "suburban": -180.0,
    }
    for index in range(_ROW_COUNT):
        floor_area_sqm = rng.uniform(32.0, 168.0)
        bedrooms = max(1, min(5, round(floor_area_sqm / 34.0 + rng.uniform(-0.45, 0.45))))
        building_age = rng.randint(0, 42)
        distance_to_center_km = rng.uniform(0.4, 24.0)
        transit_score = min(100.0, max(20.0, 96.0 - 2.7 * distance_to_center_km + rng.gauss(0, 8)))
        district = rng.choice(districts)
        has_elevator = int(building_age < 18 or rng.random() < 0.52)
        renovation_quality = rng.randint(1, 5)

        monthly_rent = (
            620.0
            + 36.5 * floor_area_sqm
            + 115.0 * bedrooms
            - 72.0 * distance_to_center_km
            + 11.5 * transit_score
            - 9.5 * building_age
            + district_adjustment[district]
            + 165.0 * has_elevator
            + 285.0 * renovation_quality
            + rng.gauss(0.0, 260.0)
        )

        rows.append(
            {
                "floor_area_sqm": f"{floor_area_sqm:.2f}",
                "bedrooms": bedrooms,
                "building_age": building_age,
                "distance_to_center_km": f"{distance_to_center_km:.2f}",
                "transit_score": "" if index % 67 == 0 else f"{transit_score:.2f}",
                "district": "" if index % 89 == 0 else district,
                "has_elevator": has_elevator,
                "renovation_quality": renovation_quality,
                "monthly_rent": f"{max(monthly_rent, 800.0):.2f}",
            }
        )
    return rows


def main() -> int:
    classification_path = _DATA_ROOT / "classification_360.csv"
    regression_path = _DATA_ROOT / "regression_360.csv"
    classification_rows = _classification_rows(random.Random(_SEED))
    regression_rows = _regression_rows(random.Random(_SEED + 1))
    _write_csv(classification_path, list(classification_rows[0]), classification_rows)
    _write_csv(regression_path, list(regression_rows[0]), regression_rows)

    for path in (classification_path, regression_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{path}: rows={_ROW_COUNT} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
