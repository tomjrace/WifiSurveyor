"""
Where the surveyor is standing, worked out from the access points heard right now.

Only access points take part. The live readings come from a scan, which hears beacons and
probe responses, and only BSSIDs the survey measured at its points are used, so captured
clients and Bluetooth devices can never pull the estimate about.

Two estimates are blended by how well each one fits:

- Fingerprint: what's heard now against what was heard at each survey point. Strong where
  the survey is dense, since it needs no model of walls or antennas.
- Trilateration: each located access point's fitted fall-off, P0 - 10 n log10(d) - walls,
  searched over the plan for where the readings agree best. It reaches the parts of the plan
  between and beyond the survey points.

Bodies, pockets and the way the tablet is held shift every reading by much the same amount, so
a tap where you really are works that shift out (``bias_db``) and moves the marker there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import analysis

USABLE_DBM = -88        # weaker than this is too unsteady to place anyone by
MISSING_DBM = -95       # what "not heard" counts as when comparing fingerprints
MIN_APS = 3             # fewer than this and there's no fix, only a guess
GRID = 80
SMOOTHING = 0.5         # how far each new fix moves the marker towards itself
MAX_FREQS = 14          # channels a live scan visits: enough for the survey's access points
# How much of a shift common to every access point each fix absorbs by itself. The differences
# between access points still place you when a pocket or a body takes 10 dB off them all; the
# rest is left for a tap to calibrate, so absolute levels still count for something.
FREE_SHIFT = 0.8


@dataclass
class Model:
    """What the survey says about the access points, built once and reused for every fix."""
    known: set[str]
    points: list[tuple[float, float, dict[str, float]]]
    freqs: list[int]
    aps: list[str] = field(default_factory=list)          # modelled BSSIDs, in grid order
    grid_x: np.ndarray | None = None
    grid_y: np.ndarray | None = None
    predicted: np.ndarray | None = None                   # (rows, cols, aps) dBm


@dataclass
class Fix:
    x: float
    y: float
    radius: float           # plan pixels: probably within this
    aps: int                # how many surveyed access points the fix used
    method: str             # "fingerprint", "trilateration" or "both"
    rough: bool


def build_model(project) -> Model:
    known: set[str] = set()
    points = []
    counts: dict[float, int] = {}
    for point in project.points:
        heard = {}
        for network in point.networks:
            bssid = network["bssid"].lower()
            known.add(bssid)
            heard[bssid] = float(network["rssi"])
            if network.get("freq_mhz"):
                counts[float(network["freq_mhz"])] = counts.get(float(network["freq_mhz"]), 0) + 1
        if heard:
            points.append((point.x, point.y, heard))
    freqs = sorted(int(f) for f, _ in sorted(counts.items(), key=lambda i: -i[1])[:MAX_FREQS])
    model = Model(known=known, points=points, freqs=freqs)

    # Access points whose position is known well enough to trilaterate from: pinned ones, and
    # estimates that fit. A guess beside its strongest point would only mislead.
    fits = []
    for site in analysis.ap_sites(project, limit=40):
        if site.get("guess") or (site.get("outside") and not site.get("placed")):
            continue
        for fit in site.get("fits", []):
            if fit.get("rms", 99) <= 8 and fit.get("p0") is not None and fit.get("exponent"):
                fits.append((site["x"], site["y"], fit))
    if not fits:
        return model

    rows = max(8, int(GRID * project.height / max(project.width, project.height)))
    cols = max(8, int(GRID * project.width / max(project.width, project.height)))
    gx = np.linspace(0, project.width, cols)
    gy = np.linspace(0, project.height, rows)
    cx, cy = np.meshgrid(gx, gy)
    xs = np.array([f[0] for f in fits])
    ys = np.array([f[1] for f in fits])
    mpp = project.meters_per_pixel or 0.05
    distance = np.hypot(cx[..., None] - xs, cy[..., None] - ys) * mpp
    walls = analysis.grid_wall_loss(project.walls, xs, ys, cx, cy)
    p0 = np.array([f[2]["p0"] for f in fits])
    exponent = np.array([f[2]["exponent"] for f in fits])
    model.aps = [f[2]["bssid"].lower() for f in fits]
    model.grid_x, model.grid_y = gx, gy
    model.predicted = p0 - exponent * 10 * np.log10(np.maximum(distance, 0.5)) - walls
    return model


def readings_from(scan) -> dict[str, float]:
    """A live scan as {bssid: dBm}, from Reading objects or survey-style dicts."""
    heard = {}
    for reading in scan:
        bssid = (reading["bssid"] if isinstance(reading, dict) else reading.bssid).lower()
        rssi = float(reading["rssi"] if isinstance(reading, dict) else reading.rssi)
        heard[bssid] = max(rssi, heard.get(bssid, -200))
    return heard


class Locator:
    def __init__(self) -> None:
        self.bias_db = 0.0
        self.track: tuple[float, float] | None = None
        self.last_heard: dict[str, float] = {}

    def reset(self) -> None:
        self.bias_db = 0.0
        self.track = None
        self.last_heard = {}

    def update(self, model: Model, heard: dict[str, float]) -> Fix | None:
        """A new fix from a scan, smoothed into the track; None when too few APs agree."""
        self.last_heard = heard
        fix = estimate(model, heard, self.bias_db)
        if fix is None:
            return None
        if self.track is None:
            self.track = (fix.x, fix.y)
        else:
            self.track = (self.track[0] + SMOOTHING * (fix.x - self.track[0]),
                          self.track[1] + SMOOTHING * (fix.y - self.track[1]))
        fix.x, fix.y = self.track
        return fix

    def calibrate(self, model: Model, x: float, y: float) -> float | None:
        """
        You're at (x, y). Learns how much stronger or weaker the access points read here than
        the survey expects, from the latest scan, and puts the marker there. Returns the shift
        in dB, or None when the latest scan had too little to learn from.
        """
        self.track = (x, y)
        expected = _expected_at(model, x, y)
        shifts = [rssi - expected[bssid] for bssid, rssi in self.last_heard.items()
                  if bssid in expected and rssi >= USABLE_DBM]
        if len(shifts) < MIN_APS:
            return None
        self.bias_db = float(np.clip(np.median(shifts), -15, 15))
        return self.bias_db


def estimate(model: Model, heard: dict[str, float], bias_db: float = 0.0) -> Fix | None:
    live = {bssid: rssi - bias_db for bssid, rssi in heard.items()
            if bssid in model.known and rssi >= USABLE_DBM}
    if len(live) < MIN_APS:
        return None
    found = [f for f in (_fingerprint(model, live), _trilaterate(model, live)) if f]
    if not found:
        return None
    if len(found) == 1:
        (x, y, radius, rms, method), = found
    else:
        weights = [1 / (f[3] ** 2 + 1) for f in found]
        total = sum(weights)
        x = sum(w * f[0] for w, f in zip(weights, found)) / total
        y = sum(w * f[1] for w, f in zip(weights, found)) / total
        # Two estimates far apart are each less certain than either claims
        apart = float(np.hypot(found[0][0] - found[1][0], found[0][1] - found[1][1]))
        radius = max(sum(w * f[2] for w, f in zip(weights, found)) / total, apart / 2)
        rms = min(f[3] for f in found)
        method = "both"
    return Fix(x=float(x), y=float(y), radius=float(radius), aps=len(live), method=method,
               rough=rms > 7 or len(live) < 4)


def _fingerprint(model: Model, live: dict[str, float]):
    """The survey points whose scans look most like this one, averaged by how alike."""
    scored = []
    for x, y, heard in model.points:
        strong = {b: r for b, r in heard.items() if r >= USABLE_DBM}
        common = set(strong) & set(live)
        if len(common) < MIN_APS:
            continue
        both = set(strong) | set(live)
        shift = FREE_SHIFT * float(np.mean([live[b] - strong[b] for b in common]))
        error = [max(live.get(b, MISSING_DBM) - shift, MISSING_DBM) - max(strong.get(b, MISSING_DBM), MISSING_DBM)
                 for b in both]
        scored.append((float(np.sqrt(np.mean(np.square(error)))), x, y))
    if not scored:
        return None
    scored.sort()
    best = scored[:3]
    weights = np.array([1 / (rms ** 2 + 1) for rms, _, _ in best])
    xs = np.array([x for _, x, _ in best])
    ys = np.array([y for _, _, y in best])
    x, y = float((weights * xs).sum() / weights.sum()), float((weights * ys).sum() / weights.sum())
    radius = float(np.sqrt((weights * ((xs - x) ** 2 + (ys - y) ** 2)).sum() / weights.sum()))
    return x, y, max(radius, 20.0), best[0][0], "fingerprint"


def _trilaterate(model: Model, live: dict[str, float]):
    if model.predicted is None:
        return None
    columns = [i for i, bssid in enumerate(model.aps) if bssid in live]
    if len(columns) < MIN_APS:
        return None
    predicted = model.predicted[..., columns]
    measured = np.array([live[model.aps[i]] for i in columns])
    error = measured - predicted
    error = error - FREE_SHIFT * error.mean(axis=-1, keepdims=True)
    # Access points the model says should be loud here but weren't heard count against it
    silent = [i for i, bssid in enumerate(model.aps) if bssid not in live]
    squares = (error ** 2).sum(axis=-1)
    if silent:
        loud = np.maximum(model.predicted[..., silent] - (USABLE_DBM + 6), 0)
        squares = squares + (loud ** 2).sum(axis=-1)
    rms = np.sqrt(squares / (len(columns) + len(silent) * 0.25))
    row, col = np.unravel_index(np.argmin(rms), rms.shape)
    best = float(rms[row, col])
    close = rms <= best + 3
    cell = (model.grid_x[1] - model.grid_x[0]) * (model.grid_y[1] - model.grid_y[0])
    radius = float(np.sqrt(close.sum() * cell / np.pi))
    return float(model.grid_x[col]), float(model.grid_y[row]), radius, best, "trilateration"


def _expected_at(model: Model, x: float, y: float) -> dict[str, float]:
    """What the survey expects to hear at (x, y): the model where there is one, else the
    nearest survey point's own readings."""
    if model.predicted is not None:
        col = int(np.abs(model.grid_x - x).argmin())
        row = int(np.abs(model.grid_y - y).argmin())
        return {bssid: float(model.predicted[row, col, i]) for i, bssid in enumerate(model.aps)}
    if not model.points:
        return {}
    nearest = min(model.points, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
    return dict(nearest[2])
