# Soundfonts

houseband renders MIDI to audio by shelling out to FluidSynth, which needs a soundfont: a bank of sampled instruments mapped onto the 128 General MIDI programs.
Which bank you install is the single biggest factor in how the output sounds, and it has nothing to do with how well the agents composed.

## Why these are fetched, not committed

Not licensing.
Both default banks below are MIT licensed, so committing them would be entirely legal.

It is size.
The default bank is 40MB and the alternative is 148MB.
Git stores every version of a binary file forever, so a repo that commits a soundfont is a repo where every clone pays for it and every future update doubles the cost.

There is a second reason that matters more for a public repo.
`soundfonts/` is in `.gitignore` as a directory, not just as `*.sf2`/`*.sf3`, because users will drop their own banks in there.
Plenty of freely downloadable soundfonts are freeware-for-personal-use, or have opaque sample provenance, and an accidental `git add -A` should not be able to publish one.

## Installing

```bash
python scripts/fetch_soundfont.py            # the default, MuseScore General
python scripts/fetch_soundfont.py --list     # every option, with its license
python scripts/fetch_soundfont.py --name fluidr3-gm
```

The script is idempotent, verifies a pinned SHA-256, checks the file really is a RIFF/`sfbk` container of plausible size, and prints the license text that shipped with the bank before it exits.
It saves that license file next to the bank, because the MIT license requires the notice to travel with any copy you redistribute.

`houseband/config.py::find_soundfont` then discovers it.
Resolution order is: `HOUSEBAND_SF2` or `config.toml`, then `PREFERRED_SOUNDFONT_NAMES` inside `soundfonts/`, then any `.sf3`/`.sf2` in `soundfonts/`, then whatever FluidSynth or the distribution bundled.

## Options

### MuseScore General (default)

| | |
|---|---|
| File | `MuseScore_General.sf3` |
| Size | 40 MB (39,900,972 bytes) |
| License | **MIT (verified)** |
| Source | `https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/MuseScore_General.sf3` |
| Checksum | `5b85b6c2c61d10b2b91cddd41efcce7b25cd31c8271d511c73afafbef20b6fa3` (**self-computed**, see below) |

The MuseScore 4 default bank, maintained by S. Christian Collins and descended from FluidR3.
SF3 is the same RIFF/`sfbk` container as SF2 with Ogg-Vorbis-compressed sample data, which is how a 206MB bank becomes a 40MB download.
FluidSynth 2.x plays it directly.

**How it sounds.**
The best all-round choice here, and the reason it is the default.
Acoustic piano, strings, electric piano and the drum kits are all usable.
Overdriven and distorted guitars are the weakest patches, which is unfortunate given that "epic long-form rock" is the motivating prompt for this project: sustained power chords sound more like a synth pad than an amp.
Brass and reeds are decent but obviously sampled at a single dynamic.

**License verification.**
Confirmed, from primary sources.
`MuseScore_General_License.md`, fetched from the same upstream directory as the bank, is the MIT license text with copyright "Mono version: Copyright (c) 2014-16 Michael Cowgill" and "Copyright (c) 2000-2002, 2008 Frank Wen".
Debian packages the same file as `musescore-general-soundfont` under the same terms.
The fetch script prints this file, so you can read it yourself rather than trusting this paragraph.

**Checksum caveat, stated plainly.**
Upstream publishes no checksum file for this directory.
The SHA-256 pinned in `scripts/fetch_soundfont.py` was computed from a download verified on 2026-07-26.
That protects against corrupted downloads and a tampered mirror, but it is not an upstream signature: if the MuseScore project republishes the file, the fetch will fail with a mismatch and someone will have to establish why before updating the pin.
That is the intended behaviour.

### FluidR3 GM

| | |
|---|---|
| File | `FluidR3_GM.sf2` |
| Size | 135 MB download, 148 MB installed (148,398,306 bytes) |
| License | **MIT (verified)** |
| Source | `https://deb.debian.org/debian/pool/main/f/fluid-soundfont/fluid-soundfont_3.1.orig.tar.gz` |
| Checksum | `2621acaa1c78e4abdb24bdd163230cc577e61276936d6aa6e3180582142f0343` (**upstream-published**) |

Frank Wen's bank from 2000-2008, uncompressed SF2, and the ancestor of MuseScore General.
The long-standing default on Linux.

**How it sounds.**
Fuller and wetter than MuseScore General on some patches and thinner on others.
Its acoustic piano has more body; its strings are more obviously looped.
Worth trying if the default's piano bothers you, but it is 3.7x the download for a lateral move rather than an upgrade.

**License verification.**
Confirmed, from primary sources.
The tarball's own `README` says "I hereby release Fluid under the MIT license, as described in COPYING", and `COPYING` is the MIT text, copyright (c) 2000-2002, 2008 Frank Wen.
Debian's reviewed copyright file for `fluid-soundfont` records the same.
The fetch script extracts and prints `COPYING`.

One provenance note the author makes himself, in `README`: the bank was built partly from "samples found in the public domain" that he edited, and partly from his own recordings.
That is a weaker statement than "every sample is originally mine", though considerably stronger than most free soundfonts manage.

**Why the Debian tarball and not a more obvious mirror.**
Because Debian publishes a PGP-signed `.dsc` containing `Checksums-Sha256` for the tarball, which is a real chain of custody, and because Debian ftpmaster reviewed the license.
The alternative sources are worse than they look.
The Internet Archive item `fluidr3-gm-gs`, which is the top web result, is labelled **CC BY-ND 4.0** by whoever uploaded it.
That contradicts the upstream MIT license and, if it were true, no-derivatives would make the bank unusable.
It is an uploader-applied label rather than the author's license, but it is exactly the kind of thing that makes a "widely redistributed" soundfont a bad thing to source casually.

The fetch script streams the tarball rather than saving it, extracting only `FluidR3_GM.sf2` and `COPYING`, so peak disk use is the bank rather than the bank plus the archive.
It verifies the SHA-256 of both the archive and the extracted bank.

## Not auto-installed

### VintageDreamsWaves-v2 (the zero-network fallback)

307K, by Ian Wilson, 1996, freeware and redistributable with its notice.
FluidSynth bundles it, and `find_soundfont()` will pick it up from the Homebrew Cellar or `/usr/share/sounds/sf2/` if nothing better is present.

It is a set of synthesised waveforms written for AWE32/AWE64 sound cards, not a sampled acoustic bank, so it sounds precisely like 1996.
Its value is that a clone with no network access can still render audio and exercise the whole pipeline.
Do not judge the system's output on it, and do not use it for anything you intend to listen to twice.

### GeneralUser GS

Around 30MB, by S. Christian Collins, currently v2.0.3.
`config.py` lists `GeneralUser-GS.sf2` in `PREFERRED_SOUNDFONT_NAMES`, so if you install it by hand into `soundfonts/` it will be found.

The fetch script does not download it, for two reasons, both from the author's own `LICENSE.txt`:

1. He asks directly: "If you plan to feature GeneralUser GS on your own website, please do not link directly to my download files."
   Automating a download from his server is the same thing with extra steps.
2. The license is custom, not OSI-approved, and candid about sample provenance in a way worth reading before you build on it: "some were taken from other banks freely (and legally) available on the Internet... I cannot be 100% sure where all of the samples originated."

The grant itself is broad ("You may use GeneralUser GS without restriction for your own music creation, private or commercial") and it is a well-regarded bank.
But it is not MIT, and calling it "permissive" without that context would be sloppy.
Install it yourself from `schristiancollins.com` if you want it.

### A CC0 or public-domain GM bank

**Could not confirm one.**
Searching for a genuinely CC0 or public-domain General MIDI bank turned up candidates whose licensing could not be verified from a primary source, uploads to aggregator sites with licenses applied by the uploader rather than the author, and collections assembled from samples of unstated origin.

If you know of a GM bank with a verifiable CC0 dedication from its actual author, that is a welcome contribution.
Until then this document does not name one, because the whole point of this file is that nothing in it is asserted without a source.

## Using a bank you already have

```bash
export HOUSEBAND_SF2=/path/to/your.sf2
```

or in `config.toml` at the repo root:

```toml
soundfont = "/path/to/your.sf2"
```

Either works with any SF2 or SF3 FluidSynth 2.x can load.

## What the license covers, and what it does not

MIT applies to the soundfont as a work: copy it, modify it, redistribute it, keep the notice with it.

Whether audio you *render* with a soundfont is a "substantial portion of the Software" is a genuinely unsettled question, and reasonable people read it differently.
The MuseScore community has argued both sides.
The conservative reading, and the cheap one, is to credit the bank alongside anything you publish.
This is not legal advice, and if you are shipping commercial music rendered from one of these banks you should get some.
