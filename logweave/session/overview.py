"""
The two overview strips, as data -- SPEC 8.2, 8.3, 8.4, FR-32, FR-33, D4.

Qt-free, and that is not a formality here. Everything a strip *is* -- which
pixel covers which rows, how many entries of each severity fall in it, what
colour that comes out -- is decided in this module and tested headlessly on an
exact pixel buffer. The widget in `ui.strips` wraps the buffer in a `QImage`
and blits it, and that is all it does.

## Two strips, one structure

The timeline's vertical axis is wall-clock time; the minimap's is merged row
index. They differ *only* in how a pixel is turned into a time range:

* **Timeline** (SPEC 8.2): pixel `p` covers an equal share of the merged time
  span. The edges are `linspace(first, last, H + 1)` -- arithmetic.
* **Minimap** (SPEC 8.3): pixel `p` covers rows `[pN/H, (p+1)N/H)`, and those
  row boundaries are converted to times by `MergeIndex.row_times`, which reads
  the checkpoint table.

After that the two are identical: the time edges go to every source's
histogram, each returns its counts per range, and they are summed. So the
second strip costs one extra array of `H + 1` times and no second data
structure, which is what keeps risk R3 contained.

## Why the sources are summed per repaint rather than merged once

The obvious alternative is one merged histogram over the whole session, built
when the session changes. It would make a repaint a single range-sum instead of
`k` of them -- and it would need a merged bucket grid, which at eight sources
spanning different periods is up to 64k buckets of its own, rebuilt on every
skew nudge and every source toggle.

Summing `k` range-sums instead costs `k x 74 us` for 800 pixels, which at the
sixteen-source end of FR-9 is still ~1.2 ms against a 16 ms frame -- and the
answer is memoised anyway (see `_Memo`), because what actually moves between
repaints is the viewport box, not the histogram underneath it. The strip
re-aggregates when the session, the geometry or the row range changes, and on
a scroll it does not re-aggregate at all.

## Skew

A source's skew (D2) shifts its entries in merged time without touching its
index. The strips honour it by shifting the *query* rather than the data: the
edges handed to a source's histogram have its skew subtracted, so a source
corrected by an hour contributes to the pixel an hour away. Snapped to that
source's own bucket, like every other query -- a skew finer than a bucket moves
nothing, which is the correct answer for a strip whose pixels are minutes wide.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..index.histogram import CLASSES, Histogram
from ..index.severity import Severity
from . import palette

MIN_MARK_PX = 2
"""
Narrowest a severity class may be drawn when it is present at all.

Without this the strips would be honest and useless. One ERROR among ten
thousand INFO lines in the same pixel is 0.01% of that pixel's width, which
rounds to nothing -- and finding that one error is the single thing a user is
scanning an overview for (FR-11). So a class that has *any* entries in a pixel
is guaranteed two pixels of width, taken from the classes below it.

It is a deliberate distortion of the proportions, and it only ever runs upward:
the width a class gets is never *reduced* below its share.
"""

BAR_MIN_PX = 1
"""
Narrowest bar drawn for a pixel that has any entries at all.

Idle time has to read as blank (SPEC 8.2), so a pixel with no entries draws
nothing -- but a pixel with a handful against a neighbour with a million would
otherwise also draw nothing, and the strip would show a gap where there is
data. One pixel is the difference between "nothing happened" and "not much".
"""


@dataclass(frozen=True)
class Strip:
    """
    One strip's aggregation, `height` pixels tall.

    `counts[p]` is the entries per severity class in pixel `p`, summed over
    every enabled source. `times[p]` is the lower edge of pixel `p`'s time
    range, and `rows[p]` the lower edge of its row range -- the minimap fills
    both, the timeline only `times`, since a time is all it seeks on.
    """

    height: int
    counts: np.ndarray
    """`(height, 8)` int64."""

    times: np.ndarray
    """`(height + 1,)` int64 wall_us edges."""

    rows: np.ndarray | None = None
    """`(height + 1,)` int64 merged-row edges. None for the timeline."""

    @property
    def totals(self) -> np.ndarray:
        """Entries per pixel, all classes."""
        return self.counts.sum(axis=1)

    @property
    def peak(self) -> int:
        """The busiest pixel's entry count -- what density is scaled against."""
        return int(self.totals.max()) if self.counts.size else 0

    def top_severity(self) -> np.ndarray:
        """
        The highest severity class present in each pixel, or -1 for an empty one.

        What a one-pixel-wide rendering would have to fall back to, and what the
        tooltip reports.
        """
        if not self.counts.size:
            return np.zeros(0, dtype=np.int64)
        present = self.counts > 0
        idx = CLASSES - 1 - np.argmax(present[:, ::-1], axis=1)
        return np.where(present.any(axis=1), idx, -1).astype(np.int64)


class Overview:
    """
    Both strips over a session's sources.

    Constructed cheaply and often -- the session hands out a fresh one whenever
    the merge is rebuilt -- so it holds no derived state beyond the memo of the
    last aggregation at each height.
    """

    def __init__(self, session) -> None:
        self.session = session
        self._memo: dict[tuple, Strip] = {}

    # ------------------------------------------------------------------
    # the active set
    # ------------------------------------------------------------------

    def _contributors(self) -> list[tuple[Histogram, int]]:
        """
        `(histogram, skew_us)` for every enabled source that has one.

        A source with no recognisable timestamp format indexes fine and has no
        histogram (FR-19), and one that is switched off (FR-9) must not appear
        in an overview of what is being shown. Both simply do not contribute.
        """
        out = []
        for s in self.session.sources:
            if not s.meta.enabled:
                continue
            hist = s.index.hist
            if hist is not None and hist.nbuckets:
                out.append((hist, int(s.meta.skew_us)))
        return out

    @property
    def available(self) -> bool:
        """Whether there is anything to draw. The widgets hide themselves if not."""
        return bool(self._contributors())

    def _generation(self) -> tuple:
        """
        A cheap key for "the data behind the strips has not changed".

        Entry counts, skews and enabled flags, which between them cover every
        way the aggregation can move: a file opened or closed, a source toggled,
        a clock corrected, a follow append. Deliberately not a hash of the
        histograms -- that would cost more than the aggregation it is saving.
        """
        return (
            self.session.total_rows,
            tuple((id(s.index.hist), int(s.meta.skew_us), bool(s.meta.enabled))
                  for s in self.session.sources),
        )

    def invalidate(self) -> None:
        self._memo.clear()

    # ------------------------------------------------------------------
    # aggregation
    # ------------------------------------------------------------------

    def _aggregate(self, edges: np.ndarray) -> np.ndarray:
        """
        Counts per severity for each range in `edges`, summed over the sources.

        `edges` is in *merged* time. Each source is asked in its own time base,
        which is merged time less its skew (D2).

        The final edge is pushed one bucket past where the caller put it, and
        that is a fix rather than a flourish. A range is a half-open run of
        buckets `[lo, hi)`, so an edge landing *inside* the bucket that holds
        the last entry excludes that bucket -- and the last edge always does,
        because it is placed at the last entry's own timestamp. The first
        version of this reported 6,996 of 7,000 rows and looked right, which is
        exactly the kind of near-miss a strip cannot show you. One bucket is
        added per source, in its own bucket width, which is within the
        snapping error the whole structure already accepts.
        """
        out = np.zeros((max(0, edges.size - 1), CLASSES), dtype=np.int64)
        if out.shape[0] == 0:
            return out
        for hist, skew in self._contributors():
            e = np.asarray(edges, dtype=np.int64) - skew
            e = e.copy()
            e[-1] += hist.bucket_us
            out += hist.range_counts(e)
        return out

    def timeline(self, height: int) -> Strip:
        """
        The time-proportional strip (SPEC 8.2, FR-33).

        Idle gaps come out blank because they are blank: a pixel whose time
        range holds no entries has a zero row, and nothing is drawn for it.
        That is the property the whole strip exists for, and it is why the axis
        is time and not rows -- a row axis cannot show an absence, because there
        are no rows in it to be absent.
        """
        height = max(1, int(height))
        key = ("t", height, self._generation())
        got = self._memo.get(key)
        if got is not None:
            return got

        lo, hi = self.session.time_span()
        if hi <= lo:
            # A span of one instant -- a single entry, or a file whose every
            # entry shares a timestamp. `linspace` would give an array of
            # identical edges and every bucket would read as empty, so the span
            # is widened by a second to put everything in the first pixel.
            hi = lo + 1_000_000
        edges = np.linspace(lo, hi, height + 1).astype(np.int64)
        strip = Strip(height=height, counts=self._aggregate(edges), times=edges)
        self._memo[key] = strip
        return strip

    def minimap(self, height: int) -> Strip:
        """
        The row-proportional strip (SPEC 8.3, FR-32).

        Compressed overview of the whole merged stream: pixel `p` covers an
        equal share of the *rows*, so the strip is a scrollbar you can read.
        Scrolling is linear in it, which is exactly what the timeline is not.
        """
        height = max(1, int(height))
        key = ("m", height, self._generation())
        got = self._memo.get(key)
        if got is not None:
            return got

        total = self.session.total_rows
        merge = self.session.merge
        if not total or merge is None:
            edges = np.zeros(height + 1, dtype=np.int64)
            strip = Strip(height=height,
                          counts=np.zeros((height, CLASSES), dtype=np.int64),
                          times=edges, rows=edges.copy())
            self._memo[key] = strip
            return strip

        rows = np.linspace(0, total, height + 1).astype(np.int64)
        times = merge.row_times(rows)
        # `row_times` is monotonic in principle but interpolated in practice,
        # and a flat stretch -- thousands of rows sharing a timestamp -- gives
        # two identical edges and a pixel with an empty range. Forced upward so
        # each pixel's range is at least one microsecond wide, which keeps the
        # strip's rows in step with the row ranges they are labelled with.
        times = np.maximum.accumulate(times)
        strip = Strip(height=height, counts=self._aggregate(times),
                      times=times, rows=rows)
        self._memo[key] = strip
        return strip

    # ------------------------------------------------------------------
    # seeking (FR-32, FR-33)
    # ------------------------------------------------------------------

    def time_at_pixel(self, pixel: int, height: int) -> int:
        """Wall time a click at `pixel` on a timeline `height` tall means."""
        strip = self.timeline(height)
        p = max(0, min(int(pixel), height - 1))
        return int(strip.times[p])

    def row_at_pixel(self, pixel: int, height: int) -> int:
        """Merged row a click at `pixel` on a minimap `height` tall means."""
        total = self.session.total_rows
        if not total:
            return 0
        p = max(0, min(int(pixel), height - 1))
        return min(total - 1, int(self.minimap(height).rows[p]))

    def pixel_of_row(self, row: int, height: int) -> int:
        """Where a merged row sits on a minimap -- the viewport box (FR-32)."""
        total = self.session.total_rows
        if total <= 0:
            return 0
        return max(0, min(height - 1, int(row) * height // total))

    def pixel_of_time(self, wall_us: int, height: int) -> int:
        """Where a wall time sits on the timeline."""
        lo, hi = self.session.time_span()
        if hi <= lo:
            return 0
        frac = (int(wall_us) - lo) / (hi - lo)
        return max(0, min(height - 1, int(frac * height)))


# ---------------------------------------------------------------------------
# rendering (SPEC 8.2, 8.4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StripTheme:
    """
    The colours a strip is drawn in, as plain RGB.

    Here rather than in `ui.theme` because the renderer below is Qt-free and
    tested on its pixels: a test asserts that an ERROR bucket is drawn in the
    palette's error colour, which it can only do if the palette is reachable
    without a widget toolkit.
    """

    background: tuple[int, int, int] = (0x16, 0x18, 0x1d)
    severity: tuple[tuple[int, int, int], ...] = field(
        default_factory=lambda: tuple(
            _rgb(palette.SEVERITY_COLOURS[Severity(i)]) for i in range(CLASSES)))


def _rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def widths_for(counts: np.ndarray, width: int, *, peak: int | None = None,
               min_mark: int = MIN_MARK_PX) -> np.ndarray:
    """
    Per-pixel-row segment widths: `(rows, 8)` int64 summing to at most `width`.

    Two things are encoded at once, which is what makes one strip answer both
    "when was it busy" and "where are the errors":

    * **Density.** A row's total width is its entry count against the busiest
      row's, so an idle stretch is blank and a burst is a full-width band.
    * **Composition.** That width is divided between the severity classes in
      proportion to their counts, lowest class first, so the band's colour is
      the mix of what is in it.

    The minimum-width rule then runs over the result: any class with entries
    gets at least `min_mark` pixels, funded by the widest class below it. See
    `MIN_MARK_PX` for why a strip without it is unusable.
    """
    counts = np.asarray(counts, dtype=np.int64)
    rows = counts.shape[0]
    out = np.zeros((rows, CLASSES), dtype=np.int64)
    if rows == 0 or width <= 0:
        return out

    totals = counts.sum(axis=1)
    top = int(totals.max()) if peak is None else int(peak)
    if top <= 0:
        return out

    # Density first: how wide the whole band is.
    band = np.maximum(
        np.where(totals > 0, BAR_MIN_PX, 0),
        (totals * width // top).astype(np.int64),
    )
    np.clip(band, 0, width, out=band)

    # Then composition, by truncating division. The remainder is given to the
    # *highest* class present rather than spread around: it is a pixel or two,
    # and putting it on the class a reader is hunting for is strictly better
    # than putting it on INFO.
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(totals[:, None] > 0,
                         counts * band[:, None] // np.maximum(totals[:, None], 1),
                         0)
    out[:] = share
    busy = totals > 0
    if busy.any():
        slack = band - out.sum(axis=1)
        highest = np.where(counts > 0, np.arange(CLASSES), -1).max(axis=1)
        take = busy & (highest >= 0) & (slack > 0)
        out[take, highest[take]] += slack[take]

    if min_mark > 0:
        _apply_min_mark(out, counts, band, min_mark)
    return out


def _apply_min_mark(out: np.ndarray, counts: np.ndarray, band: np.ndarray,
                    min_mark: int) -> None:
    """
    Widen every present-but-invisible class to `min_mark`, in place.

    Worked from the highest class down, because that is the order of what
    matters: a CRITICAL must be visible even if it costs the INFO beside it,
    and never the reverse. The cost is taken from the widest class *below* the
    one being widened, so no class is ever starved to feed a quieter one.

    A loop over eight classes and a few hundred rows, done in NumPy per class
    rather than per row -- eight vector passes, not `8 x H` Python iterations.
    """
    rows = out.shape[0]
    if rows == 0:
        return
    index = np.arange(rows)
    for level in range(CLASSES - 1, 0, -1):
        short = (counts[:, level] > 0) & (out[:, level] < min_mark) & (band > 0)
        if not short.any():
            continue
        want = np.minimum(min_mark, band[short]) - out[short, level]
        below = out[short, :level]
        if below.size == 0:
            continue
        donor = np.argmax(below, axis=1)
        rows_short = index[short]
        have = below[np.arange(below.shape[0]), donor]
        give = np.minimum(want, have)
        out[rows_short, donor] -= give
        out[rows_short, level] += give


def render_rgba(strip: Strip, width: int, *, theme: StripTheme | None = None,
                reverse: bool = False) -> np.ndarray:
    """
    The strip as a `(height, width, 4)` RGBA buffer, ready to blit.

    A buffer rather than a sequence of draw calls, and that is the R3
    mitigation reaching the screen: an 800-pixel strip of eight classes is up
    to 6400 separate fills, which is a measurable slice of a frame, while this
    is two NumPy calls and one `drawImage`. SPEC 8 says a repaint should be a
    blit; this is what makes it one.

    `reverse` draws each band from the right edge instead of the left, which is
    what puts the two strips back to back with their quiet ends together.

    ## Why run-lengths and not a per-pixel comparison

    The obvious shape -- work out which class owns each pixel, then colour by
    it -- was the first version, and it cost **5.65 ms a strip** at 828 pixels.
    Two strips at 11.3 ms left 4.7 ms of a 16 ms frame for the view beside
    them, which is risk R3 arriving by the back door: the aggregation SPEC 8
    worries about was 0.15 ms, and all of the cost was in turning it into
    pixels.

    Two changes, measured, take it to **0.84 ms**:

    * A row is eight contiguous runs plus a background run, and `np.repeat`
      builds exactly that in one call -- against eight broadcast comparisons
      over the whole buffer, which is 1.2 ms of the old figure.
    * `np.take(..., out=buf)` writes the colours straight into the buffer.
      Indexing a three-channel *view* of it instead allocates the result and
      then copies it across a stride of four, which was 1.5 ms of its own.
    """
    theme = theme or StripTheme()
    height = strip.height
    buf = np.empty((height, max(1, width), 4), dtype=np.uint8)
    if width <= 0 or not strip.counts.size:
        buf[..., 0:3] = theme.background
        buf[..., 3] = 255
        return buf

    widths = widths_for(strip.counts, width)

    # Nine runs a row: one per severity class, then the background that fills
    # out the rest of the width. `counts` may be zero for any of them, which is
    # what makes a run-length encoding right here -- an absent class is a run
    # of length nought and needs no special case.
    counts = np.empty((height, CLASSES + 1), dtype=np.int64)
    counts[:, :CLASSES] = widths
    counts[:, CLASSES] = width - widths.sum(axis=1)

    runs = np.tile(_RUN_CLASSES, height)
    if reverse:
        runs = np.tile(_RUN_CLASSES_REVERSED, height)
        counts = counts[:, ::-1]
    classes = np.repeat(runs, counts.ravel()).reshape(height, width)
    np.take(_lut(theme), classes, axis=0, out=buf)
    return buf


_RUN_CLASSES = np.arange(CLASSES + 1, dtype=np.uint8)
"""The eight severity classes then the background, as palette rows."""

_RUN_CLASSES_REVERSED = _RUN_CLASSES[::-1].copy()
"""
The same runs right to left, for a strip whose band grows from its right edge.

Reversing the run *order* rather than flipping the finished buffer: a flip is
another full-size copy, and the whole point of the function above is that there
is exactly one write per pixel.
"""


def _lut(theme: StripTheme) -> np.ndarray:
    """
    `(9, 4)` RGBA palette: the eight severity colours, then the background.

    Built per call, which is nine rows of four bytes -- against caching it on
    the theme, which would need the theme to stop being frozen.
    """
    lut = np.empty((CLASSES + 1, 4), dtype=np.uint8)
    lut[:CLASSES, 0:3] = np.array(theme.severity, dtype=np.uint8)
    lut[CLASSES, 0:3] = theme.background
    lut[:, 3] = 255
    return lut


def overlay_marks(buf: np.ndarray, pixels: np.ndarray, colour: tuple[int, int, int],
                  *, span: int = 1) -> np.ndarray:
    """
    Draw sparse marks over a rendered strip, in place (SPEC 8.4).

    Search hits (FR-44) and bookmarks (FR-37) are sparse *row* lists, converted
    to pixel rows by the caller and drawn over both strips. The data that fills
    them arrives with M7; this is the drawing path, and it is here now because
    the strips are what it draws on and a separate overlay pass later would
    mean re-deriving the geometry it needs.

    Marks are drawn at the left edge and over whatever is beneath, because a
    hit that is hidden by a dense band is a hit the user cannot find.
    """
    if buf.size == 0 or np.size(pixels) == 0:
        return buf
    rows = np.clip(np.asarray(pixels, dtype=np.int64), 0, buf.shape[0] - 1)
    w = max(1, min(int(span), buf.shape[1]))
    buf[rows[:, None], np.arange(w)[None, :], 0:3] = colour
    return buf
