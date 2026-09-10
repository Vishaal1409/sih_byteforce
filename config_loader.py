"""config_loader.py — one way to read /config, for every phase.

CLAUDE.md requires the city-pair basket, weights and thresholds to live in
`/config` rather than scattered through the code. This module is the single
reader for those files, so the simulator, the ETL, the index math, the API and
the dashboard all see the same basket and the same weights.

    from config_loader import load_routes, load_airlines, load_simulator_config

    for route in load_routes():
        print(route.key, route.traffic_weight)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent / "config"

ROUTES_PATH = CONFIG_DIR / "routes.yaml"
AIRLINES_PATH = CONFIG_DIR / "airlines.yaml"
SIMULATOR_PATH = CONFIG_DIR / "simulator.yaml"

#: Weights must sum to 1.0 within this tolerance or the config is rejected.
WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class Route:
    """One city-pair in the fixed APIx basket."""

    origin: str
    dest: str
    name: str
    traffic_weight: float
    distance_km: int
    base_fare_inr: float

    @property
    def key(self) -> str:
        """Canonical route identifier, e.g. 'DEL-BOM'."""
        return f"{self.origin}-{self.dest}"


@dataclass(frozen=True, slots=True)
class Airline:
    """One carrier in the basket."""

    code: str
    name: str
    carrier_type: str
    market_share_weight: float

    @property
    def is_budget(self) -> bool:
        return self.carrier_type == "budget"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config file missing: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def _check_weights(values: list[float], path: Path, field: str) -> None:
    """Fail loudly on a mis-normalised basket.

    A weight set that does not sum to 1.0 silently rescales the whole index, so
    this is checked at load time rather than discovered in a chart.
    """
    total = sum(values)
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise ValueError(
            f"{path.name}: {field} sums to {total!r}, expected 1.0. "
            "Re-normalise the weights."
        )


@lru_cache(maxsize=1)
def load_routes() -> tuple[Route, ...]:
    """The fixed city-pair basket, in config order."""
    data = _read_yaml(ROUTES_PATH)
    routes = tuple(
        Route(
            origin=r["origin"].strip().upper(),
            dest=r["dest"].strip().upper(),
            name=r["name"],
            traffic_weight=float(r["traffic_weight"]),
            distance_km=int(r["distance_km"]),
            base_fare_inr=float(r["base_fare_inr"]),
        )
        for r in data["routes"]
    )
    if not routes:
        raise ValueError(f"{ROUTES_PATH.name}: basket is empty")
    _check_weights([r.traffic_weight for r in routes], ROUTES_PATH, "traffic_weight")
    return routes


@lru_cache(maxsize=1)
def load_airlines() -> tuple[Airline, ...]:
    """The carriers in the basket, in config order."""
    data = _read_yaml(AIRLINES_PATH)
    airlines = tuple(
        Airline(
            code=a["code"].strip().upper(),
            name=a["name"],
            carrier_type=a["carrier_type"],
            market_share_weight=float(a["market_share_weight"]),
        )
        for a in data["airlines"]
    )
    if not airlines:
        raise ValueError(f"{AIRLINES_PATH.name}: no airlines configured")
    for a in airlines:
        if a.carrier_type not in ("budget", "full_service"):
            raise ValueError(
                f"{AIRLINES_PATH.name}: {a.code} has unknown carrier_type "
                f"{a.carrier_type!r}"
            )
    _check_weights(
        [a.market_share_weight for a in airlines], AIRLINES_PATH, "market_share_weight"
    )
    return airlines


@lru_cache(maxsize=1)
def load_simulator_config() -> dict[str, Any]:
    """Raw simulator parameters. See config/simulator.yaml for what each means."""
    return _read_yaml(SIMULATOR_PATH)


def route_by_key(key: str) -> Route:
    """Look up a route by 'DEL-BOM' style key. Raises KeyError if not in basket."""
    key = key.strip().upper()
    for route in load_routes():
        if route.key == key:
            return route
    raise KeyError(f"route {key!r} is not in the basket (config/routes.yaml)")


def airline_by_code(code: str) -> Airline:
    """Look up a carrier by IATA code. Raises KeyError if not in basket."""
    code = code.strip().upper()
    for airline in load_airlines():
        if airline.code == code:
            return airline
    raise KeyError(f"airline {code!r} is not in the basket (config/airlines.yaml)")


def clear_cache() -> None:
    """Drop cached config — for tests that write temporary config files."""
    load_routes.cache_clear()
    load_airlines.cache_clear()
    load_simulator_config.cache_clear()


if __name__ == "__main__":
    routes, airlines = load_routes(), load_airlines()
    print(f"{len(routes)} routes (traffic_weight sums to "
          f"{sum(r.traffic_weight for r in routes):.6f}):")
    for r in routes:
        print(f"  {r.key}  {r.name:<22} w={r.traffic_weight:.3f}  "
              f"{r.distance_km:>5}km  base=Rs.{r.base_fare_inr:,.0f}")
    print(f"\n{len(airlines)} airlines (market_share_weight sums to "
          f"{sum(a.market_share_weight for a in airlines):.6f}):")
    for a in airlines:
        print(f"  {a.code}  {a.name:<12} w={a.market_share_weight:.3f}  {a.carrier_type}")
    sim = load_simulator_config()
    print(f"\nsimulator config v{sim['simulator_version']}, seed={sim['random_seed']}, "
          f"{len(sim['demand_shocks'])} demand shocks")
