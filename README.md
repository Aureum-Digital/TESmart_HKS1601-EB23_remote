# TESmart HKS1601-EB23 KVM Controller

Web + MQTT controller for the TESmart HKS1601-EB23 16-port HDMI KVM switch,
talking to the switch over its raw TCP protocol (default `10.0.4.50:5000`).

- **Web UI** — input grid with custom names/icons, buzzer mute/unmute, LED
  timeout, manual refresh, auto-refresh, settings page, connection status.
- **REST API** — everything the UI does, scriptable.
- **MQTT** — control/status topics plus optional Home Assistant MQTT discovery.
- **Docker-first** — single container, config persisted in a `/config` volume.
- **Home Assistant add-on** — this repo is also an installable add-on
  repository with Ingress ("Open Web UI") and options in the HA panel.

## Home Assistant add-on

1. In HA go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add
   `https://github.com/Aureum-Digital/TESmart_HKS1601-EB23_remote`
2. Install **TESmart KVM Controller** from the store.
3. Open the add-on's **Configuration** tab and set `kvm_host` (and MQTT
   options if you don't use the Mosquitto add-on — with Mosquitto installed
   the broker is auto-discovered, leave `mqtt_host` empty).
4. Start the add-on, then click **Open Web UI** (the panel also appears in
   the sidebar as **KVM** via Ingress).

When running as an add-on, connection/MQTT/polling settings come from the HA
configuration panel and are shown read-only in the web UI; input names, icons
and hidden inputs are managed in the web UI and persist in the add-on data.
See [tesmart_kvm/DOCS.md](tesmart_kvm/DOCS.md) for all options.

## Quick start (standalone Docker)

```bash
docker compose up -d --build
```

Then open <http://localhost:8081>. Edit `docker-compose.yml` first if your KVM
is not at `10.0.4.50:5000` or you want MQTT enabled from the start. The
container listens on 8080 internally; the compose file maps it to host port
8081 (8080 was taken on the original host) — change the `ports:` mapping to
taste.

Environment variables (`TESMART_HOST`, `MQTT_HOST`, …) only **seed**
`config/config.json` on the very first start. After that, whatever you save in
the web UI's settings page wins. Delete `config/config.json` to re-seed from
the environment.

| Variable | Default | Purpose |
|---|---|---|
| `TESMART_HOST` | `10.0.4.50` | KVM IP/hostname |
| `TESMART_PORT` | `5000` | KVM TCP port |
| `MQTT_HOST` | *(empty)* | MQTT broker; empty disables MQTT |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(empty)* | Broker credentials |
| `MQTT_BASE_TOPIC` | `tesmart/kvm` | Base for all topics |
| `CONFIG_DIR` | `/config` | Where `config.json` lives |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every TX/RX hex frame |

> **Security note:** there is no authentication on the web UI/API, and
> `GET /api/config` returns the MQTT password so the settings form can
> round-trip it. Run this on a trusted LAN (or behind a reverse proxy with
> auth) only.

## Running without Docker (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
CONFIG_DIR=./config uvicorn app.main:app --reload --port 8080
pytest                      # protocol + client tests
```

## REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | Cached state: active input, connection, MQTT status |
| `POST` | `/api/refresh` | Query the device right now |
| `POST` | `/api/input/{1..16}` | Switch input |
| `POST` | `/api/buzzer/mute` / `/api/buzzer/unmute` | Buzzer control |
| `POST` | `/api/led-timeout/{10\|30\|never}` | LED timeout |
| `GET/POST` | `/api/config` | Read / update persisted config (partial updates OK) |

Device-unreachable errors return **502** with a `detail` message. Interactive
docs at `/docs`.

## MQTT

All topics are relative to the configurable base topic (default `tesmart/kvm`).

**Published (retained):**

| Topic | Payload |
|---|---|
| `…/availability` | `online` / `offline` (LWT) |
| `…/current_input` | `1`…`16` |
| `…/current_input_name` | configured friendly name |
| `…/status` | JSON snapshot (`connected`, `current_input`, `last_error`, …) |

**Subscribed:**

| Topic | Payload |
|---|---|
| `…/set/input` | `1`…`16`, or a configured input name |
| `…/set/buzzer` | `mute` / `unmute` |
| `…/set/led_timeout` | `10` / `30` / `never` |
| `…/refresh` | anything |

**Home Assistant discovery** (on by default when MQTT is enabled, prefix
`homeassistant`) announces: a select for the input (using your friendly
names), a sensor for the current input number, an optimistic switch for the
buzzer, an optimistic select for LED timeout, and a refresh button — all under
one "TESmart KVM" device. Discovery configs are republished when you save
settings, so renamed inputs propagate.

## Protocol notes

Commands are 6-byte frames `AA BB 03 <cmd> <data> EE`:

| Action | Frame |
|---|---|
| Select input *n* (1–16) | `AA BB 03 01 <n> EE` |
| Query active input | `AA BB 03 10 00 EE` |
| Mute / unmute buzzer | `AA BB 03 02 00 EE` / `AA BB 03 02 01 EE` |
| LED timeout 10 s / 30 s / never | `AA BB 03 03 0A EE` / `AA BB 03 03 1E EE` / `AA BB 03 03 00 EE` |

The status response is `AA BB 03 11 <zero_based_input> <checksum>` where the
input is **zero**-indexed (`0x00` = PC1) and the last byte is a checksum
(`0x16 + zero_based_input`), **not** `EE`. The device only answers if the TCP
connection is kept open after the query — the client handles this.

Each command uses a fresh TCP connection with retries; all device access is
serialised, and everything is async so the web server never blocks on the
switch.

### Experimental: device network config

The device also accepts undocumented ASCII strings (`IP:10.0.4.50;`,
`PT:5000;`, `GW:10.0.4.1;`, `MA:255.255.255.0;`) on the same port to change
its network settings. These are implemented in the library only
(`TESmartKVM.set_network_config()` / `protocol.build_network_config()`) and
are deliberately **not** exposed via the UI or REST API — a wrong value can
make the switch unreachable.

## Project layout

```
repository.yaml         # makes this repo a HA add-on repository
docker-compose.yml      # standalone deployment
tesmart_kvm/            # the add-on (also the standalone image context)
  config.yaml           #   HA add-on manifest: options schema, ingress, ports
  build.yaml            #   per-arch base images for Supervisor builds
  Dockerfile            #   BUILD_FROM-aware; same image standalone and add-on
  DOCS.md               #   add-on documentation shown in HA
  translations/en.yaml  #   pretty option labels in the HA config panel
  app/
    tesmart/            # standalone library — no web/MQTT dependencies
      protocol.py       #   frame building/parsing (pure functions)
      client.py         #   async TCP client (TESmartKVM)
    config.py           # JSON config in $CONFIG_DIR; reads HA add-on options
    controller.py       # shared state + background status poller
    mqtt_bridge.py      # MQTT publish/subscribe + HA discovery
    main.py             # FastAPI app / REST API
    static/index.html   # web UI (relative URLs — works behind HA Ingress)
tests/                  # protocol frame + client tests (fake KVM server)
```

How the dual mode works: the app resolves its config dir as `$CONFIG_DIR` →
`/data` (if `/data/options.json` exists, i.e. running as an add-on) →
`/config`. As an add-on it overlays `/data/options.json` onto the persisted
config on every start and, when `mqtt_host` is empty, asks the Supervisor for
the Mosquitto service credentials (`services: mqtt:want`).
