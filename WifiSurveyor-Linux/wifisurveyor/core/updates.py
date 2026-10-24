"""Checking for, downloading and installing software and firmware for the radios.

  HackRF       the hackrf tools and firmware from Ubuntu (apt), or the newest release from
               Great Scott Gadgets on GitHub. Firmware is flashed with hackrf_spiflash, which
               refuses a file made for another board, then the HackRF is reset.
  RTL-SDR      has no firmware of its own; what updates is the rtl-sdr driver and tools: from
               Ubuntu, or built from the newest osmocom release (the RTL-SDR Blog fork for a
               Blog V4) into /usr/local.
  tinySA       firmware from the tinySA author's site (athome.kaashoek.com, "tinySA4" for the
               Ultra family), flashed with dfu-util once the tinySA is in DFU mode, which only
               its buttons can do.
  RF Explorer  firmware only through RF Explorer's own Uploader, which looks up the latest
               itself: this fetches the Linux Uploader, installs Mono for it, and starts it.

Nothing is changed without being asked: a check only reads, and each change is its own action
the person starts (and confirms, for firmware). Installing uses sudo when it needs no password,
otherwise polkit asks for one.
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..net import outbound
from .project import data_dir, hand_back, invoking_user, make_dir

GITHUB = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_TAGS = "https://api.github.com/repos/{repo}/tags"
HACKRF_REPO = "greatscottgadgets/hackrf"
RTLSDR_REPO, RTLSDR_BLOG_REPO = "osmocom/rtl-sdr", "rtlsdrblog/rtl-sdr-blog"
TINYSA_DFU = {"ultra": "http://athome.kaashoek.com/tinySA4/DFU/",
              "original": "http://athome.kaashoek.com/tinySA/DFU/"}
RFE_UPLOADER = "https://version.rf-explorer.com/sw/fw/RFExplorer_UploaderLinux.zip"
HACKRF_FIRMWARE = {"HackRF One": "hackrf_one_usb.bin", "HackRF Jawbreaker": "hackrf_jawbreaker_usb.bin",
                   "rad1o": "hackrf_rad1o_usb.bin"}
FIRMWARE_DIR = Path("/usr/share/hackrf")
DFU_ID = ("0483", "df11")          # an STM32 in DFU mode: how a tinySA shows up to be flashed

Say = Callable[[str], None]


@dataclass
class Action:
    key: str
    label: str                    # the button
    about: str                    # what it will do, in a sentence
    run: Callable[[Say], str]     # does it; returns what happened
    confirm: str = ""             # asked first when not empty (anything that flashes firmware)


@dataclass
class Status:
    device: str
    lines: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    current: bool = False         # nothing newer found

    @property
    def text(self) -> str:
        return " ".join(self.lines)


# ---------- small helpers ----------

def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def newer(candidate: str | None, installed: str | None) -> bool:
    if not candidate:
        return False
    if not installed:
        return True
    return version_key(candidate) > version_key(installed)


def _upstream(version: str) -> str:
    """A Debian version's upstream part: "2.0.2-2build1" -> "2.0.2", "1:2.0" -> "2.0"."""
    return version.split(":", 1)[-1].split("-", 1)[0].split("+", 1)[0]


def root_command(command: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return command
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return ["sudo", "-n", *command]
    return ["pkexec", *command]


def run_logged(command: list[str], say: Say, timeout: float = 1800,
               env: dict | None = None) -> tuple[int, str]:
    """Runs a command, passing each line it prints to ``say``. Returns (exit code, the last lines)."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1, env=env)
    tail: list[str] = []
    began = time.monotonic()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-12:]
            say(line)
        if time.monotonic() - began > timeout:
            proc.kill()
            break
    return proc.wait(), "\n".join(tail)


def apt_versions(package: str) -> tuple[str | None, str | None]:
    """(installed, candidate) from apt's package lists; None for what isn't there."""
    try:
        text = subprocess.run(["apt-cache", "policy", package], capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, None

    def field_(name: str) -> str | None:
        match = re.search(rf"{name}:\s*(\S+)", text)
        value = match.group(1) if match else None
        return None if value in (None, "(none)") else value

    return field_("Installed"), field_("Candidate")


def refresh_apt(say: Say) -> bool:
    """apt-get update, when that needs no password (root, or sudo without one)."""
    if os.geteuid() != 0 and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        return False
    say("Refreshing the package lists…")
    code, _tail = run_logged(root_command(["apt-get", "update", "-qq"]), lambda _l: None, 180)
    return code == 0


def apt_install(packages: list[str], say: Say) -> str:
    say(f"Installing {', '.join(packages)}…")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    command = root_command(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                            *packages])
    code, tail = run_logged(command, say, env=env)
    if code != 0:
        raise RuntimeError(f"apt-get failed ({code}): {tail.splitlines()[-1] if tail else ''}")
    return f"Installed {', '.join(packages)}."


def fetch_json(url: str, timeout: float = 15) -> dict | list:
    return json.loads(outbound.fetch(url, headers={"Accept": "application/vnd.github+json"},
                                     timeout=timeout))


def github_latest(repo: str) -> str | None:
    """The newest release's tag, or the newest tag when there are no releases."""
    try:
        return str(fetch_json(GITHUB.format(repo=repo)).get("tag_name") or "") or None
    except Exception:
        pass
    try:
        tags = fetch_json(GITHUB_TAGS.format(repo=repo))
        return str(tags[0]["name"]) if tags else None
    except Exception:
        return None


def updates_dir() -> Path:
    return make_dir(data_dir() / "updates")


def download(url: str, target: Path, say: Say, limit_mb: float = 200) -> Path:
    """Fetches a file with progress, refusing anything over ``limit_mb``."""
    partial = target.with_suffix(target.suffix + ".part")
    with outbound.open_url(url, timeout=30) as answer, open(partial, "wb") as out:
        size = int(answer.headers.get("Content-Length") or 0)
        got, said = 0, 0.0
        while True:
            chunk = answer.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > limit_mb * 1e6:
                raise RuntimeError(f"{url} is larger than {limit_mb:.0f} MB; not downloading it.")
            out.write(chunk)
            if time.monotonic() - said > 1:
                said = time.monotonic()
                say(f"Downloading {target.name}: {got / 1e6:.1f}" + (f" of {size / 1e6:.1f}" if size else "")
                    + " MB")
    partial.replace(target)
    hand_back(target)
    return target


def usb_present(ids: tuple[str, str], root: Path = Path("/sys/bus/usb/devices")) -> bool:
    for entry in root.glob("*"):
        try:
            if ((entry / "idVendor").read_text().strip().lower(),
                    (entry / "idProduct").read_text().strip().lower()) == ids:
                return True
        except OSError:
            continue
    return False


# ---------- HackRF ----------

def firmware_file_version(path: Path) -> str | None:
    """The release a HackRF firmware image was built from, from the version string inside it."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    match = re.search(rb"(20\d\d\.\d\d\.\d+)", data)
    return match.group(1).decode() if match else None


def check_hackrf(device, say: Say = lambda _t: None) -> Status:
    status = Status(device.model if device is not None else "HackRF")
    refresh_apt(say)
    installed, candidate = apt_versions("hackrf")
    status.lines.append(f"hackrf tools {_upstream(installed) if installed else 'not installed'}"
                        + (f" (Ubuntu has {_upstream(candidate)})" if newer(candidate, installed) else "") + ".")
    if newer(candidate, installed):
        status.actions.append(Action(
            "hackrf_apt", f"Install hackrf {_upstream(candidate)}",
            "Installs the newer hackrf tools and firmware files from Ubuntu.",
            lambda say: apt_install(["hackrf", "hackrf-firmware"], say)))
    image = FIRMWARE_DIR / HACKRF_FIRMWARE.get(getattr(device, "name", "HackRF One"), "hackrf_one_usb.bin")
    shipped = firmware_file_version(image)
    flashed = None
    if device is not None:
        try:
            say("Asking the HackRF its firmware version…")
            flashed = device.firmware()
            status.lines.append(f"Its firmware is {flashed}.")
        except Exception as error:
            status.lines.append(f"Couldn't read its firmware: {error}.")
    say("Looking up the newest HackRF release on GitHub…")
    latest = github_latest(HACKRF_REPO)
    latest_plain = latest.lstrip("v") if latest else None
    if latest:
        status.lines.append(f"Newest release: {latest_plain}.")
    if device is not None and shipped and flashed and newer(shipped, flashed):
        status.actions.append(Action(
            "hackrf_flash", f"Flash firmware {shipped}",
            f"Writes {image.name} ({shipped}, from the installed package) to the HackRF and resets it.",
            lambda say: flash_hackrf(device, image, say),
            confirm=f"Flash firmware {shipped} into the {device.model}? Keep it plugged in until "
                    "it's done (under a minute)."))
    best_here = max(filter(None, [shipped, _upstream(installed) if installed else None]),
                    key=version_key, default=None)
    if device is not None and latest_plain and newer(latest_plain, best_here) and newer(latest_plain, flashed):
        status.actions.append(Action(
            "hackrf_github", f"Get {latest_plain} from GitHub",
            f"Downloads hackrf-{latest_plain}.tar.xz (about 26 MB) from Great Scott Gadgets and "
            "flashes its firmware. The tools stay Ubuntu's until Ubuntu catches up.",
            lambda say: hackrf_from_github(device, latest, say),
            confirm=f"Download HackRF {latest_plain} and flash its firmware into the "
                    f"{device.model}? Keep it plugged in until it's done."))
    status.current = not status.actions
    if status.current:
        status.lines.append("Nothing newer to install.")
    return status


def flash_hackrf(device, image: Path, say: Say) -> str:
    """hackrf_spiflash -w, which checks the image is for this board, then -R to restart it."""
    if not image.is_file():
        raise RuntimeError(f"{image} isn't there.")
    command = ["hackrf_spiflash", *(["-d", device.usb.serial] if device.usb.serial else []),
               "-w", str(image), "-R"]
    with device.exclusive():
        say(f"Flashing {image.name}…")
        code, tail = run_logged(command, say, 180)
    if code != 0:
        raise RuntimeError(f"hackrf_spiflash failed: {tail.splitlines()[-1] if tail else code}")
    return ("Flashed and reset. If it doesn't come back by itself within a few seconds, unplug "
            "it and plug it in again.")


def hackrf_from_github(device, tag: str, say: Say) -> str:
    version = tag.lstrip("v")
    url = f"https://github.com/{HACKRF_REPO}/releases/download/{tag}/hackrf-{version}.tar.xz"
    archive = download(url, updates_dir() / f"hackrf-{version}.tar.xz", say, limit_mb=80)
    wanted = HACKRF_FIRMWARE.get(device.name, "hackrf_one_usb.bin")
    target = updates_dir() / f"{version}-{wanted}"
    with tarfile.open(archive) as bundle:
        member = next((m for m in bundle.getmembers()
                       if m.isfile() and m.name.endswith(f"firmware-bin/{wanted}")), None)
        if member is None:
            raise RuntimeError(f"No firmware-bin/{wanted} in the release.")
        source = bundle.extractfile(member)
        target.write_bytes(source.read() if source else b"")
    hand_back(target)
    return flash_hackrf(device, target, say)


# ---------- RTL-SDR ----------

def check_rtlsdr(devices: list, say: Say = lambda _t: None) -> Status:
    status = Status("RTL-SDR")
    status.lines.append("An RTL-SDR has no firmware to update; its driver and tools do update.")
    refresh_apt(say)
    installed, candidate = apt_versions("rtl-sdr")
    local = _local_rtl_version()
    status.lines.append(f"rtl-sdr {_upstream(installed) if installed else 'not installed'} from Ubuntu"
                        + (f", and {local} built into /usr/local" if local else "") + ".")
    if newer(candidate, installed):
        status.actions.append(Action(
            "rtl_apt", f"Install rtl-sdr {_upstream(candidate)}",
            "Installs the newer rtl-sdr package from Ubuntu.",
            lambda say: apt_install(["rtl-sdr"], say)))
    blog_v4 = any(getattr(d, "name", "").endswith("V4") for d in devices)
    # A Blog V3 works with either, but its antenna power (rtl_biast) and its HF input
    # (direct sampling on the Q branch) need the RTL-SDR Blog fork's tools
    blog = blog_v4 or any("Blog" in getattr(d, "name", "") for d in devices)
    repo = RTLSDR_BLOG_REPO if blog else RTLSDR_REPO
    say(f"Looking up the newest {repo} release on GitHub…")
    latest = github_latest(repo)
    have = max(filter(None, [_upstream(installed) if installed else None, local]), key=version_key,
               default=None)
    if latest:
        status.lines.append(f"Newest {'RTL-SDR Blog' if blog else 'osmocom'} release: {latest}"
                            + (" (the Blog V4 needs this fork)." if blog_v4 else
                               " (for the V3's antenna power and HF input)." if blog else "."))
    if latest and (blog and not local or newer(latest, have)):
        status.actions.append(Action(
            "rtl_build", f"Build {latest} from source",
            f"Downloads {repo} {latest}, builds it (cmake, a minute or two) and installs it into "
            "/usr/local, which takes over from Ubuntu's copy.",
            lambda say: build_rtlsdr(repo, latest, say),
            confirm=f"Build and install {repo} {latest} into /usr/local? It needs build tools "
                    "(build-essential, cmake, libusb-1.0-0-dev), which are installed first."))
    status.current = not status.actions
    if status.current:
        status.lines.append("Nothing newer to install.")
    return status


def _local_rtl_version() -> str | None:
    """The version of a source build in /usr/local, from the marker build_rtlsdr leaves."""
    try:
        return Path("/usr/local/share/wifisurveyor-rtl-sdr-version").read_text().strip() or None
    except OSError:
        return None


def build_rtlsdr(repo: str, tag: str, say: Say) -> str:
    apt_install(["build-essential", "cmake", "pkg-config", "libusb-1.0-0-dev"], say)
    archive = download(f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz",
                       updates_dir() / f"{repo.replace('/', '-')}-{tag}.tar.gz", say, limit_mb=30)
    work = Path(tempfile.mkdtemp(prefix="rtl-sdr-build-"))
    with tarfile.open(archive) as bundle:
        safe = [m for m in bundle.getmembers()
                if not m.name.startswith("/") and ".." not in Path(m.name).parts
                and (m.isfile() or m.isdir())]
        bundle.extractall(work, members=safe)
    top = next(p for p in work.iterdir() if p.is_dir())
    script = (f"set -e; cd '{top}'; mkdir -p build; cd build; "
              "cmake .. -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON -DCMAKE_INSTALL_PREFIX=/usr/local; "
              "make -j\"$(nproc)\"; make install; ldconfig; "
              f"printf %s '{tag}' > /usr/local/share/wifisurveyor-rtl-sdr-version; "
              "udevadm control --reload-rules || true")
    say(f"Building {repo} {tag}…")
    code, tail = run_logged(root_command(["sh", "-c", script]), say, 1800)
    shutil.rmtree(work, ignore_errors=True)
    if code != 0:
        raise RuntimeError(f"The build failed: {tail.splitlines()[-1] if tail else code}")
    return (f"Installed {tag} into /usr/local. Unplug the RTL-SDR and plug it in again so the "
            "new udev rule applies.")


# ---------- tinySA ----------

TINYSA_FILE = re.compile(r"(tinySA4?_v(\d+)\.(\d+)-(\d+)-g([0-9a-f]+))\.bin")


def tinysa_version_key(version: str) -> tuple[int, ...]:
    """"tinySA4_v1.4-252-g2b4aa51" -> (1, 4, 252): release, then commits since it."""
    match = re.search(r"v(\d+)\.(\d+)(?:-(\d+))?", version or "")
    if not match:
        return ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def tinysa_latest(ultra: bool) -> tuple[str, str] | None:
    """(version, .bin URL) of the newest firmware in the author's DFU folder."""
    base = TINYSA_DFU["ultra" if ultra else "original"]
    listing = outbound.fetch(base, timeout=20).decode("utf-8", "replace")
    prefix = "tinySA4_" if ultra else "tinySA_"
    found = [m for m in TINYSA_FILE.finditer(listing) if m.group(1).startswith(prefix)]
    if not found:
        return None
    best = max(found, key=lambda m: tinysa_version_key(m.group(1)))
    return best.group(1), base + best.group(1) + ".bin"


def check_tinysa(device, say: Say = lambda _t: None,
                 release: Callable[[], None] | None = None) -> Status:
    status = Status(getattr(device, "name", "tinySA") if device is not None else "tinySA")
    ultra = bool(getattr(device, "ultra", True))
    version = getattr(device, "model", "") if device is not None else ""
    if version:
        status.lines.append(f"Its firmware is {version}.")
    elif usb_present(DFU_ID):
        status.lines.append("A tinySA is waiting in DFU mode.")
    say("Looking up the newest tinySA firmware…")
    try:
        latest = tinysa_latest(ultra)
    except Exception as error:
        status.lines.append(f"Couldn't reach the tinySA download folder: {error}.")
        return status
    if latest is None:
        status.lines.append("Couldn't find a firmware file in the download folder.")
        return status
    status.lines.append(f"Newest: {latest[0]}.")
    if not version or tinysa_version_key(latest[0]) > tinysa_version_key(version):
        status.actions.append(Action(
            "tinysa_flash", f"Install {latest[0].split('_', 1)[-1]}",
            f"Downloads {latest[0]}.bin, then waits for the tinySA in DFU mode and flashes it "
            "with dfu-util.",
            lambda say: flash_tinysa(device, latest[0], latest[1], say, release=release),
            confirm=DFU_STEPS.format(how=DFU_HOW["ultra" if ultra else "original"])))
    status.current = not status.actions
    if status.current:
        status.lines.append("It's up to date.")
    return status


DFU_HOW = {"ultra": "switch it off, then hold the jog button down while switching it on (the "
                    "screen stays black)",
           "original": "on the tinySA choose CONFIG → DFU (or hold the jog button while switching "
                       "it on)"}
DFU_STEPS = ("Firmware goes in over USB while the tinySA is in DFU mode. When you press Install, "
             "the app closes its connection to the tinySA and waits up to two minutes: {how}. "
             "Flashing takes about 20 seconds; don't unplug it meanwhile. It restarts by itself "
             "afterwards.")


def flash_tinysa(device, version: str, url: str, say: Say, wait_s: float = 120,
                 release: Callable[[], None] | None = None) -> str:
    target = download(url, updates_dir() / f"{version}.bin", say, limit_mb=4)
    data = target.read_bytes()
    # The image names its own version: a truncated or wrong download won't
    if len(data) < 32_000 or version.split("_", 1)[-1].encode() not in data:
        raise RuntimeError(f"{target.name} doesn't look like tinySA firmware {version}; not flashing it.")
    if not shutil.which("dfu-util"):
        apt_install(["dfu-util"], say)
    if device is not None:
        if release is not None:
            release()
        else:
            device.close()
    say("Put the tinySA in DFU mode now. Waiting for it…")
    deadline = time.monotonic() + wait_s
    while not usb_present(DFU_ID):
        if time.monotonic() > deadline:
            raise TimeoutError("The tinySA didn't appear in DFU mode within two minutes. "
                               "Nothing was flashed.")
        time.sleep(0.5)
    say("Found it in DFU mode. Flashing…")
    code, tail = run_logged(root_command(["dfu-util", "-d", "0483:df11", "-a", "0", "-s",
                                          "0x08000000:leave", "-D", str(target)]), say, 180)
    if code != 0 and "File downloaded successfully" not in tail:
        raise RuntimeError(f"dfu-util failed: {tail.splitlines()[-1] if tail else code}")
    return f"Flashed {version}. The tinySA restarts by itself and the app reconnects to it."


# ---------- RF Explorer ----------

def uploader_dir() -> Path:
    return make_dir(updates_dir() / "rfexplorer-uploader")


def uploader_exe() -> Path | None:
    found = sorted(glob.glob(str(uploader_dir() / "**" / "*.exe"), recursive=True))
    return Path(found[0]) if found else None


def check_rfexplorer(device, say: Say = lambda _t: None,
                     release: Callable[[], None] | None = None) -> Status:
    status = Status("RF Explorer")
    firmware = getattr(device, "firmware", "") if device is not None else ""
    if firmware:
        status.lines.append(f"Its firmware is v{firmware}.")
    status.lines.append("RF Explorer firmware only installs through RF Explorer's own Uploader, "
                        "which checks for the latest version itself.")
    mono = shutil.which("mono") is not None
    exe = uploader_exe()
    status.lines.append(("The Uploader is downloaded" if exe else "The Uploader isn't downloaded yet")
                        + ("; Mono is installed." if mono else "; it needs Mono (mono-complete, a few hundred MB)."))
    status.actions.append(Action(
        "rfe_uploader", "Open the Uploader" if exe and mono else "Get the Uploader",
        "Downloads RF Explorer's Linux Uploader" + ("" if mono else " and installs Mono") +
        ", lets go of the RF Explorer, and opens the Uploader to check and install firmware.",
        lambda say: open_uploader(device, say, release=release)))
    return status


def open_uploader(device, say: Say, release: Callable[[], None] | None = None) -> str:
    if not shutil.which("mono"):
        apt_install(["mono-complete"], say)
    exe = uploader_exe()
    if exe is None:
        archive = download(RFE_UPLOADER, updates_dir() / "RFExplorer_UploaderLinux.zip", say, limit_mb=60)
        with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as bundle:
            for member in bundle.infolist():
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    continue
                bundle.extract(member, uploader_dir())
        for path in uploader_dir().rglob("*"):
            hand_back(path)
        exe = uploader_exe()
        if exe is None:
            raise RuntimeError("The Uploader download had no program in it.")
    if device is not None:
        if release is not None:
            release()
        else:
            device.close()
    command, env = as_person(["mono", str(exe)])
    subprocess.Popen(command, cwd=str(exe.parent), env=env, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return ("The RF Explorer Uploader is open (its window may take a few seconds). The app let go "
            "of the RF Explorer; it reconnects once the Uploader is closed and Setup → Check again "
            "is pressed.")


def as_person(command: list[str]) -> tuple[list[str], dict]:
    """
    A command run as the person rather than root, on their display: the Uploader stores its
    settings in the home folder and RF Explorer says not to run it as root. Mono's windows
    need X11, which GNOME's Wayland session provides through XWayland.
    """
    env = dict(os.environ)
    user = invoking_user()
    if user is None:
        env.setdefault("DISPLAY", ":0")
        return command, env
    import pwd

    uid = user[0]
    name = pwd.getpwuid(uid).pw_name
    runtime = f"/run/user/{uid}"
    keep = {"HOME": str(user[2]), "USER": name, "LOGNAME": name, "XDG_RUNTIME_DIR": runtime,
            "DISPLAY": os.environ.get("DISPLAY") or ":0",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    auth = os.environ.get("XAUTHORITY") or next(iter(sorted(glob.glob(f"{runtime}/.mutter-Xwaylandauth.*"))), "")
    if auth:
        keep["XAUTHORITY"] = auth
    if os.environ.get("WAYLAND_DISPLAY"):
        keep["WAYLAND_DISPLAY"] = os.environ["WAYLAND_DISPLAY"]
    # runuser itself runs as root with root's environment; env hands the person's to mono
    return (["runuser", "-u", name, "--", "env", "-i", *[f"{k}={v}" for k, v in keep.items()],
             *command], dict(os.environ))
