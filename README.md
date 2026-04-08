# HF-256 MSA

**Multi-session encrypted radio BBS — HF, VHF, and internet — in a Raspberry Pi appliance.**

HF-256 MSA (Multi-Session Architecture) is a self-contained communication system that lets amateur radio operators exchange encrypted messages, files, and live chat across HF radio, VHF radio, and internet links — simultaneously. It runs entirely on a Raspberry Pi and is operated through a browser — no keyboard, no monitor, no command line required in normal use.

---

## What Changed in v0.1.0

| Feature | Alpha 0.0.6 | Alpha 0.1.0 (MSA) |
|---------|-------------|-------------------|
| Simultaneous spoke connections | 1 | Up to 10 |
| VHF AX.25 (Direwolf 9600 baud) | ✗ | ✓ |
| HF AX.25 (Direwolf 300 baud) | ✗ | ✓ |
| Chat routing | Spoke ↔ Hub only | Broadcast to all connected spokes |
| Hub broadcast | ✗ | `/wall` + sidebar broadcast bar |
| Session panel | ✗ | Live spoke list with kick button |
| Hub-to-hub mesh sync | ✗ | TCP port 14257 + HF path planned |
| Dual soundcard (VHF + HF) | ✗ | ✓ simultaneous |
| TCP server | Single-client | Multi-client asyncio |

---

## What HF-256 Does

HF-256 MSA creates a private, encrypted radio BBS between a **hub station** and multiple **spoke stations**. Every message, file, and chat is protected with AES-256-GCM encryption using a shared network key that only your group holds.

**You can:**
- Exchange live chat with all connected stations simultaneously — the hub broadcasts every message to all authenticated spokes
- Send store-and-forward messages to stations that are currently off the air; the hub delivers them when the station next checks in
- Distribute files (emergency procedures, net schedules, maps) to any field station on any transport
- Connect up to 10 spokes at the same time across any mix of TCP, VHF AX.25, HF AX.25, and ARDOP transports
- Run a hub that automatically accepts ARDOP, Direwolf, and TCP connections without operator intervention
- Synchronise two hub stations with each other over the internet (mesh sync)

**Supported transports:**

| Transport | Baud rate | Use case |
|-----------|-----------|---------|
| ARDOP HF | ~200–1000 bps | HF SSB — long-distance, adaptive rate |
| ARDOP FM | ~2000 bps | VHF/UHF FM — local/regional, fast |
| VHF AX.25 (Direwolf) | 9600 baud | VHF packet — G3RUH FSK, multiple sessions |
| HF AX.25 (Direwolf) | 300 baud | HF packet — Bell 202 AFSK, multiple sessions |
| TCP | Network speed | Internet or LAN — testing and fixed infrastructure |
| Hybrid Mode | Mixed | Hub only — any combination of the above simultaneously |

---

## Hardware

Each HF-256 node is a **Raspberry Pi 4** running the HF-256 software. Any station on your network can be reached with a phone, tablet, or laptop browser — the Pi serves the interface on port 80.

### Single-radio setup (all modes)

One USB audio interface handles everything — ARDOP HF, ARDOP FM, VHF AX.25, or HF AX.25:

| Radio | Bands | Audio | PTT | Notes |
|-------|-------|-------|-----|-------|
| DigiRig Mobile | Any | External USB | RTS | Plug-and-play with most transceivers |
| Xiegu X6100 | HF | Built-in USB | CI-V CAT | Portable SDR-based HF transceiver |
| Xiegu G90 | HF | External USB | CI-V CAT | Requires DigiRig or similar |
| Icom IC-705 | HF/VHF/UHF | Built-in USB | CI-V CAT | Excellent for ARDOP FM on VHF/UHF |
| Icom IC-7300 | HF/6m | Built-in USB | CI-V CAT | Popular SDR-based HF base station |
| Icom IC-7100 | HF/VHF/UHF | Built-in USB | CI-V CAT | HF/VHF/UHF base station |
| Icom IC-9700 | VHF/UHF/SHF | Built-in USB | CI-V CAT | Ideal for VHF AX.25 9600 baud |

### Dual-radio setup (VHF + HF simultaneously)

Hub stations can run **two radios at the same time** — one for VHF packet (9600 baud) and one for HF packet (300 baud) — using two separate DigiRig Mobile adapters:

```
Pi USB port 1 ── DigiRig #1 ── 2m FM radio     (Direwolf port 0, 9600 baud)
Pi USB port 2 ── DigiRig #2 ── HF SSB radio    (Direwolf port 1, 300 baud)
```

Both radios are controlled by a single Direwolf process. ARDOP and TCP can run alongside Direwolf simultaneously via Hybrid Mode.

---

## The PiTFT Display

The HF-256 Pi appliance uses an **Adafruit Mini PiTFT 1.3" 240×240 colour display** mounted directly on the Pi's GPIO header. It shows at-a-glance status including IP address, Wi-Fi mode, station role, and the number of active spoke sessions.

### Physical buttons

**Reset to Hotspot mode (both buttons, 10 seconds)**
Hold **both buttons simultaneously for 10 seconds**. The Pi switches back to hotspot mode and broadcasts the `HF256-N0CALL` network.

**Graceful shutdown (bottom button, 10 seconds)**
Hold the **bottom button for approximately 10 seconds** until the display shows **"Shutting down"**. Always use this procedure to avoid SD card corruption.

---

## Network Roles

### Hub Station
The hub is the centre of the network. It runs continuously, listening for incoming connections from field stations on all enabled transports simultaneously. The hub:
- Accepts connections from up to 10 spokes at the same time across any transport mix
- Broadcasts live chat from any spoke to all other authenticated spokes
- Holds messages for offline stations and delivers them on next check-in
- Stores files available for download by any authenticated station
- Authenticates connecting stations against a local user database
- Can synchronise its message store and file library with other hub stations over the internet via mesh sync

### Spoke Station
A spoke is a field unit that calls into the hub. The spoke operator:
- Selects a transport (TCP, ARDOP HF/FM, VHF AX.25, or HF AX.25)
- Connects with `/connect <CALLSIGN>` or `/connect <IP>`
- Authenticates with `/auth <password>`
- Can chat live with the hub operator and all other authenticated spokes
- Can send and retrieve store-and-forward messages and download files
- Disconnects when done — the hub holds any incoming messages for the next check-in

---

## First-Time Setup

When a HF-256 Pi is first powered on it starts in **access point mode**, broadcasting its own Wi-Fi network.

**Step 1 — Connect to the HF-256 Wi-Fi hotspot**

| Network name | Password |
|---|---|
| `HF256-N0CALL` | `hf256setup` |

**Step 2 — Open the setup page**

Browse to `http://hf256.local` or `http://192.168.4.1`

**Step 3 — Select your radio and test hardware**

Select your radio interface. Use the **Test PTT** and **Check Audio Level** buttons to confirm your radio is correctly connected.

**Step 4 — Callsign and role**

Enter your callsign and select **Hub** or **Spoke**.

**Step 5 — Network key**

All stations must share the same AES-256 key. Generate a new key on the hub or paste one you have received.

**Step 6 — Direwolf configuration (hub only, optional)**

If running VHF and/or HF AX.25, open **Settings → Direwolf** after initial setup completes. Set the ALSA card number for each radio and the PTT serial port. Click **Apply** — the portal writes `/etc/direwolf/direwolf.conf` and starts the Direwolf service.

**Step 7 — Mesh peers (hub only, optional)**

Open **Settings → Mesh Peers** and add the IP address of any other hub you want to synchronise with. Sync runs automatically every 5 minutes.

---

## The Console

The console is the main operating interface at `http://<pi-address>/console`.

```
┌─────────────────────────────────────────────────────────────────────┐
│ HF-256  Status  Settings  Console  Hub Files  Help    Alpha 0.1.0   │
├──────────────────┬──────────────────────────────────────────────────┤
│ N0HUB │ TCP │ ⬤ connected │ 🔒 Encrypted │ ✓ Auth │ ⬡ 3 sessions  │
├──────────────────┴──────────────────────────────────────────────────┤
│                                                                      │
│  ★ Console connected                                                │
│  ★ Hub mode — spokes connect on port 14256                          │
│  ★ VHF AX.25 / HF AX.25 available if Direwolf is running           │
│  ★ W1ABC authenticated [VHF_AX25]                                  │
│  ← W1ABC: Hello from the field                                      │
│  ← W2DEF: Copy that, reading you 5x5                               │
│  → N0HUB [WALL]: Net starts in 10 minutes on 14.300                │
│                                                                      │
├──────────────────┬───────────────────────────────────────────────── │
│ > type message   │ Transport      │ Active Sessions       │          │
│ WALL▶ broadcast  │ ◉ TCP/Internet │ W1ABC [VHF_AX25] 12s │          │
│                  │ ○ Hybrid Mode  │ W2DEF [TCP]      45s │          │
│                  │ ○ ARDOP HF     │ W3GHI [HF_AX25]  8s  │          │
│                  │ ○ ARDOP FM     │                       │          │
│                  │ ○ VHF AX.25    │                       │          │
│                  │ ○ HF AX.25     │                       │          │
└──────────────────┴───────────────────────────────────────────────── ┘
```

The status bar shows callsign, transport, connection state, encryption mode, auth state, and — on hub stations — a live session count. The sidebar shows all available transports and, for hub stations, a live list of connected spokes with idle times and a ✕ disconnect button for each.

The **WALL▶** broadcast bar above the input field lets hub operators send a message to all connected spokes instantly.

---

## Typical Spoke Session

### 1. Select transport

Click the transport button in the sidebar that matches your radio setup:
- **TCP / Internet** — direct IP connection
- **ARDOP HF** — HF SSB (start modem first)
- **ARDOP FM** — VHF/UHF FM (start modem first)
- **VHF AX.25 9600** — Direwolf packet (Direwolf must be configured)
- **HF AX.25 300** — Direwolf HF packet (Direwolf must be configured)

### 2. Connect to the hub
```
/connect N0HUB           ← ARDOP or AX.25 (callsign)
/connect 192.168.1.10    ← TCP (IP address)
```

### 3. Authenticate
```
/auth yourpassword
```

### 4. Chat, message, and transfer
```
Hello everyone on the net       ← broadcast to all connected stations
/send W1ABC Meet at 0900Z       ← store for offline station
/retrieve                       ← collect waiting messages
/files                          ← list hub files
/download emergency_plan.pdf    ← download a file
```

### 5. Disconnect
```
/disconnect
```

---

## Hub Operations

### Managing multiple sessions

The sidebar **Active Sessions** panel shows every connected spoke with its transport type, idle time, and a ✕ button to force-disconnect it. The same information is available as a command:

```
/sessions
```

### Broadcasting to all spokes
```
/wall Net starts in 10 minutes on 14.300
```
Or use the **WALL▶** bar above the input field. The message is delivered to all authenticated spokes simultaneously regardless of which transport they connected on.

### Sending directly to one spoke
```
/send W1ABC Your file is ready for download
```
If the spoke is currently connected the message is delivered immediately. If offline it is stored for the next check-in.

### Disconnecting a spoke
```
/kick W1ABC
```
Or click the ✕ button next to their callsign in the session panel.

### Adding users
```
/adduser W1ABC secretpassword
/adduser W2DEF anotherpassword
```

### Checking stored messages and files
```
/storage
```

### Adding files for distribution
Open `http://<hub-pi-address>/files`. Upload files by clicking or dragging and dropping, add descriptions, edit or delete existing files.

---

## Direwolf AX.25

Direwolf provides **connected-mode AX.25** packet radio sessions. Unlike ARDOP, Direwolf handles multiple simultaneous connections natively — up to 10 connected AX.25 sessions can be active at the same time, all ARQ-handled automatically by Direwolf.

### VHF AX.25 — 9600 baud

- **Modulation:** G3RUH FSK — requires a flat audio path (no de-emphasis)
- **Typical range:** 20–100 km line-of-sight on a 2m FM radio
- **Throughput:** Several kilobits per second — file transfers that take 15 minutes on HF complete in seconds
- **Hardware:** DigiRig Mobile + any 2m FM radio with a data/accessory port

### HF AX.25 — 300 baud

- **Modulation:** Bell 202 AFSK, mark 1600 Hz / space 1800 Hz — standard HF packet tones
- **Typical range:** Regional to worldwide depending on band and conditions
- **Throughput:** ~250 bps effective — slower than ARDOP HF but handles multiple simultaneous sessions
- **Hardware:** DigiRig Mobile + any HF SSB transceiver

### Configuring Direwolf

Open **Settings → Direwolf** in the web portal. Set:

| Setting | VHF channel | HF channel |
|---------|-------------|------------|
| ALSA card | Card index from `arecord -l` | Second card index |
| Serial port | `/dev/ttyUSB0` (DigiRig) | `/dev/ttyUSB1` |
| PTT method | RTS | RTS |
| Baud rate | 9600 | 300 (fixed) |

Click **Apply**. The portal writes `/etc/direwolf/direwolf.conf`, unmasks the Direwolf service, and starts it. Verify with `journalctl -u direwolf -n 20`.

Direwolf is **masked by default** on the Pi image — it cannot start until the operator configures valid audio card numbers.

---

## Mesh Sync

Hub stations can synchronise their message stores and file libraries with each other automatically using the mesh sync protocol on **TCP port 14257**.

### How it works

1. Hub A connects to Hub B on port 14257
2. Hub A sends the SHA-256 digests of all messages and files in its store
3. Hub B compares digests and sends only the items Hub A does not have
4. Both sides swap roles and repeat in the other direction
5. The sync completes in seconds for small stores over a LAN; minutes over HF

All frames are encrypted with the shared network key — a hub without the correct key cannot sync.

### Configuring mesh peers

Open **Settings → Mesh Peers** and add IP addresses:

```
192.168.1.20         ← LAN hub
hub2.example.net     ← Remote hub by DNS name
10.0.0.5:14257       ← Remote hub with explicit port
```

Sync runs every 5 minutes automatically. Trigger an immediate sync:

```
POST /api/mesh/sync-now   {"address": "192.168.1.20"}
```

---

## Command Reference

### Connection
| Command | Description |
|---------|-------------|
| `/connect <CALLSIGN>` | Connect via ARDOP HF/FM or AX.25 |
| `/connect <IP> [port]` | Connect to hub via TCP |
| `/disconnect` | Cleanly disconnect |

### Authentication
| Command | Description |
|---------|-------------|
| `/auth <password>` | Authenticate with the hub |
| `/encrypt on\|off` | Toggle AES-256-GCM encryption |
| `/whoami` | Show callsign, transport, session count, and auth status |

### Messaging
| Command | Description |
|---------|-------------|
| type + Enter | Send live chat — broadcast to all authenticated spokes |
| `/send <CALL> <message>` | Store a message for an offline station |
| `/bul <message>` | Store a bulletin for all registered stations |
| `/retrieve` | Retrieve messages stored for you at the hub |

### File transfer
| Command | Description |
|---------|-------------|
| `/files` | List files available on the hub |
| `/download <filename>` | Download a file from the hub |
| `/cancel` | Cancel a download in progress |

### Hub only
| Command | Description |
|---------|-------------|
| `/wall <message>` | Broadcast to all connected authenticated spokes |
| `/sessions` | List all active sessions with transport and idle time |
| `/kick <CALLSIGN>` | Disconnect a specific spoke session |
| `/adduser <CALL> <password>` | Add a user to the hub |
| `/listusers` | List all registered users |
| `/storage` | Show queued messages and available files |

### General
| Command | Description |
|---------|-------------|
| `/clear` | Clear the console window |
| `/help` | Show the command reference panel |
| `/passwd <current> <new>` | Change your password |

---

## Encryption

All traffic is encrypted with **AES-256-GCM** using a 256-bit network key shared among all stations. The key never travels over the air. Every message, file chunk, and authentication packet is individually encrypted with a random 96-bit IV — no two transmissions look the same even if the content is identical.

The network key is stored in `/etc/hf256/network.key` on each Pi. Store-and-forward messages are double-encrypted: first with the network key in transit, then stored on the hub encrypted so that storage compromise does not expose message content.

Mesh sync frames between hub stations are also AES-256-GCM encrypted with the shared network key.

## Global Use and Regulatory Compliance

HF-256 fully supports plaintext operation. In the United States (47 CFR Part 97), Canada, and many other jurisdictions, encryption of amateur radio transmissions is prohibited or restricted. Disable encryption with:

```
/encrypt off
```

The status bar shows **🔓 Plaintext** as a permanent reminder. All stations in a session must use the same mode.

**It is the responsibility of each operator to understand and comply with the laws and licence conditions that apply in their jurisdiction.**

---

## Troubleshooting

**Hub shows session count but I cannot see chat from other spokes**
- Confirm both spokes have authenticated (`/auth`) — unauthenticated sessions do not receive broadcasts
- Check `journalctl -u hf256-portal -n 50` for any HubCore dispatch errors

**VHF AX.25 sessions connect but immediately drop**
- Verify Direwolf is running: `systemctl status direwolf`
- Check audio levels: `arecord -l` to confirm the card index matches Settings → Direwolf
- 9600 baud requires a **flat audio cable** — do not use a cable with de-emphasis filtering
- Confirm the radio's data port is set to 9600 baud in the radio menu

**HF AX.25 connection attempts do not complete**
- Confirm both stations are on USB mode (not LSB) — HF packet uses 1600/1800 Hz tones
- 300 baud FRACK is 10 seconds — be patient; the ARQ handshake takes longer than ARDOP
- Check `journalctl -u direwolf -n 30` for any "no signal" or audio level messages

**Direwolf crashes immediately after starting**
- The most common cause is an incorrect ALSA card index — run `arecord -l` on the Pi and verify the card numbers
- A second cause is the serial PTT port not existing: `ls /dev/ttyUSB*` to confirm ports
- Check: `journalctl -u direwolf -n 20`

**Mesh sync is not running**
- Confirm the peer IP is reachable on port 14257: `nc -zv <peer-ip> 14257`
- Both hubs must have the same network key — sync frames are encrypted; mismatched keys cause silent decryption failure
- Check: `journalctl -u hf256-portal -n 50 | grep mesh`

**ARDOP connection attempts time out**
- Confirm the hub Pi has ARDOP HF selected and ardopc is running (green status on Status page)
- Check that both stations are on the same frequency with USB mode
- Check `journalctl -u hf256-portal -n 50` for startup errors

**Session limit reached — new spokes rejected**
- Default maximum is 10 sessions; change `max_sessions` in `/etc/hf256/settings.json` and restart the portal
- Idle sessions are evicted after 5 minutes of silence; unauthenticated sessions after 2 minutes

**Both sides disconnected but console still shows Connected**
- The inactivity watchdog disconnects after 2 minutes of silence (radio) or 5 minutes (TCP)
- Force disconnect with `/disconnect` at any time

**No audio / PTT not keying**
- Visit the Status page and run the PTT test
- For DigiRig Mobile: confirm the USB serial port is `/dev/ttyUSB0` (or `/dev/ttyUSB1` for the second unit)
- For CI-V radios: confirm the CI-V baud rate is 19200 and the port is `/dev/ttyACM0` or `/dev/ttyACM1`

---

## File Locations on the Pi

### Runtime data

```
/etc/hf256/
├── network.key              ← AES-256 network key (protect this)
├── settings.json            ← Station configuration (all settings)
├── config.env               ← Modem startup parameters
└── backups/                 ← Timestamped config.env backups

/etc/direwolf/
└── direwolf.conf            ← Generated by portal Settings → Direwolf
                               (masked/unconfigured until operator sets it up)

/home/pi/.hf256/
├── passwords.json           ← Hub user database — SHA-256 hashed (hub only)
├── mesh_sync.json           ← Mesh sync state — last-sync timestamps per peer
├── hub_files/               ← Files available for spoke download (hub only)
│   ├── document.pdf
│   └── document.pdf.desc    ← Optional description shown in /files listing
└── hub_messages/            ← Store-and-forward mailboxes (hub only)
    ├── W1ABC/
    │   └── 1714000000000    ← Filename = millisecond timestamp
    └── W2DEF/
        └── 1714000001234
```

### Application (installed by build.sh)

```
/opt/hf256/
│
├── hf256/                   ← Core Python package
│   ├── __init__.py
│   ├── ardop.py             ← ARDOP modem interface (ARDOPConnection)
│   ├── chat.py              ← Wire protocol message types and pack/unpack
│   ├── crypto.py            ← AES-256-GCM key management
│   ├── filetransfer.py      ← File chunking helpers
│   ├── freedv.py            ← FreeDV transport
│   ├── freedv_transport.py  ← FreeDV KISS transport
│   ├── kiss.py              ← KISS framing
│   ├── mercury_transport.py ← Mercury transport
│   ├── storage.py           ← Message store helpers
│   │
│   ├── session_manager.py   ← ★ NEW — ClientSession + SessionManager
│   ├── tcp_transport.py     ← ★ UPDATED — TCPServerTransport (multi-client)
│   │                                      TCPTransport (spoke/client, unchanged API)
│   ├── direwolf_transport.py ← ★ NEW — Direwolf AGW transport (VHF + HF AX.25)
│   ├── hub_core.py          ← ★ NEW — Multi-session hub protocol handler
│   ├── mesh_sync.py         ← ★ NEW — Hub-to-hub message + file synchronisation
│   └── direwolf_config.py   ← ★ NEW — Generates /etc/direwolf/direwolf.conf
│
└── portal/                  ← Web portal (Flask, runs as root on port 80)
    ├── app.py               ← ★ UPDATED — multi-session hub services at boot
    ├── hardware.py          ← Hardware detection helpers
    ├── display.py           ← PiTFT display driver
    └── templates/
        ├── console.html     ← ★ UPDATED — session panel, broadcast bar, Direwolf buttons
        ├── status.html      ← Status dashboard
        ├── settings.html    ← Settings wizard
        ├── setup.html       ← First-run setup wizard
        ├── files.html       ← Hub file management
        └── help.html        ← Command reference

/usr/local/bin/
└── ardopc                   ← ARDOP modem binary (ardopcf arm64)

/usr/bin/
└── direwolf                 ← Direwolf soundcard TNC (from apt)
```

### Systemd services

```
/etc/systemd/system/
├── hf256-portal.service     ← Web portal (Flask on port 80) — ENABLED
├── hf256-display.service    ← PiTFT display daemon — ENABLED
├── hf256-firstboot.service  ← First-boot setup tasks — ENABLED (runs once)
├── hf256-wlan.service       ← Wi-Fi AP / client mode manager — ENABLED
├── freedvtnc2.service       ← FreeDV TNC (started on demand) — ENABLED
├── rigctld.service          ← Hamlib CAT control (started on demand) — ENABLED
├── direwolf.service         ← ★ NEW — Direwolf AX.25 TNC — MASKED until configured
└── hf256.service            ← Legacy standalone mode — MASKED (superseded by portal)
```

### Source repository layout

This is where files live in the development repository and how `build.sh` maps them to the Pi image:

```
<repo-root>/
│
├── hf256/                   ← Python package → /opt/hf256/hf256/
│   ├── __init__.py
│   ├── ardop.py
│   ├── chat.py
│   ├── crypto.py
│   ├── filetransfer.py
│   ├── freedv.py
│   ├── freedv_transport.py
│   ├── kiss.py
│   ├── mercury_transport.py
│   ├── storage.py
│   ├── session_manager.py   ← ★ NEW — place here
│   ├── tcp_transport.py     ← ★ REPLACE existing file with new version
│   ├── direwolf_transport.py ← ★ NEW — place here
│   ├── hub_core.py          ← ★ NEW — place here
│   ├── mesh_sync.py         ← ★ NEW — place here
│   └── direwolf_config.py   ← ★ NEW — place here
│
├── portal/                  ← Flask portal → /opt/hf256/portal/
│   ├── app.py               ← ★ UPDATE — apply app_additions.py patch
│   ├── hardware.py
│   ├── display.py
│   └── templates/
│       ├── console.html     ← ★ REPLACE with new version
│       ├── status.html
│       ├── settings.html
│       ├── setup.html
│       ├── files.html
│       └── help.html
│
├── services/                ← Systemd units → /etc/systemd/system/
│   ├── hf256-portal.service
│   ├── hf256-display.service
│   ├── hf256-firstboot.service
│   ├── hf256-wlan.service
│   ├── freedvtnc2.service
│   ├── rigctld.service
│   └── direwolf.service     ← ★ NEW — place here
│
├── scripts/                 ← Shell scripts → /opt/hf256/scripts/
│   ├── first-boot.sh
│   ├── wifi-mode.sh
│   ├── wifi-mode-boot.sh
│   ├── start-stack.sh
│   ├── stop-stack.sh
│   └── hf256-wifi-restore.sh
│
├── configs/                 ← System configs → installed by build.sh
│   ├── asound.conf          → /etc/asound.conf
│   ├── hostapd.conf         → /etc/hostapd/hostapd.conf
│   ├── hostapd-rfkill.conf  → /etc/systemd/system/hostapd.service.d/rfkill.conf
│   └── dnsmasq.conf         → /etc/dnsmasq.d/hf256.conf
│
└── image/                   ← Build artifacts (not installed)
    ├── build.sh             ← ★ UPDATED — builds v0.1.0 image
    ├── ardopcf_arm_Linux_64 ← ARDOP binary (download separately)
    └── radios.json          ← Supported radio definitions
```

---

## Ports Reference

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 80 | TCP | inbound | Web portal (Flask) |
| 14256 | TCP | inbound (hub) | Spoke connections — multi-client |
| 14257 | TCP | inbound (hub) | Hub-to-hub mesh sync |
| 8000 | TCP | loopback | Direwolf AGW interface |
| 8001 | TCP | loopback | Direwolf KISS interface |
| 8002 | TCP | loopback | FreeDV TNC command port |
| 8515 | TCP | loopback | ardopc command port |
| 8516 | TCP | loopback | ardopc data port |

Ports 8000–8002, 8515–8516 are loopback-only and not exposed to the network.

---

## SSH Access

```
Username: pi
Password: 12345678
```

```bash
ssh pi@hf256.local
ssh pi@192.168.4.1      # AP mode
ssh pi@<assigned-ip>    # client Wi-Fi mode
```

> **Security note:** Change the default password after initial setup: `passwd` at the SSH prompt. This is especially important for hub stations accessible over the internet.

---

## Key Log Locations

| Service | Command |
|---------|---------|
| Portal + HubCore | `journalctl -u hf256-portal -n 50 -f` |
| Direwolf | `journalctl -u direwolf -n 50 -f` |
| Session watchdog | `journalctl -u hf256-portal -n 50 \| grep session` |
| Mesh sync | `journalctl -u hf256-portal -n 50 \| grep mesh` |
| ARDOP modem | `journalctl -u hf256-portal -n 50 \| grep ardop` |
| Display daemon | `journalctl -u hf256-display -n 20` |
| Application log | `tail -f ~/.hf256/hf256.log` |

---

*HF-256 MSA — Version Alpha 0.1.0*
