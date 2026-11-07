"""
The paint budget -- NFR-5. Run: `python tools/bench_ui.py`

Separate from `tools/bench.py` because that one must run with no Qt installed
(the engine CI job checks exactly that) and this one cannot.

## Why this does not use the offscreen platform

It would be convenient and it would be wrong. The offscreen backend has **no
font database**: every family resolves to the empty string, `fixedPitch()` is
False and every glyph measures 13x13. Text timings taken there are a fallback
renderer's, not Qt's, and they were optimistic by around 2x in the direction
that matters. So this opens a real window, parked off-screen.

## What it measures, and why those two numbers

* **A scroll step** -- the common case. `scrollContentsBy` blits the viewport
  and only the newly exposed band is repainted, so this is a three-row cost
  regardless of window size.
* **A full repaint** -- a seek, a resize, a filter change. Linear in visible
  rows at roughly 230 us a row, which is where the budget eventually goes.

Long lines get their own case because they were a 10x hazard before the message
was sliced to the visible character window.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QFontInfo, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.session import Session  # noqa: E402
from logweave.ui.mergedview import MergedLogView  # noqa: E402

BUDGET_MS = 16.0
FMT = QImage.Format.Format_ARGB32_Premultiplied


def synth_log(path: Path, n: int, *, long_lines: bool) -> Path:
    """
    A log with the shape that broke the naive paint: SQL and stack traces.

    Generated rather than taken from the corpus because the interesting variable
    is line length, and the corpus is deliberately realistic rather than extreme.
    """
    rng = random.Random(11)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            ts = (f"2026-10-04 {14 + i // 3600 % 10:02d}:"
                  f"{i // 60 % 60:02d}:{i % 60:02d}.{i * 977 % 1000000:06d}")
            if not long_lines:
                body = rng.choice([
                    "INFO worker started", "DEBUG cache probe miss",
                    "WARN pool utilisation above 85 percent",
                    "ERROR connection reset by peer",
                ])
            elif i % 3 == 0:
                body = "ERROR query failed: SELECT " + ", ".join(
                    f"t{j}.col_{j}" for j in range(220))
            elif i % 3 == 1:
                body = "ERROR unhandled: " + " at ".join(
                    f"com.example.svc.Layer{j}.method{j}(Layer{j}.java:{j * 7})"
                    for j in range(60))
            else:
                body = "INFO short line"
            fh.write(f"{ts} {body}\n")
    return path


def time_ms(fn, n: int) -> float:
    fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1000


def bench_view(session: Session, height: int, n: int = 60) -> dict[str, float]:
    view = MergedLogView(session)
    # Shown, and parked far off-screen. `resize()` on a widget that was never
    # shown does not reach the viewport -- the row count came back identical at
    # every height, which is how this was caught -- so the geometry has to be
    # real even though nobody is meant to see it.
    view.move(-6000, -6000)
    view.resize(1400, height)
    view.show()
    QApplication.processEvents()

    img = QImage(view.viewport().size(), FMT)
    total = session.total_rows
    rows = view.visible_rows()

    rng = random.Random(3)

    def seek_and_paint():
        view.scroll_to_row(rng.randrange(max(1, total - rows)))
        view.viewport().render(img)

    full = time_ms(seek_and_paint, n)

    band = QRect(0, 0, view.viewport().width(), 3 * view.row_height)

    def step():
        view.viewport().render(img, QPoint(0, 0), band)

    step_ms = time_ms(step, n * 4)
    actual = img.height()
    view.close()
    view.deleteLater()
    return {"rows": rows, "full": full, "step": step_ms,
            "per_row": full / rows * 1000, "viewport": actual,
            # A top-level window cannot exceed the physical screen, so a 2160px
            # request on a 1080p display silently measures a 1080p window.
            "clamped": actual < height - 120}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entries", type=int, default=20_000)
    ap.add_argument("--out", type=Path, default=None,
                    help="where to put the synthetic logs (default: a temp dir)")
    args = ap.parse_args(argv)

    import tempfile
    out = args.out or Path(tempfile.mkdtemp(prefix="logweave-bench-"))
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    probe = MergedLogView(Session())
    info = QFontInfo(probe.font())
    print(f"font: {info.family()!r} fixedPitch={info.fixedPitch()} "
          f"cell={probe._char_w}x{probe.row_height}px")
    if not info.fixedPitch():
        print("  WARNING: not a fixed-pitch font -- the message slice assumes one")
    probe.deleteLater()

    cases = [
        ("ordinary lines", synth_log(out / "ordinary.log", args.entries,
                                     long_lines=False)),
        ("long lines", synth_log(out / "long.log", args.entries,
                                 long_lines=True)),
    ]

    print(f"\n{'case':<24} {'viewport':>9} {'rows':>6} "
          f"{'full repaint':>13} {'per row':>9} {'scroll step':>12}")
    print("-" * 78)

    worst_step = 0.0
    for label, path in cases:
        session = Session()
        session.add_files([path])
        for height in (900, 1440, 2160):
            r = bench_view(session, height)
            flag = "" if r["full"] <= BUDGET_MS else "  OVER"
            if r["clamped"]:
                flag += "  (clamped to screen)"
            print(f"{label:<20} {height:>6}px {r['viewport']:>6.0f}px "
                  f"{r['rows']:>5.0f} {r['full']:>10.2f} ms "
                  f"{r['per_row']:>7.0f} us {r['step']:>9.2f} ms{flag}")
            worst_step = max(worst_step, r["step"])
        session.close()
        print()

    print(f"budget {BUDGET_MS:.0f} ms a frame (NFR-5).")
    print(f"worst scroll step {worst_step:.2f} ms "
          f"= {BUDGET_MS / worst_step:.0f}x under budget.")
    print("A full repaint is a seek or a resize, not a scroll; it exceeds the")
    print("budget above roughly 70 visible rows. See SPEC 14.1.")

    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
