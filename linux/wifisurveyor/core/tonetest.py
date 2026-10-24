"""
The wall test, step by step: how to set the tone up with the devices at hand, and what to
measure next. Plain words for whoever is holding the tablet; the maths is analysis.fit_walls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import analysis

MIN_POINTS = 5               # fit_walls needs this many points that heard the tone
PATHS_PER_WALL = 2           # a wall crossed by fewer paths is only roughly measured


def command(freq_mhz: float) -> str:
    """The tone on any computer with the hackrf package: a plain carrier at a low level."""
    return f"hackrf_transfer -c 127 -f {int(freq_mhz * 1e6)} -s 2000000 -x 10 -a 0"


@dataclass
class Setup:
    title: str
    steps: list[str] = field(default_factory=list)
    start_here: bool = False      # this tablet's HackRF can send the tone (on a long cable)
    command: str = ""
    ready: bool = True            # something here can hear the tone


def setup(freq_mhz: float, hackrf_here: str | None, analyzer_here: str | None,
          second_hackrf: str | None = None) -> Setup:
    """
    How to run the test with what's connected: ``hackrf_here`` and ``analyzer_here`` name the
    HackRF and the RF Explorer/tinySA plugged into this tablet, or are None, and
    ``second_hackrf`` names another HackRF here. A HackRF can't send and listen at once, and
    whatever sends has to stay at the tone spot while this tablet walks, so the sender is
    nearly always a second machine, or a second HackRF on a long cable.
    """
    run = command(freq_mhz)
    elsewhere = (f"On a second computer with the hackrf package (a laptop, or a Raspberry Pi on "
                 f"a power bank), plug in a HackRF and run:\n{run}\nLeave it running; Ctrl-C "
                 "stops it. If that computer has Wi-Fi Surveyor, Setup → HackRF → Test tone → "
                 "Start does the same and stops by itself after 30 minutes.")
    portapack = (f"No second computer? A HackRF with a PortaPack (Mayhem firmware) sends it on "
                 f"its own: Transmit → Signal Gen, CW, {freq_mhz:.0f} MHz, lowest gain.")
    placing = ("Stand the sender in the spot you tapped, about chest high, antenna upright, "
               "away from metal.")
    walking = ("Then walk: tap where you're standing and choose Scanning point. Measure on both "
               f"sides of each wall you want measured, at least {PATHS_PER_WALL} spots behind "
               f"each, and {MIN_POINTS} or more in all.")

    if hackrf_here and second_hackrf and not analyzer_here:
        return Setup(
            f"Your {second_hackrf} sends the tone; the {hackrf_here} listens",
            [f"With two HackRFs here, one sends and the other listens. The {second_hackrf} has "
             "to stay at the tone spot while you walk with this tablet and the "
             f"{hackrf_here}: reach it with an active USB extension cable, or move it to a "
             "second computer.",
             elsewhere,
             f"With an extension cable: in Setup → HackRF choose the {second_hackrf} and press "
             "Start under Test tone. It stops by itself after 30 minutes.",
             placing, walking],
            start_here=True, command=run)
    if hackrf_here and analyzer_here:
        return Setup(
            f"Your {hackrf_here} sends the tone; the {analyzer_here} listens",
            [f"The {hackrf_here} has to stay at the tone spot while you walk with this tablet "
             f"and the {analyzer_here}. Either move it to a second computer, or reach it with "
             "an active USB extension cable.",
             elsewhere,
             "With an extension cable: press Start the tone here. It stops by itself after "
             "30 minutes.",
             placing, walking],
            start_here=True, command=run)
    if hackrf_here:
        return Setup(
            f"Your {hackrf_here} listens; the tone has to come from somewhere else",
            [f"A HackRF can't send and listen at once, so keep the {hackrf_here} plugged in "
             "here to hear the tone. Something else has to send it:",
             f"A second HackRF. {elsewhere}",
             portapack,
             f"Or plug a tinySA or RF Explorer in here to listen, and send the tone with the "
             f"{hackrf_here} from a second computer instead.",
             placing, walking],
            command=run)
    if analyzer_here:
        return Setup(
            f"The {analyzer_here} listens; a HackRF sends the tone",
            [f"This tablet hears the tone with the {analyzer_here}. A HackRF at the tone spot "
             "sends it.", elsewhere, portapack, placing, walking],
            command=run)
    return Setup(
        "Connect something to hear the tone first",
        ["Nothing on this tablet can hear the tone yet. Plug in a tinySA, RF Explorer or "
         "HackRF here, and it's found by itself.",
         f"A HackRF at the tone spot sends it. {elsewhere}",
         portapack, placing, walking],
        command=run, ready=False)


@dataclass
class Step:
    number: int                   # 1 place, 2 start and scan, 3 measure more, 4 fit
    text: str
    action: str | None = None     # "setup" (show how), "fit", or None


def wall_paths(project) -> dict[int, int]:
    """For each wall, how many points that heard the tone lie behind it, seen from the tone."""
    source = project.tone_source
    counts: dict[int, int] = {}
    if not source:
        return counts
    for x, y, _level in analysis.tone_samples(project):
        for index, wall in enumerate(project.walls):
            if analysis._crosses(source["x"], source["y"], x, y, wall.x1, wall.y1, wall.x2, wall.y2):
                counts[index] = counts.get(index, 0) + 1
    return counts


def next_step(project, can_hear: bool) -> Step:
    source = project.tone_source
    if not source:
        return Step(1, "Tap the plan where the tone generator will sit, and choose Tone generator.")
    if not can_hear:
        return Step(2, "Connect a tinySA, RF Explorer or HackRF to this tablet to hear the tone.",
                    "setup")
    tried = [p for p in project.points if p.tone and abs(p.tone.get("freq_mhz", 0) - source["freq_mhz"]) < 1]
    heard = analysis.tone_samples(project)
    if not tried:
        return Step(2, f"Start the tone at {source['freq_mhz']:.0f} MHz, then tap where you're "
                       "standing and choose Scanning point.", "setup")
    if not heard:
        return Step(2, "The tone hasn't been heard yet. Check it's running and set up as shown, "
                       "or measure closer to it first.", "setup")
    if not project.walls:
        return Step(3, f"Heard at {len(heard)} point{'s' if len(heard) != 1 else ''}. Draw the walls "
                       "between the tone and those points (Draw wall), so each can be measured.")
    paths = wall_paths(project)
    measured = [i for i, count in paths.items() if count >= PATHS_PER_WALL]
    thin = [i for i, count in paths.items() if count < PATHS_PER_WALL]
    if len(heard) >= MIN_POINTS and measured:
        extra = (f" {len(thin)} more wall{'s have' if len(thin) != 1 else ' has'} only one point "
                 "behind; measure behind them too, or fit now.") if thin else ""
        return Step(4, f"Ready: {len(measured)} wall{'s' if len(measured) != 1 else ''} measured from "
                       f"{len(heard)} points. Fit the walls.{extra}", "fit")
    wanted = max(0, MIN_POINTS - len(heard))
    parts = [f"Heard at {len(heard)} point{'s' if len(heard) != 1 else ''}."]
    if wanted:
        parts.append(f"{wanted} more needed.")
    if not measured:
        parts.append(f"Measure at least {PATHS_PER_WALL} spots behind each wall you want "
                     "measured, on the far side from the tone.")
    return Step(3, " ".join(parts))
