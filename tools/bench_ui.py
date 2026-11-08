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

## The strips (M5)

Risk R3 was that two minimap render paths become the frame-budget bottleneck.
Three numbers settle it, and they are measured here rather than reasoned about:

* **a strip re-aggregation** -- what a resize or a newly opened file costs,
  which is the range-sum of SPEC 8.1 plus the NumPy that turns it into pixels
* **a strip repaint** -- what a scroll costs, which should be one `drawImage`
  and no aggregation at all, because the viewport box moves and the data under
  it does not
* **both strips together**, since D4 ships two of them and they repaint in the
  same frame as the view
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

from logweave.highlight import Ruleset  # noqa: E402
from logweave.session import Session  # noqa: E402
from logweave.ui.mergedview import MergedLogView  # noqa: E402
from logweave.ui.strips import MinimapStrip, TimelineStrip  # noqa: E402

BUDGET_MS = 16.0
FMT = QImage.Format.Format_ARGB32_Premultiplied


def synth_traces(path: Path, n: int) -> Path:
    """
    A log where nearly every row has styled *spans*, not just a line colour.

    The span path is the one highlighting adds to the frame: each span is a
    separate `drawText` over text already drawn. A file of exception class
    names is the realistic worst case -- a Java service in trouble logs
    thousands of them -- and it is the case that would hide a per-span cost if
    the benchmark only ever measured lines that match a whole-line rule.
    """
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            ts = (f"2026-10-04 {14 + i // 3600 % 10:02d}:"
                  f"{i // 60 % 60:02d}:{i % 60:02d}.{i * 977 % 1000000:06d}")
            classes = " caused by ".join(
                f"com.example.svc.Layer{j}TimeoutException"
                for j in range(1 + i % 6)
            )
            fh.write(f"{ts} ERROR request failed: {classes}\n")
    return path


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


def bench_view(session: Session, height: int, n: int = 60,
               *, highlight: bool = True) -> dict[str, float]:
    view = MergedLogView(session)
    if not highlight:
        # An empty ruleset still goes through `style_of`, so this measures the
        # rules' cost rather than the cost of the code path that applies them.
        view.highlights = Ruleset([])
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
    spans = max((len(view.highlights.style_of(t).spans)
                 for t in (r.message for r in session.rows(0, 40))), default=0)
    return {"rows": rows, "full": full, "step": step_ms, "spans": spans,
            "per_row": full / rows * 1000, "viewport": actual,
            # A top-level window cannot exceed the physical screen, so a 2160px
            # request on a 1080p display silently measures a 1080p window.
            "clamped": actual < height - 120}


def bench_strips(session: Session, height: int, n: int = 60) -> dict[str, float]:
    """
    The two overview strips at `height` pixels (SPEC 8, risk R3).

    Both are shown in a real window for the same reason the view is: the
    offscreen backend is not what the user runs, and a blit is exactly the kind
    of operation a fallback renderer gets wrong in a flattering direction.
    """
    strips = []
    for cls in (MinimapStrip, TimelineStrip):
        s = cls(session)
        s.move(-6000, -6000)
        s.resize(s.width(), height)
        s.show()
        strips.append(s)
    QApplication.processEvents()

    images = [QImage(s.size(), FMT) for s in strips]
    rows = session.total_rows
    rng = random.Random(5)

    def repaint():
        """A scroll: the box moves, the aggregation is memoised."""
        for s, img in zip(strips, images, strict=True):
            s.set_viewport(rng.randrange(max(1, rows - 60)), 60)
            s.render(img)

    def reaggregate():
        """A resize or a new file: the buffer and the counts are rebuilt."""
        for s, img in zip(strips, images, strict=True):
            s.session.overview.invalidate()
            s.refresh()
            s.render(img)

    both_repaint = time_ms(repaint, n)
    both_reagg = time_ms(reaggregate, n)

    # One strip on its own, so the per-strip cost is attributable rather than
    # halved on an assumption.
    one = strips[0]
    one_img = images[0]

    def one_repaint():
        one.set_viewport(rng.randrange(max(1, rows - 60)), 60)
        one.render(one_img)

    single = time_ms(one_repaint, n * 4)

    actual = strips[0].height()
    for s in strips:
        s.close()
        s.deleteLater()
    # Like the view's cases: a top-level window cannot exceed the physical
    # screen, so a 2160px request on a 1080p display silently measures a
    # 1080px strip. Reported rather than assumed, because a strip's cost is
    # linear in its height and a clamped row would read as free scaling.
    return {"repaint_one": single, "repaint_both": both_repaint,
            "reaggregate_both": both_reagg, "height": float(actual)}


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
        ("exception spans", synth_traces(out / "traces.log", args.entries)),
    ]

    print(f"\n{'case':<24} {'viewport':>9} {'rows':>6} "
          f"{'full repaint':>13} {'per row':>9} {'scroll step':>12}")
    print("-" * 78)

    worst_step = 0.0
    highlight_cost = []
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

        # The same frame with the rules removed, so the cost of highlighting is
        # attributable rather than buried in the total (FR-30 vs NFR-5).
        on = bench_view(session, 900)
        off = bench_view(session, 900, highlight=False)
        highlight_cost.append((label, on, off))
        session.close()
        print()

    print(f"{'highlighting':<24} {'rules on':>10} {'rules off':>10} "
          f"{'delta':>9} {'spans/row':>10}")
    print("-" * 68)
    for label, on, off in highlight_cost:
        delta = on["full"] - off["full"]
        print(f"{label:<24} {on['full']:>7.2f} ms {off['full']:>7.2f} ms "
              f"{delta:>+7.2f} ms {on['spans']:>10.0f}")
    print()

    print(f"{'overview strips (M5)':<24} {'tall':>7} {'1 repaint':>10} "
          f"{'2 repaint':>10} {'2 re-aggregate':>15}")
    print("-" * 68)
    worst_strip = 0.0
    session = Session()
    session.add_files([cases[0][1]])
    for height in (900, 1440, 2160):
        r = bench_strips(session, height)
        clamped = "  (clamped)" if r["height"] < height - 120 else ""
        print(f"{str(height) + 'px asked':<24} {r['height']:>5.0f}px "
              f"{r['repaint_one']:>7.3f} ms {r['repaint_both']:>7.3f} ms "
              f"{r['reaggregate_both']:>12.3f} ms{clamped}")
        worst_strip = max(worst_strip, r["reaggregate_both"])
    session.close()
    print()

    print(f"budget {BUDGET_MS:.0f} ms a frame (NFR-5).")
    print(f"worst strip cost {worst_strip:.2f} ms "
          f"= {BUDGET_MS / max(worst_strip, 1e-9):.0f}x under budget "
          f"(both strips, re-aggregating: risk R3).")
    print(f"worst scroll step {worst_step:.2f} ms "
          f"= {BUDGET_MS / worst_step:.0f}x under budget.")
    print("A full repaint is a seek or a resize, not a scroll; it exceeds the")
    print("budget above roughly 70 visible rows. See SPEC 14.1.")

    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
