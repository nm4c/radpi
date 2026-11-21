"""Which Raspberry Pi this is, and what that means for the RadPi HATs.

Setting the HATs up (overlay, serial port, codec routing) is deploy/setup-hat.sh's job; this is
what the server needs at run time: the right serial port for the SA818, and a hint to print when
the hardware isn't set up.
"""

from pathlib import Path

SETUP_HINT = ("with a RadPi HAT, run  sudo ./deploy/setup-hat.sh control  (or sa818) from the git "
              "clone, reboot, then  sudo ./deploy/setup-hat.sh --check")


def model():
    """'Raspberry Pi 5 Model B Rev 1.0' etc., or '' when not on a Pi."""
    try:
        return Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode(errors="replace")
    except OSError:
        return ""


def generation():
    """5 for a Pi 5 / 500 / CM5, 4 for a Pi 4 / 400 / CM4, 3 for older, None if not a Pi."""
    m = model()
    if "Raspberry Pi" not in m:
        return None
    for gen in (5, 4):
        if f"Pi {gen}" in m or f"Pi {gen}00" in m or f"Compute Module {gen}" in m:
            return gen
    return 3


def sa818_port():
    """The UART on GPIO14/15. On a Pi 5, /dev/serial0 is the separate 3-pin debug connector, so
    it's /dev/ttyAMA0 there (with dtparam=uart0=on); on older Pis /dev/serial0 follows whichever
    UART is on GPIO14/15."""
    return "/dev/ttyAMA0" if generation() == 5 else "/dev/serial0"
