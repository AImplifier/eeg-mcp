# Examples

Four end-to-end workflows. Each is a transcript you can follow with an agent,
with the reasoning behind each step rather than just the calls.

Start with the [tutorial](tutorial-first-realtime-session.md) if you have not
run a session yet — it needs no hardware.

<div class="grid cards" markdown>

- **[Replay-Driven Development](replay-driven-development.md)**

    Build and debug a pipeline against a real recording playing at true rate,
    then move it to hardware unchanged. The safest way to develop anything that
    will eventually touch a subject.

    *Tools:* `inspect_recording`, `start_replay`, `replay_control`,
    `get_epoch_around_event`

- **[Closed-Loop Neurofeedback](closed-loop-neurofeedback.md)**

    Turn a band-power feature into a stimulation trigger, with the latency
    budget measured rather than assumed.

    *Tools:* `set_filters`, `get_band_power`, `send_stim_event`, `stream_status`

- **[Live Clinical Review](clinical-live-review.md)**

    Open a recording, watch it, judge the signal, annotate what you see, and
    keep a report that opens anywhere.

    *Tools:* `start_monitor`, `check_signal_quality`, `plot_spectrum`,
    `export_report`

- **[Stimulation Protocols](stimulation-protocols.md)**

    TMS and tES through the safety gates: rehearse on `log`, configure templates
    from your device manual, arm, deliver, disarm.

    *Tools:* `list_stim_backends`, `attach_stim_backend`, `arm_stim`,
    `send_stim_train`

</div>

## A note on how these read

Lines like

```text
start_stream(session_id="demo", board="synthetic")
```

are the tool calls your agent makes. You do not type them — you ask for the
outcome in plain language and the agent chooses the calls. They are written out
here so you can see exactly what happened and reproduce it.
