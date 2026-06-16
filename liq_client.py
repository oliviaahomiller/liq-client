#!/usr/bin/env python3
"""
Liquefaction + BoP client loop.
Polls liquefaction and BoP telemetry every second.

For liquefaction: checks alerts, detects command/reality divergence, and drives
the status LEDs. For BoP: polls and logs key readings only — BoP telemetry has
no fault flags or *_actual feedback fields, so there is nothing to alert on or
divergence-check (see BopSensors in control/src/models/telemetry.rs).

Also exposes send_control() for writing controls to either subsystem.

Runs on the Raspberry Pi — GPIO 17 = LED 1 (warnings), GPIO 27 = LED 2 (errors).

The liquefaction and BoP backends may live on different hosts. Set each
independently with LIQ_BASE_URL / BOP_BASE_URL. BASE_URL is a fallback for
either one if its specific variable is unset (handy when they share a host).

Usage:
    # Separate hosts:
    LIQ_BASE_URL=http://<liq-host>:8000 BOP_BASE_URL=http://<bop-host>:8000 python3 liq_client.py

    # Same host for both:
    BASE_URL=http://<host>:8000 python3 liq_client.py
"""

import os
import time
import requests
from gpiozero import LED

led_warning = LED(17)  # LED 1 — warnings
led_error   = LED(27)  # LED 2 — errors

BASE_URL     = os.environ.get("BASE_URL", "http://localhost:3000")
LIQ_BASE_URL = os.environ.get("LIQ_BASE_URL","http://10.3.0.183:3000" )
BOP_BASE_URL = os.environ.get("BOP_BASE_URL", "http://10.3.0.94:3000" )

LIQ_TELEMETRY_URL = f"{LIQ_BASE_URL}/api/v1/system/liquefaction/telemetry"
LIQ_CONTROLS_URL  = f"{LIQ_BASE_URL}/api/v1/system/liquefaction/controls"
BOP_TELEMETRY_URL = f"{BOP_BASE_URL}/api/v1/system/bop/telemetry"
BOP_CONTROLS_URL  = f"{BOP_BASE_URL}/api/v1/system/bop/controls"

CONTROLS_URLS = {
    "liquefaction": LIQ_CONTROLS_URL,
    "bop":          BOP_CONTROLS_URL,
}

# The backend puts every route behind a Bearer-token check (require_auth in
# control/src/api/middleware.rs). Without this header the server replies
# "400 Bad Request: Header of type `authorization` was missing".
# AUTH_TOKEN is the shared default; LIQ_/BOP_ override it per host since the
# two backends may run with different AUTH_TOKEN values.
AUTH_TOKEN     = os.environ.get("AUTH_TOKEN", "")
LIQ_AUTH_TOKEN = os.environ.get("LIQ_AUTH_TOKEN", AUTH_TOKEN)
BOP_AUTH_TOKEN = os.environ.get("BOP_AUTH_TOKEN", AUTH_TOKEN)

AUTH_TOKENS = {
    "liquefaction": LIQ_AUTH_TOKEN,
    "bop":          BOP_AUTH_TOKEN,
}


def auth_headers(token: str) -> dict:
    """Bearer auth header for the backend, or empty if no token is set."""
    return {"Authorization": f"Bearer {token}"} if token else {}


POLL_INTERVAL_S = 1.0

# Controls that have a matching *_actual sensor — checked for divergence.
# Liquefaction only: BoP exposes no *_actual feedback fields.
COMMAND_ACTUAL_PAIRS = [
    ("column_heater",          "column_heater_actual"),
    ("haskell_inlet",          "haskell_inlet_actual"),
    ("haskell_control",        "haskell_control_actual"),
    ("co2_sensor_zero",        "co2_sensor_zero_actual"),
    ("tc_probe_heaters_enable","tc_probe_heaters_enable_actual"),
    ("chiller_n20c_power",     "chiller_n20c_power_actual"),
    ("chiller_5c_power",       "chiller_5c_power_actual"),
    ("o2_sensor_power",        "o2_sensor_power_actual"),
    ("spare_k7",               "spare_k7_actual"),
]


def send_control(control_id: str, value, subsystem: str = "liquefaction") -> bool:
    """POST a single control value to a subsystem. Returns True on success."""
    base = CONTROLS_URLS.get(subsystem)
    if base is None:
        print(f"[CONTROL FAIL]  unknown subsystem {subsystem!r}")
        return False
    url = f"{base}/{control_id}"
    try:
        resp = requests.post(
            url,
            json={"value": value},
            headers=auth_headers(AUTH_TOKENS.get(subsystem, "")),
            timeout=5,
        )
        body = resp.json()
        if body.get("status") == "error":
            print(f"[CONTROL ERROR] {subsystem}.{control_id}={value} → {body}")
            return False
        print(f"[CONTROL OK]    {subsystem}.{control_id}={value}")
        return True
    except Exception as e:
        print(f"[CONTROL FAIL]  {subsystem}.{control_id}={value} → {e}")
        return False


def check_alerts(sensors: dict):
    """Liquefaction alerts. Returns (has_warning, has_error). Does not touch the
    LEDs — main() aggregates flags across subsystems and drives them once."""
    has_warning = False
    has_error   = False

    if sensors.get("error_condition"):
        print(f"[ALERT] error_condition set — fault_code={sensors.get('fault_code')}")
        has_error = True
    if sensors.get("base_controller_fault"):
        print("[ALERT] base_controller_fault")
        has_error = True

    if sensors.get("liquid_level_low_warning"):
        print("[ALERT] liquid_level_low_warning")
        has_warning = True
    if sensors.get("o2_cutoff_active"):
        print(f"[ALERT] O2 cutoff active — o2_ppm={sensors.get('o2_ppm')}")
        has_warning = True
    if sensors.get("loop_slow"):
        print(f"[ALERT] firmware loop slow — loop_duration_ms={sensors.get('loop_duration_ms')}")
        has_warning = True

    return has_warning, has_error


def check_bop_alerts(sensors: dict):
    """BoP alerts, mirroring check_alerts(). Returns (has_warning, has_error).

    NOTE: BopSensors (control/src/models/telemetry.rs) does not currently expose
    any of these fault/warning fields, so these checks never fire today — every
    .get() returns None. They are wired up in advance: the moment the firmware /
    Rust model starts publishing these flags, this will poll them and the shared
    LEDs will respond, exactly like liquefaction. Rename the keys below to match
    whatever fields BopSensors eventually gains."""
    has_warning = False
    has_error   = False

    if sensors.get("error_condition"):
        print(f"[BOP ALERT] error_condition set — fault_code={sensors.get('fault_code')}")
        has_error = True
    if sensors.get("base_controller_fault"):
        print("[BOP ALERT] base_controller_fault")
        has_error = True

    if sensors.get("warning_condition"):
        print("[BOP ALERT] warning_condition")
        has_warning = True
    if sensors.get("loop_slow"):
        print(f"[BOP ALERT] firmware loop slow — loop_duration_ms={sensors.get('loop_duration_ms')}")
        has_warning = True

    return has_warning, has_error


def check_divergence(controls: dict, sensors: dict) -> bool:
    diverged = False
    for cmd_key, actual_key in COMMAND_ACTUAL_PAIRS:
        cmd    = controls.get(cmd_key)
        actual = sensors.get(actual_key)
        if cmd is None or actual is None:
            continue
        if cmd != actual:
            print(f"[DIVERGE] {cmd_key}: commanded={cmd}, actual={actual}")
            diverged = True
    return diverged


def process_liquefaction_sample(sample: dict):
    """Returns (has_warning, has_error) for this subsystem."""
    liq      = sample["liquefaction"]
    controls = liq["controls"]
    sensors  = liq["sensors"]

    has_warning, has_error = check_alerts(sensors)
    if check_divergence(controls, sensors):
        has_error = True

    # Log a few key readings
    print(
        f"  [LIQ] state={sensors.get('system_state')}  "
        f"o2={sensors.get('o2_ppm')} ppm  "
        f"accum={sensors.get('accumulator_pressure_psi')} psi  "
        f"hold_tank={sensors.get('holding_tank_pressure_psi')} psi  "
        f"vent_flow={sensors.get('column_vent_flow_rate_slpm')} slpm"
    )

    return has_warning, has_error


def process_bop_sample(sample: dict):
    """Returns (has_warning, has_error) for this subsystem. No divergence check —
    BoP exposes no *_actual feedback fields."""
    sensors = sample["bop"]["sensors"]

    has_warning, has_error = check_bop_alerts(sensors)

    # Log a few key readings
    print(
        f"  [BOP] accum_dry={sensors.get('co2_accumulator_dry_pressure')} "
        f"dew_pt={sensors.get('co2_accumulator_dry_dew_point')}  "
        f"extr_manifold={sensors.get('extraction_manifold_pressure')}  "
        f"purge_manifold={sensors.get('purge_manifold_pressure')}  "
        f"prod_co2={sensors.get('product_co2_ppm')} ppm  "
        f"prod_flow={sensors.get('product_co2_flow')}  "
        f"compressor_pwr={sensors.get('compressor_power')}"
    )

    return has_warning, has_error


def fetch_latest(url: str, label: str, token: str = ""):
    """GET a telemetry endpoint and return the most recent sample, or None."""
    try:
        resp = requests.get(url, headers=auth_headers(token), timeout=5)
        data = resp.json()
        samples = data.get("data", [])
        if not samples:
            print(f"[WARN] no {label} samples in response")
            return None
        return samples[-1]
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] cannot reach server for {label} telemetry")
    except Exception as e:
        print(f"[ERROR] {label} telemetry: {e}")
    return None


def main():
    print(
        f"Polling:\n"
        f"  {LIQ_TELEMETRY_URL}\n"
        f"  {BOP_TELEMETRY_URL}\n"
        f"every {POLL_INTERVAL_S}s — Ctrl-C to stop\n"
    )
    while True:
        has_warning = False
        has_error   = False

        liq_sample = fetch_latest(LIQ_TELEMETRY_URL, "liquefaction", LIQ_AUTH_TOKEN)
        if liq_sample is not None:
            try:
                w, e = process_liquefaction_sample(liq_sample)
                has_warning |= w
                has_error   |= e
            except Exception as e:
                print(f"[ERROR] processing liquefaction sample: {e}")

        bop_sample = fetch_latest(BOP_TELEMETRY_URL, "bop", BOP_AUTH_TOKEN)
        if bop_sample is not None:
            try:
                w, e = process_bop_sample(bop_sample)
                has_warning |= w
                has_error   |= e
            except Exception as e:
                print(f"[ERROR] processing bop sample: {e}")

        # Drive the shared LEDs once from the combined state so neither
        # subsystem clobbers the other's alert.
        led_warning.off() if has_warning else led_warning.on()
        led_error.on()   if has_error   else led_error.off()

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    finally:
        led_warning.off()
        led_error.off()
