#!/usr/bin/env python3
"""Install a General MIDI soundfont into ``soundfonts/``.

Why this script exists at all: FluidSynth ships a 307K freeware bank from 1996
(VintageDreamsWaves) which renders audio fine but sounds like a 1996 sound card,
because that is exactly what it was for. Every candidate in a round then sounds
equally dated, which is a real problem for a system whose whole output is audio.

Why fetch rather than commit: both default banks below are MIT licensed, so
committing them would be legal. They are 38MB and 141MB. That is what makes
fetch-on-setup correct, not licensing.

Three properties this script is built around:

* **The license is printed, every time, from the file that shipped with the
  bank.** Not from a hardcoded string that could drift from reality, and not from
  our summary of it. A user should never wonder what they just installed.
* **Checksums are pinned and checked.** Where upstream publishes one we use
  theirs; where it does not, we pin one we verified ourselves and say so.
* **No third-party imports.** This runs before ``pip install`` in
  ``scripts/setup.sh`` and early in the Docker build, so the standard library is
  all it may assume.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
SOUNDFONTS_DIR = REPO_ROOT / "soundfonts"

# Both SF2 and SF3 are RIFF containers with an ``sfbk`` form type; SF3 differs
# only in that its sample data is Ogg Vorbis rather than PCM. So one magic check
# covers both, and it is worth doing: the common failure mode for a fetch script
# is silently saving an HTML error page under a .sf2 name.
RIFF_MAGIC = b"RIFF"
SFBK_FORM = b"sfbk"

USER_AGENT = "houseband-fetch-soundfont/1 (+https://github.com/)"


@dataclass(frozen=True)
class Bank:
    """One installable soundfont and everything we claim to know about it."""

    key: str
    filename: str
    url: str
    approx_size: str
    license_id: str
    license_verified: str
    """How the license was confirmed, or why it could not be. Printed verbatim."""
    sound: str
    """Honest note on how it sounds, so the choice is not a coin flip."""
    sha256: str
    checksum_provenance: str
    """Where the pinned digest came from. Upstream-published beats self-computed."""
    expected_size: int
    #: Path of the license file to save beside the bank.
    license_filename: str
    #: For tarball sources: the member holding the bank, and the member holding
    #: its license. ``None`` means the URL is the bank itself.
    tar_member: str | None = None
    tar_license_member: str | None = None
    #: Only for direct downloads: where to fetch the license text from.
    license_url: str | None = None


BANKS: tuple[Bank, ...] = (
    Bank(
        key="musescore-general",
        filename="MuseScore_General.sf3",
        url=(
            "https://ftp.osuosl.org/pub/musescore/soundfont/"
            "MuseScore_General/MuseScore_General.sf3"
        ),
        license_url=(
            "https://ftp.osuosl.org/pub/musescore/soundfont/"
            "MuseScore_General/MuseScore_General_License.md"
        ),
        license_filename="MuseScore_General_License.md",
        approx_size="40 MB",
        license_id="MIT",
        license_verified=(
            "VERIFIED. MuseScore_General_License.md, fetched from the same "
            "upstream directory as the bank, is the MIT license text, "
            "copyright (c) 2014-16 Michael Cowgill and (c) 2000-2002, 2008 "
            "Frank Wen. Debian ships it as musescore-general-soundfont under "
            "the same terms."
        ),
        sound=(
            "The MuseScore 4 default bank. Best all-round choice: usable "
            "acoustic piano, strings and drum kits, and a small enough download "
            "that setup is not a coffee break."
        ),
        sha256="5b85b6c2c61d10b2b91cddd41efcce7b25cd31c8271d511c73afafbef20b6fa3",
        checksum_provenance=(
            "SELF-COMPUTED. Upstream publishes no checksum file for this "
            "directory, so this digest was computed from a download verified on "
            "2026-07-26. It pins the bytes against corruption and mirror "
            "tampering, but it is not an upstream signature."
        ),
        expected_size=39_900_972,
    ),
    Bank(
        key="fluidr3-gm",
        filename="FluidR3_GM.sf2",
        url=(
            "https://deb.debian.org/debian/pool/main/f/fluid-soundfont/"
            "fluid-soundfont_3.1.orig.tar.gz"
        ),
        tar_member="fluid-soundfont-3.1/FluidR3_GM.sf2",
        tar_license_member="fluid-soundfont-3.1/COPYING",
        license_filename="FluidR3_GM_COPYING.txt",
        approx_size="135 MB download, 148 MB installed",
        license_id="MIT",
        license_verified=(
            "VERIFIED. The upstream tarball's own README says 'I hereby release "
            "Fluid under the MIT license, as described in COPYING', and COPYING "
            "is the MIT text, copyright (c) 2000-2002, 2008 Frank Wen. Debian's "
            "reviewed copyright file for fluid-soundfont records the same."
        ),
        sound=(
            "The classic Linux GM bank, and the ancestor of MuseScore General. "
            "Fuller and wetter than MuseScore General on some patches, thinner "
            "on others. Worth trying if the default's piano bothers you."
        ),
        # Digest of the .tar.gz, not of the extracted .sf2, because that is what
        # Debian signs. The extracted file's digest is checked separately.
        sha256="2621acaa1c78e4abdb24bdd163230cc577e61276936d6aa6e3180582142f0343",
        checksum_provenance=(
            "UPSTREAM-PUBLISHED. This is the Checksums-Sha256 entry for "
            "fluid-soundfont_3.1.orig.tar.gz in Debian's PGP-signed "
            "fluid-soundfont_3.1-6.dsc, independently reproduced here."
        ),
        expected_size=134_835_922,
    ),
)

# The digest of the bank extracted from the tarball, kept apart from Bank.sha256
# so that field can always mean "digest of the thing at the URL".
EXTRACTED_SHA256 = {
    "fluidr3-gm": "74594e8f4250680adf590507a306655a299935343583256f3b722c48a1bc1cb0",
}

DEFAULT_BANK = "musescore-general"

BANKS_BY_KEY = {bank.key: bank for bank in BANKS}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def info(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def rule(title: str = "") -> None:
    info(f"\n{'-' * 72}")
    if title:
        info(title)
        info("-" * 72)


def print_license(bank: Bank, text: str | None) -> None:
    """Show the license for ``bank``, preferring the text that shipped with it."""
    rule(f"LICENSE: {bank.filename} -- {bank.license_id}")
    info(bank.license_verified)
    if text:
        info("")
        info("Full text as shipped with the bank:")
        info("")
        for line in text.strip().splitlines():
            info(f"  {line}")
    else:
        info("")
        info(
            "Could not retrieve the license file itself. Do not redistribute "
            "this bank until you have read it at the source."
        )
    info("-" * 72)


# ---------------------------------------------------------------------------
# Download and verification
# ---------------------------------------------------------------------------


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=120)


def fetch_text(url: str) -> str | None:
    try:
        with _open(url) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        info(f"  ! could not fetch {url}: {exc}")
        return None


def _copy_with_progress(
    source, dest_path: Path, total: int, label: str
) -> tuple[int, str]:
    """Stream ``source`` to ``dest_path``, returning (bytes written, sha256)."""
    digest = hashlib.sha256()
    written = 0
    last_report = -1
    with dest_path.open("wb") as handle:
        while chunk := source.read(1 << 20):
            handle.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if total:
                percent = int(100 * written / total)
                if percent >= last_report + 5:
                    last_report = percent
                    info(f"  {label}: {percent:3d}%  ({written / 1e6:.0f} MB)")
    if not total:
        info(f"  {label}: {written / 1e6:.0f} MB")
    return written, digest.hexdigest()


def check_soundfont_file(path: Path, minimum: int = 200_000) -> list[str]:
    """Sanity-check that ``path`` is a plausible, non-trivial SF2/SF3.

    Cheap, and it catches the whole class of failure where a redirect or an
    error page lands on disk under a soundfont's name and the real problem only
    surfaces later as a confusing FluidSynth error.
    """
    problems: list[str] = []
    size = path.stat().st_size
    if size < minimum:
        problems.append(
            f"only {size} bytes, which is too small to be a real GM bank "
            f"(expected at least {minimum})"
        )
    with path.open("rb") as handle:
        header = handle.read(12)
    if header[0:4] != RIFF_MAGIC:
        problems.append(f"does not start with RIFF (got {header[0:4]!r})")
    elif header[8:12] != SFBK_FORM:
        problems.append(
            f"is a RIFF file but its form type is {header[8:12]!r}, not 'sfbk'"
        )
    return problems


def verify_digest(actual: str, expected: str, what: str, enforce: bool) -> None:
    if actual == expected:
        info(f"  checksum OK ({what})")
        return
    message = (
        f"checksum mismatch for {what}\n"
        f"  expected {expected}\n"
        f"  actual   {actual}\n"
        "Either upstream republished the file or the download is not what it "
        "claims to be. Re-run with --no-verify-checksum only if you have "
        "established which."
    )
    if enforce:
        raise SystemExit(f"FAILED: {message}")
    info(f"  ! {message}")


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


def install_direct(bank: Bank, dest_dir: Path, verify: bool) -> Path:
    target = dest_dir / bank.filename
    with tempfile.TemporaryDirectory(dir=dest_dir) as staging:
        staged = Path(staging) / bank.filename
        info(f"  downloading {bank.url}")
        with _open(bank.url) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total and total != bank.expected_size:
                info(
                    f"  ! upstream reports {total} bytes, expected "
                    f"{bank.expected_size}. Continuing; the checksum decides."
                )
            _, digest = _copy_with_progress(response, staged, total, bank.filename)
        verify_digest(digest, bank.sha256, bank.filename, verify)
        if problems := check_soundfont_file(staged):
            raise SystemExit(
                f"FAILED: downloaded {bank.filename} is not a usable soundfont:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )
        # Atomic-ish: the file only appears at its real name once it is whole and
        # verified, so an interrupted run cannot leave a half-file that
        # find_soundfont() would happily pick up.
        shutil.move(str(staged), str(target))
    return target


def install_from_tarball(bank: Bank, dest_dir: Path, verify: bool) -> Path:
    """Extract one bank out of a source tarball, streaming it.

    Streaming rather than downloading-then-opening keeps peak disk use to the
    extracted bank instead of the bank plus a 135MB archive, and lets us hash
    the archive on the way past for free.
    """
    assert bank.tar_member
    target = dest_dir / bank.filename
    outer = hashlib.sha256()

    class Hashing:
        """A read-only shim that digests the archive as tarfile consumes it."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def read(self, size: int = -1) -> bytes:
            chunk = self._wrapped.read(size)
            outer.update(chunk)
            return chunk

    with tempfile.TemporaryDirectory(dir=dest_dir) as staging:
        staged = Path(staging) / bank.filename
        license_text: str | None = None
        info(f"  downloading and extracting {bank.url}")
        with _open(bank.url) as response:
            total = int(response.headers.get("Content-Length") or 0)
            # Stream mode ("r|gz") means members arrive in archive order and can
            # be read exactly once, which is why COPYING is captured as it goes
            # past rather than looked up afterwards.
            with tarfile.open(fileobj=Hashing(response), mode="r|gz") as archive:
                for member in archive:
                    if member.name == bank.tar_license_member:
                        handle = archive.extractfile(member)
                        if handle:
                            license_text = handle.read().decode(
                                "utf-8", errors="replace"
                            )
                    elif member.name == bank.tar_member:
                        handle = archive.extractfile(member)
                        if handle is None:
                            raise SystemExit(
                                f"FAILED: {bank.tar_member} is not a regular file."
                            )
                        _, inner = _copy_with_progress(
                            handle, staged, member.size, bank.filename
                        )
                        if expected := EXTRACTED_SHA256.get(bank.key):
                            verify_digest(inner, expected, bank.filename, verify)

        if not staged.exists():
            raise SystemExit(
                f"FAILED: {bank.tar_member} was not found in the archive."
            )
        verify_digest(outer.hexdigest(), bank.sha256, Path(bank.url).name, verify)
        if problems := check_soundfont_file(staged):
            raise SystemExit(
                f"FAILED: extracted {bank.filename} is not a usable soundfont:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )
        if license_text:
            (dest_dir / bank.license_filename).write_text(license_text)
        shutil.move(str(staged), str(target))

    print_license(bank, license_text)
    return target


def install(bank: Bank, dest_dir: Path, verify: bool = True, force: bool = False) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / bank.filename

    if target.exists() and not force:
        problems = check_soundfont_file(target)
        if not problems:
            info(f"{bank.filename} is already installed at {target}")
            info("  (nothing to do; pass --force to re-download)")
            _show_existing_license(bank, dest_dir)
            return target
        info(f"{target} exists but is not usable, re-downloading:")
        for problem in problems:
            info(f"  - {problem}")

    rule(f"Installing {bank.filename} ({bank.approx_size}, {bank.license_id})")
    info(bank.sound)
    info("")
    info(f"Checksum: {bank.checksum_provenance}")
    info("")

    if bank.tar_member:
        target = install_from_tarball(bank, dest_dir, verify)
    else:
        target = install_direct(bank, dest_dir, verify)
        license_text = fetch_text(bank.license_url) if bank.license_url else None
        if license_text:
            (dest_dir / bank.license_filename).write_text(license_text)
        print_license(bank, license_text)

    info("")
    info(f"Installed {target} ({target.stat().st_size / 1e6:.0f} MB)")
    info(f"License saved to {dest_dir / bank.license_filename}")
    info(
        "The MIT license requires this notice to travel with any copy of the "
        "bank you redistribute. It says nothing about audio you render with it."
    )
    return target


def _show_existing_license(bank: Bank, dest_dir: Path) -> None:
    path = dest_dir / bank.license_filename
    print_license(bank, path.read_text() if path.exists() else None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def list_banks() -> None:
    print("Available soundfonts:\n")
    for bank in BANKS:
        default = "  (default)" if bank.key == DEFAULT_BANK else ""
        print(f"{bank.key}{default}")
        print(f"  file      {bank.filename}")
        print(f"  size      {bank.approx_size}")
        print(f"  license   {bank.license_id}")
        print(f"  verified  {bank.license_verified}")
        print(f"  checksum  {bank.checksum_provenance}")
        print(f"  sound     {bank.sound}")
        print(f"  source    {bank.url}")
        print()
    print(
        "Also usable, but not auto-installed:\n"
        "  VintageDreamsWaves-v2.sf2  307K, 1996 freeware, bundled with\n"
        "    FluidSynth. Found automatically as a last resort. Renders, but\n"
        "    sounds like the AWE32 sound card it was written for.\n"
        "  GeneralUser GS  ~30MB, custom permissive license. Its author asks\n"
        "    that nobody hotlink his download files, so install it by hand from\n"
        "    schristiancollins.com into soundfonts/ if you want it. See\n"
        "    docs/soundfonts.md for the license caveat.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install a General MIDI soundfont into soundfonts/. Idempotent: an "
            "already-installed bank is left alone."
        ),
        epilog="Every bank prints its license before this script exits.",
    )
    parser.add_argument(
        "--list", action="store_true", help="show available banks and their licenses"
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_BANK,
        choices=sorted(BANKS_BY_KEY),
        help=f"which bank to install (default: {DEFAULT_BANK})",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=SOUNDFONTS_DIR,
        help="install directory (default: soundfonts/ in the repo root)",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download even if already present"
    )
    parser.add_argument(
        "--no-verify-checksum",
        action="store_true",
        help="proceed on a checksum mismatch instead of failing. Only useful if "
        "upstream has republished a file and you have confirmed why.",
    )
    args = parser.parse_args(argv)

    if args.list:
        list_banks()
        return 0

    bank = BANKS_BY_KEY[args.name]
    try:
        target = install(
            bank, args.dest, verify=not args.no_verify_checksum, force=args.force
        )
    except urllib.error.URLError as exc:
        info(
            f"FAILED: could not reach {bank.url}: {exc}\n"
            "If you have no network access, houseband will fall back to the "
            "307K bank bundled with FluidSynth, or you can point HOUSEBAND_SF2 "
            "at any .sf2/.sf3 you already have."
        )
        return 1

    # The path goes to stdout, alone, so a caller can use it:
    #   SF=$(python scripts/fetch_soundfont.py)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
