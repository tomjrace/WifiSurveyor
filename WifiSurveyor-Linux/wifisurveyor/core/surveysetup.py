"""What a survey measures at each point, chosen once for the kind of survey it is.

A quick coverage walk wants a scan and a short sweep; a production or industrial site wants
the other bands too; a security sweep wants Bluetooth and finder tags. Each type is a
starting point: the setup dialog ticks its boxes, and changing any makes it Custom. The setup
is kept with the survey (Project.survey_setup), so reopening it, or a follow-up of the same
site, measures the same way. Its spectrum ranges are the ones ticked on the Spectrum page
(core.rangechoice): changing them in either place changes them in both.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

from ..rf import bands as rfbands

WIFI_RANGE_NAMES = ("2.4 GHz", "5 GHz (UNII-1/2A)", "5 GHz (UNII-2C/3/4)", "6 GHz")
# Listening for hidden networks' names: seconds on each channel, and at most this many
# channels a point, strongest unnamed network first
HIDDEN_SECONDS = 4
HIDDEN_CHANNELS = 3


@dataclass
class Setup:
    kind: str = "standard"
    wifi_ranges: list[int] = field(default_factory=lambda: [0, 1, 2, 3])
    sweeps: int = 8
    dwell_count: int = 3
    dwell_seconds: int = 4
    check_link: bool = True
    join: bool = False
    speed: bool = False
    bluetooth: bool = False
    tags: bool = False
    identify: bool = True
    cellular: bool = False
    # Monitor mode on the hidden networks' channels at each point, to hear their names
    # (core.hidden). Needs root and a monitor-capable adapter besides the scanning one.
    hidden_names: bool = False
    band_groups: list[str] = field(default_factory=list)
    bands: list[str] = field(default_factory=list)
    # Ranges ticked on the Spectrum page that are neither Wi-Fi nor a band of rf.bands (the
    # wide overviews, cellular downlinks): [name, low MHz, high MHz]
    extra_ranges: list[list] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "Setup":
        known = {f.name for f in fields(cls)}
        payload = dict(payload or {})
        if "hidden_names" not in payload and payload.get("kind") in TYPE_BY_KEY:
            # Saved before the choice existed: as its type does now, so it stays that type
            payload["hidden_names"] = TYPE_BY_KEY[payload["kind"]][2].hidden_names
        setup = cls(**{k: v for k, v in payload.items() if k in known})
        setup.wifi_ranges = [i for i in setup.wifi_ranges if 0 <= int(i) < len(WIFI_RANGE_NAMES)]
        setup.bands = [k for k in setup.bands if k in rfbands.BY_KEY]
        setup.extra_ranges = [[str(e[0]), float(e[1]), float(e[2])] for e in setup.extra_ranges
                              if len(e) == 3]
        return setup

    def same_choices(self, other: "Setup") -> bool:
        mine, theirs = self.to_dict(), other.to_dict()
        mine.pop("kind")
        theirs.pop("kind")
        return ({k: sorted(v) if isinstance(v, list) else v for k, v in mine.items()}
                == {k: sorted(v) if isinstance(v, list) else v for k, v in theirs.items()})


def _with_groups(groups: list[str], **values) -> Setup:
    return Setup(band_groups=list(groups), bands=[b.key for b in rfbands.for_groups(groups)], **values)


# (key, name, what it's for, the setup)
TYPES = (
    ("quick", "Quick Wi-Fi coverage",
     "Signal and noise at each point, fast: for walking a large site, or a first look.",
     Setup("quick", sweeps=3, dwell_count=0, identify=False)),
    ("standard", "Standard Wi-Fi survey",
     "Coverage, noise, channel use and what's on the busy channels. The usual survey.",
     Setup("standard")),
    ("thorough", "Thorough Wi-Fi: interference and capacity",
     "Everything Wi-Fi: longer sweeps and dwells, throughput, Bluetooth and cellular. For "
     "diagnosing a troubled network. Joins the network chosen on the map to test it.",
     Setup("thorough", sweeps=12, dwell_count=5, dwell_seconds=6, join=True, speed=True,
           bluetooth=True, cellular=True, hidden_names=True)),
    ("event", "Live event or production",
     "Wi-Fi plus wireless mics and IEMs, intercom, DMX, video links and the venue's services, "
     "to plan frequencies and find trouble before the show.",
     _with_groups(["mics", "production", "venue"], kind="event", sweeps=6, dwell_count=2,
                  bluetooth=True)),
    ("industrial", "Warehouse or industrial",
     "Wi-Fi plus sub-GHz industrial and IoT (900 MHz, 433 MHz, SCADA), business radio and "
     "cellular, where scanners, sensors and radios share the air.",
     _with_groups(["subghz", "business"], kind="industrial", cellular=True)),
    ("office", "Office or business",
     "Wi-Fi with connection and throughput, Bluetooth, cellular, business radio and the "
     "building's public services.",
     _with_groups(["business", "venue"], kind="office", join=True, speed=True, bluetooth=True,
                  cellular=True)),
    ("security", "Security sweep",
     "Unknown transmitters: finder tags (AirTags and the like), Bluetooth, what's on the busy "
     "channels, and the sub-GHz, business and venue bands.",
     _with_groups(["subghz", "business", "venue"], kind="security", bluetooth=True, tags=True,
                  cellular=True, hidden_names=True)),
    ("custom", "Custom", "Your own choice of everything below.", Setup("custom")),
)
TYPE_BY_KEY = {key: (name, about, setup) for key, name, about, setup in TYPES}


def preset(kind: str) -> Setup:
    """A fresh copy of a type's setup."""
    return Setup.from_dict(TYPE_BY_KEY.get(kind, TYPE_BY_KEY["standard"])[2].to_dict())


def matching_kind(setup: Setup) -> str:
    """The type these choices are, or "custom" when they're no type's."""
    for key, _name, _about, choices in TYPES:
        if key != "custom" and choices.same_choices(setup):
            return key
    return "custom"


def seconds_per_point(setup: Setup, analyzers: int = 1, hackrf: bool = False) -> float:
    """A rough time a point takes, so the thoroughness can be weighed against the site's size."""
    total = 7.0                                            # the Wi-Fi scans
    if analyzers and setup.wifi_ranges:
        total += len(setup.wifi_ranges) * setup.sweeps * 0.35 / max(1, analyzers)
        total += setup.dwell_count * setup.dwell_seconds / max(1, analyzers)
    if setup.check_link:
        total += 3
    if setup.join:
        total += 4
    if setup.speed:
        total += 10
    if setup.bluetooth:
        total += 5
    if setup.tags:
        total += 8
    if setup.identify and hackrf:
        total += 2 * 3
    if setup.cellular and hackrf:
        total += 3
    if setup.hidden_names:
        total += HIDDEN_SECONDS * 2           # only where hidden networks are still unnamed
    others = len(setup.bands) + len(setup.extra_ranges)
    if others and analyzers:
        total += others * 1.4 / max(1, analyzers)
    return total


def describe(setup: Setup) -> str:
    """One line for the survey page: the type and what it measures."""
    name = TYPE_BY_KEY.get(setup.kind, TYPE_BY_KEY["custom"])[0]
    wifi = ("all Wi-Fi bands" if len(setup.wifi_ranges) == len(WIFI_RANGE_NAMES) else
            "no Wi-Fi spectrum" if not setup.wifi_ranges else
            ", ".join(WIFI_RANGE_NAMES[i] for i in sorted(setup.wifi_ranges)))
    extras = [label for label, on in (("connection", setup.check_link), ("throughput", setup.speed),
                                      ("Bluetooth", setup.bluetooth), ("finder tags", setup.tags),
                                      ("channel identification", setup.identify),
                                      ("cellular", setup.cellular),
                                      ("hidden network names", setup.hidden_names)) if on]
    others = len(setup.bands) + len(setup.extra_ranges)
    if others:
        extras.append(f"{others} other range{'s' if others != 1 else ''}")
    return f"{name}: {wifi}" + (", " + ", ".join(extras) if extras else "") + "."
