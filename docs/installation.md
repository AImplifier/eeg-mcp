# Installation

Zero to a live signal in about five minutes, no hardware needed.

## 1. Create an environment

eeg-mcp needs Python 3.10 or newer.

=== "conda"

    ```bash
    conda create -n eeg-mcp python=3.11 -y
    conda activate eeg-mcp
    ```

=== "venv"

    ```bash
    python -m venv .venv
    source .venv/bin/activate      # Windows: .venv\Scripts\activate
    ```

## 2. Install

```bash
pip install eeg-mcp
```

Optional extras, only if you need them:

```bash
pip install "eeg-mcp[lsl]"       # LSL marker outlets (PsychoPy, OpenViBE, ...)
pip install "eeg-mcp[serial]"    # serial/TTL trigger delivery to hardware
pip install "eeg-mcp[lsl,serial]"
```

!!! note "Why setuptools is a runtime dependency"
    BrainFlow's native-library loader calls `importlib.resources.files()` on a
    *module* rather than a package, which raises on Python 3.11+, and it then
    falls back to `pkg_resources`. Without setuptools the board DLL never loads.
    This is pinned to `setuptools<81` because `pkg_resources` is removed in 81.

## 3. Check it works

```bash
python -c "from eeg_mcp.server import mcp; print('ok')"
```

Then confirm a board actually opens — the synthetic board needs no hardware:

```bash
python -c "
from eeg_mcp.sources.brainflow_source import BrainFlowSource
import time
s = BrainFlowSource(board='synthetic'); s.start(); time.sleep(1)
c = s.poll(); s.stop()
print(f'{c.n_samples} samples on {s.info.n_channels} channels @ {s.info.sfreq} Hz')
"
```

## 4. Register with an MCP client

The server speaks **stdio**. Point your client at the interpreter *inside the
environment you just made* — an absolute path, not a bare `python`.

=== "Claude Code"

    ```bash
    claude mcp add eeg-realtime -- /path/to/envs/eeg-mcp/bin/python -m eeg_mcp
    ```

=== "Claude Desktop"

    Edit `claude_desktop_config.json`:

    ```json
    {
      "mcpServers": {
        "eeg-realtime": {
          "command": "/path/to/envs/eeg-mcp/bin/python",
          "args": ["-m", "eeg_mcp"]
        }
      }
    }
    ```

    On Windows the command looks like
    `C:\\Users\\you\\anaconda3\\envs\\eeg-mcp\\python.exe`.

=== "Codex CLI"

    In `~/.codex/config.toml`:

    ```toml
    [mcp_servers.eeg-realtime]
    command = "/path/to/envs/eeg-mcp/bin/python"
    args = ["-m", "eeg_mcp"]
    ```

=== "Any client"

    ```bash
    python -m eeg_mcp        # stdio transport
    eeg-mcp                  # same thing, via the console script
    ```

### Passing configuration

Environment variables go in the same block. This is also the **only** place
hardware stimulation can be enabled — an agent cannot turn it on for itself:

```json
{
  "mcpServers": {
    "eeg-realtime": {
      "command": "/path/to/envs/eeg-mcp/bin/python",
      "args": ["-m", "eeg_mcp"],
      "env": {
        "EEG_MCP_RECORDINGS_DIR": "/data/eeg",
        "EEG_MCP_BUFFER_SEC": "120"
      }
    }
  }
}
```

See [Configuration](configuration.md) for every variable.

## 5. First call

Ask your agent:

> List the EEG boards you can use, then start a synthetic stream and tell me the
> dominant frequency band.

It should call `list_boards` → `start_stream` → `set_filters` → `get_band_power`.

→ Walk through it properly in the
[first real-time session tutorial](examples/tutorial-first-realtime-session.md).

## Installing from source

```bash
git clone https://github.com/AImplifier/eeg-mcp
cd eeg-mcp
pip install -e ".[lsl,serial,dev]"
python testing/verify.py
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named 'pkg_resources'` | setuptools missing | `pip install "setuptools<81"` |
| `BOARD_NOT_READY_ERROR` | Device busy or not connected | Close other software holding the board; check `serial_port` |
| `UNABLE_TO_OPEN_PORT_ERROR` | Wrong port or no permission | Check the port name; on Linux add yourself to `dialout` |
| Board opens, no samples | Stream not started, or buffer still filling | Allow ~1 s, then check `stream_status` |
| `throughput_ratio` below 1.0 | Producer not keeping up | Raise `EEG_MCP_POLL_INTERVAL_SEC`, or reduce channels |
| Hardware stim refuses to open | Safety gate, working as designed | Set `EEG_MCP_ALLOW_HARDWARE_STIM=1` in the **server** env |
| Monitor URL not loading | Token missing | Use the full URL including `?t=…` from `start_monitor` |
| Muse not found on Windows (`Failed to find Muse Device`) | Upstream bug in BrainFlow's SimpleBLE bindings ([simpleble/simpleble#31](https://github.com/simpleble/simpleble/issues/31)) | Run `muselsl stream` (uses `bleak`, unaffected) in a separate terminal, then `start_stream(board="lsl")` — see [Acquiring via LSL](hardware.md#acquiring-via-lsl-instead-of-brainflow) |
