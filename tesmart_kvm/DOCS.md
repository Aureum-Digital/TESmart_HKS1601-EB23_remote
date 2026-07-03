# KVM Network Remote

Controls a TESmart HKS1601-EB23 16-port HDMI KVM switch over its TCP
protocol, with a web UI (Ingress), a REST API and MQTT entities.

## Installation

1. Add this repository in **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**: `https://github.com/Aureum-Digital/TESmart_HKS1601-EB23_remote`
2. Install **KVM Network Remote**.
3. Set at least `kvm_host` in the **Configuration** tab and start the add-on.
4. Use **Open Web UI** (or the **KVM** entry in the sidebar) to open the panel.

## Options

| Option | Default | Description |
|---|---|---|
| `kvm_host` | `10.0.4.50` | IP/hostname of the KVM switch |
| `kvm_port` | `5000` | TCP control port of the switch |
| `poll_interval` | `5` | Seconds between status polls |
| `mqtt_host` | *(empty)* | Empty = auto-discover the Mosquitto add-on; set for an external broker |
| `mqtt_port` | `1883` | Broker port (used with an explicit `mqtt_host`) |
| `mqtt_username` / `mqtt_password` | *(empty)* | Credentials for an explicit broker |
| `mqtt_base_topic` | `tesmart/kvm` | Prefix for all MQTT topics |
| `mqtt_discovery` | `true` | Create HA entities via MQTT discovery |
| `log_level` | `info` | `debug` logs every TX/RX frame |

Connection, MQTT and polling settings are managed here and applied on every
add-on start; the web UI shows them read-only. Input **names, icons and
hidden inputs** are edited in the web UI settings and persist in the add-on's
data directory.

## MQTT

With MQTT discovery enabled the add-on creates a device **TESmart KVM** with:
an input select (using your custom names), a current-input sensor, a buzzer
switch, an LED-timeout select and a refresh button.

Raw topics (default base `tesmart/kvm`):

- publish: `…/availability`, `…/current_input`, `…/current_input_name`, `…/status`
- subscribe: `…/set/input`, `…/set/buzzer`, `…/set/led_timeout`, `…/refresh`

## Standalone use

The same add-on runs as a plain Docker container outside Home Assistant —
see the repository README.
