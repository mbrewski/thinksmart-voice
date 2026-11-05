# AGENTS.md

## Panel access

- Host: `10.145.22.243` — hostname `thinksmarter` (Lenovo ThinkSmart View, postmarketOS 6.12 aarch64).
- SSH: `ssh ubuntu@10.145.22.243`. User is `ubuntu`; `pmos` is **not** a valid login on this unit.
- Auth: password auth is enabled. Default password is `thinksmart` (see README; it is meant to be changed, so it may differ).
- Auth works passwordless with local `~/.ssh/id_ed25519` (`mbrewer@mycoiq.com`), verified 2026-11-03: `ssh -o BatchMode=yes ubuntu@10.145.22.243 'echo ok'`.
- Fallback if the key is ever removed: `sshpass -p 'thinksmart' ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no ubuntu@10.145.22.243 ...`
- The panel's Wi-Fi drops and it reboots periodically. Check `ping -c 1 -W 2 10.145.22.243` before deploys; ARP incomplete / "No route to host" means wait and retry.

## Deploy

- Kiosk install/deploy: `./tools/deploy-kiosk.sh ubuntu@10.145.22.243` (see README).
