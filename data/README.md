# Runtime data

Dieses Verzeichnis wird von Setup und Laufzeit beschrieben. Laufzeitdaten, Reports,
Modell-Katalog-Snapshots, Paperclip-Daten und generierte Setup-Skills sind ignoriert
und werden nicht versioniert.

Der Bootstrap-Report ist:

- `setup_report.json` – Werkzeug-/Modell-/Paperclip-Status ohne Zugangsdaten.

Der sichere Setup-Selbsttest nutzt `data/synth/` und berührt den Live-State nicht.
Docker-Paperclip persistiert seine Daten separat in `data/docker-paperclip/`.
