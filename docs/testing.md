# Testing & Verification

Three suites, all driving the real server through FastMCP's in-memory client —
no mocks of the acquisition, DSP, or stimulation paths.

```bash
python testing/verify.py                    # 57 checks
python testing/persona_bci_researcher.py    # 24 checks
python testing/persona_clinician.py         # 45 checks
```

Each prints `PASS`/`FAIL` per check and exits non-zero on any failure.

---

## `verify.py` — correctness

Covers the numeric core and every tool group.

### The assertion the design rests on

```text
PASS  chunked filtering == whole-signal filtering
PASS  stateless per-chunk filtering is measurably wrong (control)
```

Filtering a signal in 37-sample chunks through the stateful chain matches
filtering it whole to **1e-9**. The second line is the control: the same test
with fresh filter state per chunk is wrong by more than `1e-3`. Without it, the
first assertion would pass trivially on any implementation.

### Also checked

- Ring buffer wrap, absolute index continuity, oversized-chunk handling
- Synthetic board streaming, throughput ratio, filter installation
- Band power relative values summing to 1.0, PSD peak detection
- Marker round-trip through the board's marker channel
- Epoch extraction around a dispatched stimulation
- **Hardware stimulation refused while `EEG_MCP_ALLOW_HARDWARE_STIM` is unset**
- Replay of a real EDF: clock advance, speed, annotations re-emitted, pause,
  seek
- Error messages naming the offending value

---

## `persona_bci_researcher.py` — the engineer's questions

Uses `make_synthetic.py`, which plants ground truth deliberately: a 10 Hz sine
on O1/O2 only, and a one-sample 500 µV spike on Fp1 at **the exact onset of
every annotation**.

| Question | What it proves |
|---|---|
| Develop offline, deploy live? | The same tool sequence runs on replay and hardware |
| Do the numbers mean what I think? | Alpha localises to O1/O2, not Fz |
| Are markers sample-aligned? | **The planted spike lands at t = 0 ± 0 ms** |
| What is my latency budget? | Measured: 13 ms filter + 50 ms poll + 8 ms dispatch |
| How much train jitter? | 100.02 ms mean on a 100 ms request, no drift |
| Concurrent sessions? | Three run independently, throughput unaffected |
| Are errors actionable? | Six failure modes each name the fix |

The spike assertion is the important one: it covers annotation timing, the
replay clock, ring-buffer absolute indexing, and epoch extraction in a single
check. If any link were off by even a few samples, it would fail.

---

## `persona_clinician.py` — the reviewer's questions

| Question | What it proves |
|---|---|
| Can I open and look at a recording? | Traces render, every channel labelled |
| Is the signal good? | Per-electrode flags, verdict in plain words |
| Can I watch it live? | Monitor serves, snapshot API returns data |
| Where is the posterior rhythm? | 9.77 Hz on O1/O2, dominant band `alpha` |
| Can I annotate as I go? | Notes share the timeline with file annotations |
| Can I keep a report? | ~120 KB, self-contained, all sections present |
| Are errors plain? | No tracebacks or internals leaked |

### Two structural checks worth copying

**Self-containment.** Output is scanned for any `src`/`href` pointing at
`http(s)://` and any external `@import`. A plot that renders blank because a CDN
was unreachable is worse than no plot.

**Figures are sane.** Every SVG is parsed as XML, and every plotted point is
checked against its `viewBox`. This is what catches the classic rendering
regression — a scaling bug that silently pushes traces outside the visible area,
which no string-matching assertion would ever notice.

```text
PASS  all report figures render inside their viewBox  [3 figures, 7912 points all in bounds]
```

**Token enforcement.** The monitor's API is requested with no token and a wrong
token; both must return HTTP 403.

---

## Generating test data

```bash
python testing/make_synthetic.py [outdir]
```

Writes `synthetic_raw.fif`: 8 channels, 250 Hz, 40 s, 19 annotations, 10 Hz
alpha on O1/O2, a 500 µV spike on Fp1 at every annotation onset.

## Regenerating the hardware tables

```bash
python testing/gen_hardware_docs.py
```

Rewrites `docs/hardware.md` from the installed BrainFlow. Run it after a
BrainFlow upgrade rather than editing the table by hand.

---

## What is *not* covered

Stated plainly, because a test suite that overclaims is worse than a small one.

**Real stimulation hardware has never been exercised.** Only the `log` backend
and the hardware-*refusal* path are tested. The LSL, serial/TTL, TMS and tES
transports have not been run against a physical device — see
[Safety](safety.md#no-fabricated-vendor-drivers) for why they are built as
configurable transports rather than vendor drivers.

**Real amplifiers have not been exercised**, only the synthetic board and file
replay. Board-specific behaviour — dropouts, driver quirks, reconnection — is
untested.

**Long-run stability** beyond a few minutes is untested. Ring buffer wrap is
covered as a unit test, but not multi-hour sessions.

Contributions with access to real hardware are welcome.
