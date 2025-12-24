# TAK Santa Tracker (CoT Broadcaster)

![Screenshot_20251223_231428_ATAK](https://github.com/user-attachments/assets/badae6ef-8af0-4ffd-b0a5-5e8a2e9406f2)

Broadcast Santa’s position (and next destination) into TAK Server of Multicast to TAK Clients as Cursor-on-Target (CoT).

This script pulls Santa’s live track + route metadata from a public Santa Tracker API, then emits CoT over:

- **UDP Multicast** (great for local LAN + ATAK clients)
- **TCP** (unencrypted CoT stream)
- **TLS** (CoT over TLS; supports client certs for mTLS)

It also supports an **offline simulation mode** (useful outside of December or when the API isn’t active).

---

## What it sends

Each update interval, the script sends:

1. **Santa marker** (`type="a-n-A-C"`, `uid="SANTA"`)
   - Remarks include:
     - `Present Delivered: ...`
     - `Next: <destination>`

2. **Next destination marker** (`type="a-u-G"`)
   - **UID is the destination raw id** (e.g. `"new_york"`), so it stays consistent while the script runs.

3. **Range & Bearing line** (`type="u-rb-a"`, persistent UID generated at startup)
   - Links from Santa → next destination
   - On Ctrl+C, the script sends a **forced delete** CoT (`type="t-x-d-d"`) to remove the R&B object.

---

## Requirements

- Python **3.10+** recommended (script uses modern typing)
- Packages:
  - `requests` (**required**)
  - `geopy` (**optional**; improves destination geocoding)

Install:

```bash
python3 -m pip install requests
python3 -m pip install geopy   # optional
```
## Usage

The script can be run either **interactively** (guided prompts) or fully via **command-line arguments**.  
It broadcasts Cursor-on-Target (CoT) messages continuously at a configurable interval.

---

### Interactive mode (guided setup)

If no `--mode` is specified, the script will prompt you for output settings:

```bash
python3 santa_tracker.py
```

You will be asked to choose:
- Output mode:
  - UDP Multicast
  - TCP (unencrypted)
  - TLS (encrypted)
- Update interval (seconds)
- Network details (host, port, bind IP, etc.)

This is useful for quick testing or one-off runs.

### Command-line mode (recommended)

**Command-line usage is preferred for repeatable setups, scripts, or services.**

#### UDP Multicast (typical ATAK LAN setup)
```bash
python3 santa_tracker.py \
  --mode udp-mcast \
  --mcast 239.2.3.1 \
  --port 6969 \
  --interval 10
```

#### Optional interface and bind control:
```bash
python3 santa_tracker.py \
  --mode udp-mcast \
  --iface 192.168.68.10 \
  --bind 192.168.68.10 \
  --interval 10
```

#### TCP (unencrypted CoT stream)
```bash
python3 santa_tracker.py \
  --mode tcp \
  --host 192.168.68.100 \
  --port 8087 \
  --interval 10
```

#### Mutual TLS (client certificate authentication):
##### TLS with PKCS#12 (.p12 / .pfx) client certificate
```bash
python3 santa_tracker.py \
  --mode tls \
  --host tak.example.com \
  --port 8089 \
  --cafile /path/to/ca.pem \
  --p12file /path/to/client.p12 \
  --p12pass your_password \
  --interval 10
```

##### TLS with PEM certificates
```bash
python3 santa_tracker.py \
  --mode tls \
  --host tak.example.com \
  --port 8089 \
  --cafile /path/to/ca.pem \
  --certfile /path/to/client.pem \
  --keyfile /path/to/client.key \
  --interval 10
```

#### Testing only (disable certificate verification):
```bash
python3 santa_tracker.py \
  --mode tls \
  --host tak.example.com \
  --port 8089 \
  --insecure
```
## Simulation mode (works year-round)

Outside of December (or when live API data is unavailable), you can simulate Santa’s movement along the real route.
```bash
python3 santa_tracker.py \
  --mode udp-mcast \
  --interval 5 \
  --simulate
```

### Control speed and start location:
```bash
python3 santa_tracker.py \
  --mode udp-mcast \
  --interval 5 \
  --simulate \
  --sim-speed 250 \
  --sim-start-lat 90.0 \
  --sim-start-lon 0.0
```

-- `sim-speed` is in meters per second

Default start point is the North Pole

## Verbosity

Default output is verbose. To reduce console output:
```bash
python3 santa_tracker.py --mode udp-mcast --quiet
```
## Graceful shutdown

Press Ctrl+C to stop the script.

On exit, the script automatically sends a forced delete CoT (t-x-d-d) to remove the Range & Bearing line from TAK.

## Optional offline destination lookup (recommended)

If online geocoding isn’t available (or you want deterministic lookups), place this file next to the script:

- `ne_50m_populated_places.csv`

The script will automatically use it as an offline fallback for destination name → lat/lon resolution.
in the same folder as the script.

