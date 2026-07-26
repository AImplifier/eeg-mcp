"""eeg-mcp: a Model Context Protocol server for real-time EEG.

Three capabilities, one session model:

* **Stream** live EEG from any BrainFlow-supported board, with events.
* **Replay** an existing recording at wall-clock rate, re-emitting its
  annotations as events, so an agent pipeline can be developed and tested
  against recorded data before it ever touches hardware.
* **Stimulate** by dispatching events to software (LSL) and hardware
  (serial/TTL, TMS, tES) targets through one backend-agnostic contract.
"""

__version__ = "0.1.0"
