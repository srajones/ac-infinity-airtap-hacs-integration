![AC Infinity AirTap Icon](https://raw.githubusercontent.com/srajones/ac-infinity-airtap-hacs-integration/main/custom_components/ac_infinity_airtap_ble/brand/icon@2x.png)

# AC Infinity AirTap HACS Integration

<p align="center">
<a href="https://buymeacoffee.com/srajones"><img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee"></a>
</p>

Local **Bluetooth LE** control of AC Infinity AirTap register / booster-fan vents
(e.g. the AIRTAP T4) — no vendor cloud or phone app required.

- **Fan**: on / off and 10-step speed control.
- **Temperature** sensor.

Requires a connectable Bluetooth path in range of the vent (an ESPHome
`bluetooth_proxy` node or a local adapter on the Home Assistant host). Active
scanning is recommended.

After installing via HACS, restart Home Assistant, then add the integration from
**Settings → Devices & Services → Add Integration → AC Infinity AirTap HACS Integration**.
