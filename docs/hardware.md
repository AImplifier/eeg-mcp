# Supported hardware

!!! info "Generated, not transcribed"
    This page is produced by `testing/gen_hardware_docs.py` from the installed BrainFlow. Regenerate it after a BrainFlow upgrade rather than editing it by hand.

## EEG acquisition hardware

eeg-mcp acquires through [BrainFlow](https://brainflow.readthedocs.io/), so it supports **66 board identifiers** across the vendors below. Pass the board by name to `start_stream` — `board="cyton"`, `board="muse_2"` — or by numeric id.

`sfreq` and channel counts come from BrainFlow's own board description. A blank means the board has no static description (usually because it needs a `master_board`).

### OpenBCI

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `CYTON_BOARD` | 0 | 250 Hz | 8 | `serial_port` |
| `GANGLION_BOARD` | 1 | 200 Hz | 4 | `serial_port` |
| `CYTON_DAISY_BOARD` | 2 | 125 Hz | 16 | `serial_port` |
| `GANGLION_WIFI_BOARD` | 4 | 1600 Hz | 4 | `ip_address`, `ip_port` |
| `CYTON_WIFI_BOARD` | 5 | 1000 Hz | 8 | `ip_address`, `ip_port` |
| `CYTON_DAISY_WIFI_BOARD` | 6 | 1000 Hz | 16 | `ip_address`, `ip_port` |
| `GANGLION_NATIVE_BOARD` | 46 | 200 Hz | 4 | `serial_port` |

### InteraXon Muse

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `MUSE_S_BLED_BOARD` | 21 | 256 Hz | 4 | `serial_port` (BLED112) |
| `MUSE_2_BLED_BOARD` | 22 | 256 Hz | 4 | `serial_port` (BLED112) |
| `MUSE_2_BOARD` | 38 | 256 Hz | 4 | `mac_address` or `serial_number` (BLE) |
| `MUSE_S_BOARD` | 39 | 256 Hz | 4 | `mac_address` or `serial_number` (BLE) |
| `MUSE_2016_BOARD` | 41 | 256 Hz | 4 | `mac_address` or `serial_number` (BLE) |
| `MUSE_2016_BLED_BOARD` | 42 | 256 Hz | 4 | `serial_port` (BLED112) |
| `MUSE_S_ATHENA_BOARD` | 67 | 256 Hz | 4 | `mac_address` or `serial_number` (BLE) |

### ANT Neuro

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `ANT_NEURO_EE_410_BOARD` | 24 | 2000 Hz | — | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_411_BOARD` | 25 | 2000 Hz | 8 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_430_BOARD` | 26 | 512 Hz | 8 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_211_BOARD` | 27 | 2000 Hz | 64 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_212_BOARD` | 28 | 2000 Hz | 32 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_213_BOARD` | 29 | 2000 Hz | 16 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_214_BOARD` | 30 | 2000 Hz | 32 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_215_BOARD` | 31 | 2000 Hz | 64 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_221_BOARD` | 32 | 16000 Hz | 16 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_222_BOARD` | 33 | 16000 Hz | 32 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_223_BOARD` | 34 | 16000 Hz | 32 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_224_BOARD` | 35 | 16000 Hz | 64 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_225_BOARD` | 36 | 16000 Hz | 64 | `ip_address` or auto-discovery |
| `ANT_NEURO_EE_511_BOARD` | 51 | 4096 Hz | 24 | `ip_address` or auto-discovery |

### Mentalab Explore

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `EXPLORE_4_CHAN_BOARD` | 44 | 250 Hz | 4 | `ip_address` or auto-discovery |
| `EXPLORE_8_CHAN_BOARD` | 45 | 250 Hz | 8 | `ip_address` or auto-discovery |
| `EXPLORE_PLUS_8_CHAN_BOARD` | 54 | 250 Hz | 8 | `ip_address` or auto-discovery |
| `EXPLORE_PLUS_32_CHAN_BOARD` | 55 | 250 Hz | 32 | `ip_address` or auto-discovery |

### BrainBit / Neurosity Callibri

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `BRAINBIT_BOARD` | 7 | 250 Hz | 4 | `mac_address` or `serial_number` (BLE) |
| `CALLIBRI_EEG_BOARD` | 9 | 250 Hz | 1 | `mac_address` or `serial_number` (BLE) |
| `CALLIBRI_EMG_BOARD` | 10 | 1000 Hz | — | `mac_address` or `serial_number` (BLE) |
| `CALLIBRI_ECG_BOARD` | 11 | 125 Hz | — | `mac_address` or `serial_number` (BLE) |
| `BRAINBIT_BLED_BOARD` | 18 | 250 Hz | 4 | `mac_address` or `serial_number` (BLE) |

### Neurosity

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `NOTION_1_BOARD` | 13 | 250 Hz | 8 | `mac_address` or `serial_number` (BLE) |
| `NOTION_2_BOARD` | 14 | 250 Hz | 8 | `mac_address` or `serial_number` (BLE) |
| `CROWN_BOARD` | 23 | 256 Hz | 8 | `mac_address` or `serial_number` (BLE) |

### Enophone

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `ENOPHONE_BOARD` | 37 | 250 Hz | 4 | `mac_address` or `serial_number` (BLE) |

### OYMotion gForce

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `GFORCE_PRO_BOARD` | 16 | 500 Hz | — | `mac_address` or `serial_number` (BLE) |
| `GFORCE_DUAL_BOARD` | 19 | 500 Hz | — | `mac_address` or `serial_number` (BLE) |

### g.tec Unicorn

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `UNICORN_BOARD` | 8 | 250 Hz | 8 | `ip_address` or auto-discovery |

### FreeEEG

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `FREEEEG32_BOARD` | 17 | 512 Hz | 32 | `serial_port` |
| `FREEEEG128_BOARD` | 52 | 256 Hz | 128 | `serial_port` |

### PiEEG

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `PIEEG_BOARD` | 56 | 250 Hz | 8 | `serial_port` |

### Synchroni

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `SYNCHRONI_TRIO_3_CHANNELS_BOARD` | 58 | 250 Hz | 2 | `serial_port` |
| `SYNCHRONI_OCTO_8_CHANNELS_BOARD` | 59 | 250 Hz | 7 | `serial_port` |
| `SYNCHRONI_PENTO_8_CHANNELS_BOARD` | 61 | 250 Hz | 8 | `serial_port` |
| `SYNCHRONI_UNO_1_CHANNELS_BOARD` | 62 | 250 Hz | 1 | `serial_port` |

### OpenBCI OB-series

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `OB5000_8_CHANNELS_BOARD` | 60 | 250 Hz | 8 | `serial_port` |
| `OB3000_24_CHANNELS_BOARD` | 63 | 500 Hz | 23 | `serial_port` |

### NeuroPawn

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `NEUROPAWN_KNIGHT_BOARD` | 57 | 125 Hz | 8 | `serial_port` |
| `NEUROPAWN_KNIGHT_BOARD_IMU` | 66 | 125 Hz | 8 | `serial_port` |

### BrainAlive

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `BRAINALIVE_BOARD` | 40 | 250 Hz | 8 | `mac_address` or `serial_number` (BLE) |

### AAVAA

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `AAVAA_V3_BOARD` | 53 | 50 Hz | 8 | `mac_address` or `serial_number` (BLE) |

### OpenBCI Galea

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `GALEA_BOARD` | 3 | 250 Hz | 16 | `ip_address` or auto-discovery |

### IronBCI

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `IRONBCI_32_BOARD` | 65 | 512 Hz | 32 | `serial_port` |

### NTL

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `NTL_WIFI_BOARD` | 50 | 250 Hz | 8 | `ip_address`, `ip_port` |

### BioListener

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `BIOLISTENER_BOARD` | 64 | 500 Hz | 8 | `serial_port` |

### EmotiBit

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `EMOTIBIT_BOARD` | 47 | 25 Hz | — | `ip_address` or auto-discovery |

### No hardware required

| Board name | id | Sampling rate | EEG ch | Connection via |
|---|---:|---:|---:|---|
| `NO_BOARD` | -100 | — | — | none |
| `PLAYBACK_FILE_BOARD` | -3 | — | — | `file`, `master_board` |
| `STREAMING_BOARD` | -2 | — | — | `ip_address`, `ip_port`, `master_board` |
| `SYNTHETIC_BOARD` | -1 | 250 Hz | 16 | none |

### Boards that need no hardware

Three entries above are worth calling out, because they are how you develop and test without a rig:

- **`SYNTHETIC_BOARD`** (id -1) — generates a signal immediately. The default for `start_stream`.
- **`PLAYBACK_FILE_BOARD`** (id -3) — replays a BrainFlow CSV at true rate. Set `file` and `master_board`. For other formats, or if you need speed control and seeking, use `start_replay` instead.
- **`STREAMING_BOARD`** (id -2) — attaches to another BrainFlow process streaming over the network.

## Recording formats for replay

`start_replay` reads more than BrainFlow's own CSV, because MNE does the file handling:

| Format | Extension | Events source |
|---|---|---|
| European Data Format | `.edf` | annotations + trigger channel |
| BioSemi | `.bdf` | annotations + trigger channel |
| General Data Format | `.gdf` | annotations + trigger channel |
| EEGLAB | `.set` | annotations |
| MNE / Elekta / Neuromag | `.fif` | annotations + STI channels |
| BrainVision | `.vhdr` | marker file |
| Nihon Kohden | `.eeg` | annotations |
| Persyst | `.lay` | annotations |
| BrainFlow CSV | `.csv` | marker channel (needs `board=`) |

Anything else [MNE can read](https://mne.tools/stable/reading_raw_data.html) will also load; the table lists what is routinely exercised.

## Stimulation hardware

!!! danger "Read this before connecting a stimulator"
    The hardware backends are **generic transports driven by command templates you supply from your device's manual — not vendor drivers**, and none has been validated against a specific commercial stimulator. Shipping a plausible-looking driver that had never been tested against the real device would be worse than shipping none, because it would fail silently while connected to something pointed at a person's head.

    Use only under a protocol approved by your ethics board, on a rig whose device-level interlocks are intact, with a trained operator present. This is not a medical device.

### What each backend can talk to

| Backend | Hardware? | Transport | Typically used for |
|---|---|---|---|
| `log` | no | none | Dry runs. Accepts everything, emits nothing. |
| `lsl` | no | LSL marker outlet | PsychoPy, OpenViBE, LabRecorder, any LSL consumer |
| `brainflow_marker` | no | board marker channel | Sample-aligned marking inside the recording itself |
| `serial_ttl` | **yes** | serial byte | Trigger boxes, stimulator trigger inputs, TTL adapters |
| `template_serial` | **yes** | ASCII serial | Any device with a documented text protocol |
| `tms` | **yes** | ASCII serial + trigger | TMS stimulators; intensity in %MSO |
| `tes` | **yes** | ASCII serial | tDCS/tACS/tRNS; intensity in mA, default ceiling 4 mA |

### Devices these transports are designed to reach

Listed as *transport compatibility*, not certified support. If your device accepts TTL triggers on a serial line or exposes an ASCII command protocol, it fits one of the backends above.

| Class | Examples | Backend | How it is driven |
|---|---|---|---|
| TMS stimulators | Magstim, MagVenture, Deymed, Nexstim | `tms` | Intensity set over serial (device protocol), pulse fired by trigger |
| tES stimulators | neuroConn DC-Stimulator, Soterix, Neuroelectrics Starstim, BrainStim | `tes` | Current and ramp over serial; many also accept a TTL start |
| Trigger boxes | BrainProducts TriggerBox, Cedrus StimTracker, LabJack, custom Arduino | `serial_ttl` | One byte per event code |
| Presentation software | PsychoPy, OpenViBE, E-Prime, Presentation | `lsl` | Marker stream over the network |

Vendor names identify the device class the transport targets. They do **not** indicate testing, endorsement, or any relationship with those manufacturers. You supply the command strings from your manual:

```python
attach_stim_backend(session_id="s1", backend="tms", options={
    "port": "COM3",
    "templates": {
        "fire": "TRIG\r\n",
        "set_intensity": "AMP {intensity:.0f}\r\n",
    },
})
```

### Safety gates on every hardware backend

1. **Config** — refuses to open unless the server was started with `EEG_MCP_ALLOW_HARDWARE_STIM=1`.
2. **Arming** — `arm_stim` grants a bounded window that expires, so a stalled agent cannot resume and fire minutes later.
3. **Limits** — intensity, duration and inter-stimulus interval are clamped against configured ceilings; violations raise rather than saturate.

None of this replaces the interlocks on the device itself.
