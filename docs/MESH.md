# Fleet ⇄ Wardrive Go over Meshtastic

Wardrive Go (store build 2.29+) chats over a Meshtastic radio paired to the
phone. The fleet joins that chat by putting **its own** Meshtastic radio on the
Pi, sharing one private channel with the phone's radio.

```
 phone (Wardrive Go) ─BLE─ Heltec V3 ─LoRa─ Pi's radio ─USB─ mesh_bridge.py ─ MQTT ─ Home Assistant
                                                                  ▲
                         signot/squachwatch/+/detection, kismet/alerts/wifi
```

The Pi can't share the phone's radio. Meshtastic gives each incoming packet to
one connected client, so the Pi and Wardrive Go would each lose messages.

## 1. The Pi's radio
Flash Meshtastic onto the second Heltec. For the Wireless Stick V3, use the
`heltec-wsl-v3` build (flasher.meshtastic.org); its tiny OLED may stay blank. Set
the **same region and modem preset** as the phone's radio, then plug it into the Pi.

## 2. The private channel
On the Pi:
```bash
meshtastic --port /dev/serial/by-id/<pi-radio> --ch-add signot   # random PSK
meshtastic --port /dev/serial/by-id/<pi-radio> --info            # copy the channel URL
```
On the phone, in Wardrive Go → Meshtastic radio settings, add the `signot`
channel with the same name and PSK (or open the channel URL). Don't change the
primary channel.

## 3. Run it
```bash
sudo cp deploy/mesh/signot-mesh-bridge.env.example /etc/default/signot-mesh-bridge
sudo $EDITOR /etc/default/signot-mesh-bridge   # MESH_PORT, MESH_MY_NODES (phone radio id), MESH_DRY_RUN=0
sudo cp deploy/mesh/signot-mesh-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now signot-mesh-bridge
journalctl -u signot-mesh-bridge -f
```
The bridge must be the only program on the radio's port. Don't start Ragnar's
Mesh tab on the same port.

## What goes where
| Direction | Topic / channel | Notes |
|---|---|---|
| mesh → MQTT | `signot/mesh/nodes/<id>` (retained) | name, hw, lat/lon/alt, battery, snr, hops |
| mesh → MQTT | `signot/mesh/messages` | every text heard: `{from, from_name, channel, text, rx_snr, ts}` |
| mesh → HA | sensors *Mesh nodes heard* and *Last mesh message*; a device_tracker per `MESH_MY_NODES` id | the phone's radio position shows on the HA map |
| fleet → mesh | `signot` channel only | SquachWatch `new` hits of FLOCK, AXON, ALPR, SKIMMER, DRONE, RAVEN, META, HACKER, DEAUTH, EVIL TWIN and MY ALERT; Kismet alerts of severity ≥ 7 or deauth/spoof/rogue |

Airtime guard: one alert per (source, type, MAC) every 30 min, and at most one
message a minute (bursts of 3). Anything over that becomes one
"+N more fleet alerts (see HA)" message. If the radio has no secondary channel
named `signot`, the bridge never transmits.
