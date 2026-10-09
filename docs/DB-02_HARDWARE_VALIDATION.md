# DB-02 hardware validation procedure (operator-run, read-only)

This procedure is the hardware acceptance gate for DB-02 (ROADMAP_DEVICE_BACKEND.md). It is run by the operator on their own machine and network. It is **not** run in the cloud environment. It was run by the operator on a Seestar S30 Pro (firmware 9.31); the results, the evidence classification and the limitations are recorded in `docs/DB-02_ACCEPTANCE_RECORD.md`.

## Safety rules (apply to every step)

- The validation sends only these requests, plus the authentication prerequisite: `get_device_state` (allow-listed top-level keys only), `iscope_get_app_state`, `test_connection`.
- It must not move the telescope, open or close the arm, start or stop any camera mode, open a preview or RTSP stream, change any setting, or enable a heater. The script has no code path for any of these.
- The authentication handshake is a connection prerequisite only. It grants no permission for physical control, and TSNDSS has no control surface.
- Leave the arm and cameras in whatever state they are already in. Do not use the phone app to change modes while the script runs.
- Stop immediately and report if the telescope moves or any camera mode changes at any point.

## Credentials

- You supply your own RSA key file. TSNDSS never extracts, bundles, embeds or commits key material.
- Put the key **outside** the repository, and pass its path through an environment variable:
  - Windows CMD: `set TSNDSS_SEESTAR_KEY_PATH=C:\path\to\your\key.pem`
  - POSIX: `export TSNDSS_SEESTAR_KEY_PATH=/path/to/your/key.pem`
- The `cryptography` Python package is needed for signing and is an optional dependency. TSNDSS does not install it for you.
- `.gitignore` excludes `*.pem` and the default report name as a guard, not as a substitute for keeping the key outside the repository.

## Preconditions

1. Telescope powered, on the same network as the machine running the script, firmware 9.31 recorded.
2. You know the telescope address. Prefer an explicit address; UDP discovery is opt-in (`--udp`) and an explicit host always wins.
3. Note the physical state before you start (arm open or closed, which camera mode is shown in the phone app). You will compare it afterwards by eye.
4. Run the offline suite first: `python -m unittest tests.test_seestar_protocol tests.test_seestar_provider tests.test_seestar_boundaries tests.test_seestar_traceability`.

## Procedure

Run once:

```
python tools/seestar_readonly_validate.py --host <telescope address> --out seestar_validation_report.json
```

The script refuses to replace an existing report file; pass `--overwrite` to do so deliberately, or choose another `--out` name.

The script prints only `[PASS]`/`[FAIL]` step names. It never prints the address, key path or serial.

| Step | Expected result | Roadmap acceptance item |
|---|---|---|
| Credential configuration present | PASS | runtime configuration without secret leakage |
| Discovery returns one runtime Device Reference | PASS; report shows model, firmware and a 4-character fingerprint only | read-only discovery returns a runtime Device Reference |
| Connection established with identity verified | PASS | connection state reports provider evidence |
| Evidence refresh reaches ready | PASS | Connection lifecycle |
| Capability report is fresh and read-only | PASS; unsupported physical operations are unavailable | Capability Reports include freshness metadata; static capabilities do not prove command success |
| Telemetry sample carries host observation time | PASS; state counts recorded | telemetry preserves unknown/stale/unavailable |
| Preview availability is evidence only | PASS; availability recorded | no preview runtime |
| Mount and view state unchanged across the session | PASS | no physical side effects |
| Reconnect uses a new connection identity | PASS | REQ-042 |

Then perform these **manual** checks and record the result in the report notes:

1. The telescope did not move and no camera mode started or stopped.
2. Repeat the script with the telescope address changed (for example after a DHCP renewal or hotspot change) and confirm discovery with the new address returns the same device fingerprint.
3. Interrupt the network (disable the telescope Wi-Fi link or power it off briefly) while no script is running, run the script, and confirm it reports a nonfatal discovery failure and no crash; restore the network and confirm recovery on the next run.
4. Confirm `seestar_validation_report.json` contains no address, key path, serial, Wi-Fi name or password.

## Evidence to return

- `seestar_validation_report.json` after you have reviewed it.
- Optionally sanitized structure captures (field names and types only) of the three read replies, to turn the synthetic fixtures into verified ones. Replace addresses, serials and all Wi-Fi and location data first.
- Never return key files, passwords, addresses, serials, Wi-Fi names, location data or raw payloads.

## Open items this procedure is meant to settle

- The actual firmware 9.31 top-level key set and whether the `keys` filter is honored.
- Whether the device-side `Timestamp` field is wall-clock or a monotonic counter (DB-02 treats it as unverified raw text).
- Idle connection drop timing and behavior with a phone app connected at the same time.
- Whether the authentication handshake is accepted with the key you supply (a rejection is reported as a fatal discovery failure with category `auth_rejected`).
- Whether read requests are served without the handshake on firmware 9.31. TSNDSS requires a key by design because the handshake is documented as mandatory from firmware 7.18; that is secondary evidence and is not proven here.

## Acceptance

DB-02 may be accepted only when all steps above PASS on real hardware, the manual checks show no physical side effect, and the results are recorded. A single successful run is evidence, not production readiness.
