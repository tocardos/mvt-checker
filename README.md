# MVT Checker

Portable iOS forensics web app for Raspberry Pi + PTS (PiRogue Tool Suite).

## Stack

| Layer | Tools |
|---|---|
| Detect | `idevice_id` |
| Trust | `idevicepair` |
| Device info | `ideviceinfo` |
| Backup | `idevicebackup2` (password via env) |
| Analysis | `mvt-ios check-backup` |
| Sysdiagnose | `idevicecrashreport` |
| Storage | `/media/usb0/data/` |
| Web app | Flask + Bootstrap 5 + SSE |
| DB | SQLite on SSD (never wiped) |

## Deploy

```bash
git clone / copy files to Pi, then:
sudo bash install.sh
```

## Configuration

Edit `/etc/systemd/system/mvt-checker.service`:

```ini
Environment="AP_SSID=YourAPName"
Environment="AP_PASS=YourWiFiPass"
Environment="APP_HOST=mvt.local"
Environment="APP_PORT=5000"
Environment="MVT_BACKUP_PASSWORD=iPhoneBackupPass"
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart mvt-checker
```

## Paths on SSD

```
/media/usb0/data/
├── mvt-checker.db       # device history (NEVER deleted)
├── iocs/
│   └── indicators.json  # pulled from MVT community repo
├── mvt-backups/
│   └── <udid>/          # idevicebackup2 output
├── mvt-output/
│   └── <udid>/<ts>/     # mvt-ios check-backup output
└── sysdiagnose/
    └── <udid>/          # idevicecrashreport output + zip
```

## Workflow

1. Connect iPhone via USB, tap **Trust**
2. Open http://mvt.local:5005 (or scan QR code)
3. Click **Inspect** on detected UDID
4. **Pair** → **Backup** → **MVT check** → **View results**
5. Download sysdiagnose if needed
6. **Delete** backup data (DB history kept)

## Backup password

The backup password is **never** passed on the command line.
It is passed via the `IDEVICEBACKUP2_BACKUP_PASSWORD` environment variable,
invisible to `ps aux`.

You can set it:
- Globally via the systemd `Environment=` line
- Per-session via the password field in the UI (overrides env var)

## SSE log streaming

All long-running jobs (backup, MVT, sysdiagnose, IOC update) stream
output in real time to the browser via Server-Sent Events.
No polling. Works on mobile Safari.

## IOC update

Click **Update IOCs** in the navbar to pull the latest `indicators.json`
from the MVT community repository. Requires internet connectivity on the Pi.

## Logs

```bash
sudo journalctl -u mvt-checker -f

```
## Licence

The code is released under the European Public Licence European Union Public Licence - European Commission
