# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
# PlatformIO pre-build step (platformio.ini: extra_scripts = pre:gen_font_table.py).
# The firmware's Chinese glyph table src/almanac_font.h is not shipped: it is
# baked on this Mac from its own system fonts by host/gen_almanac_font.py
# (Songti / Hiragino Sans GB / Menlo, ~1 s). Missing -> bake it before the
# compile starts. Present -> leave it alone; after adding almanac words, run
# the generator again by hand (or delete the file) and rebuild.
# Uses /usr/bin/python3 like the host does, so the one Pillow that
# host/setup.sh installs serves both.
import os
import subprocess

Import("env")  # noqa: F821 (PlatformIO injects it)

proj = env.subst("$PROJECT_DIR")  # noqa: F821
out = os.path.join(proj, "src", "almanac_font.h")
gen = os.path.normpath(os.path.join(proj, "..", "host", "gen_almanac_font.py"))

if not os.path.exists(out):
    print("almanac_font.h missing: baking it from this Mac's system fonts (%s)" % gen)
    r = subprocess.run(["/usr/bin/python3", gen])
    if r.returncode or not os.path.exists(out):
        print("\n*** could not bake src/almanac_font.h. It needs Pillow for /usr/bin/python3:\n"
              "***   host/setup.sh   (asks, then installs it; --pillow = yes without asking)\n"
              "***   or: /usr/bin/python3 -m pip install --user pillow==11.3.0\n")
        env.Exit(1)  # noqa: F821
