#!/usr/bin/env python3
"""Turn the self-contained applicant board into an artifact-shaped body.

The Artifact host supplies its own <!doctype>, <html>, <head> and <body>, so a
full document published as-is nests one page inside another. Everything the
board needs to look like itself lives in <title> and <style>; the rest is the
body's own markup.

Runs from cron so the file is always current: publishing is the only step that
needs a session, and it should never also need a conversion.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

src = Path(sys.argv[1] if len(sys.argv) > 1
           else Path.home() / "talent-engine-runtime/board.html")
dst = Path(sys.argv[2] if len(sys.argv) > 2
           else Path.home() / "talent-engine-runtime/board.artifact.html")

html = src.read_text(encoding="utf-8")
title = re.search(r"<title>(.*?)</title>", html, re.S)
styles = re.findall(r"<style\b[^>]*>.*?</style>", html, re.S | re.I)
body = re.search(r"<body\b[^>]*>(.*)</body>", html, re.S | re.I)
if not body:
    raise SystemExit(f"{src}: no <body> found -- board format changed")

parts = [f"<title>{title.group(1).strip() if title else 'Applicant Board'}</title>"]
parts += styles
parts.append(body.group(1).strip())
dst.write_text("\n".join(parts) + "\n", encoding="utf-8")
print(f"{dst} ({dst.stat().st_size:,} bytes)")
