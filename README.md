# TAK Santa Tracker (CoT Broadcaster)

![Screenshot_20251223_231428_ATAK](https://github.com/user-attachments/assets/badae6ef-8af0-4ffd-b0a5-5e8a2e9406f2)

Broadcast Santa’s position (and next destination) into TAK Server or Multicast to TAK Clients as Cursor-on-Target (CoT).

This script pulls Santa’s live track + route metadata from a public Santa Tracker API, then emits CoT over:

- **UDP Multicast** (for local TAK clients)
- **TCP** (unencrypted CoT stream)
- **TLS** (CoT over TLS; supports client certs for mTLS)

---

## What it sends

Each update interval, the script sends:

1. **Santa marker** (`type="a-n-A-C"`, `uid="SANTA"`)
   - Remarks include:
     - `Presents Delivered: ...`
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
    -  `requests`>=2.31.0 

Install:

```bash
python3 -m pip install requests
```

## Quick start

### UDP Multicast (typical ATAK LAN)
```bash
python3 santa_tracker.py --mode udp-mcast --mcast 239.2.3.1 --port 6969 --interval 10
```

### TCP (unencrypted CoT stream to TAK Server)
```bash
python3 santa_tracker.py --mode tcp --host {takserver ip or hostname} --port 8087 --interval 10
```

### TLS (mTLS) using a PKCS#12 client cert (.p12/.pfx)
```bash
python3 santa_tracker.py --mode tls --host {takserver ip or hostname} --port 8089 \
  --p12file /path/to/client.p12 --p12pass 'cert_password' \
  --interval 10
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
  --iface 192.168.1.10 \
  --bind 192.168.1.10 \
  --interval 10
```

#### TCP (unencrypted CoT stream)
```bash
python3 santa_tracker.py \
  --mode tcp \
  --host {takserver ip or hostname} \
  --port 8087 \
  --interval 10
```

#### Mutual TLS (client certificate authentication):
##### TLS with PKCS#12 (.p12 / .pfx) client certificate
```bash
python3 santa_tracker.py \
  --mode tls \
  --host {takserver ip or hostname} \
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
  --host {takserver ip or hostname} \
  --port 8089 \
  --cafile /path/to/ca.pem \
  --certfile /path/to/client.pem \
  --keyfile /path/to/client.key \
  --interval 10
```

## Verbosity

Default output is verbose. To reduce console output:
```bash
python3 santa_tracker.py --mode udp-mcast --quiet
```
## Graceful shutdown

Press Ctrl+C to stop the script.

On exit, the script automatically sends a forced delete CoT (t-x-d-d) to remove the Range & Bearing line from TAK.

