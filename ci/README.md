# CI-Workflow aktivieren

[`ci.yml`](ci.yml) ist ein fertiger GitHub-Actions-Workflow. Er liegt **nicht**
unter `.github/workflows/`, weil der Deploy-Bot keine `workflows`-Berechtigung
hat — GitHub lehnt den Push sonst komplett ab:

```
refusing to allow a GitHub App to create or update workflow
`.github/workflows/ci.yml` without `workflows` permission
```

Mit deinem eigenen Account ist das ein einziger Befehl:

```bash
mkdir -p .github/workflows
git mv ci/ci.yml .github/workflows/ci.yml
git commit -m "ci: GitHub-Actions-Workflow aktivieren"
git push
```

Oder im Browser: `ci/ci.yml` öffnen → *Raw* → Datei unter
`.github/workflows/ci.yml` neu anlegen und einfügen.

## Was der Workflow prüft

| Job | Inhalt |
| --- | ------ |
| `smoke-test` | `compileall`, dann `scripts/smoke_test.py` (289 Prüfungen) + `scripts/login_recovery_test.py` (228 Prüfungen) auf **Python 3.11 und 3.12** |
| `smoke-test` | Start ohne `DISCORD_BOT_TOKEN` muss mit Exit-Code **2** und klarer deutscher Fehlermeldung abbrechen |
| `image` | Baut das Docker-Image aus [`Dockerfile`](../Dockerfile) |
| `image` | Startet den Container und verlangt, dass `/api/health` `"status":"healthy"` liefert — **auch** ohne gültigen Discord-Token |

Kein Job braucht ein Discord-Token oder Netzwerkzugriff auf Discord: Der
Smoke-Test arbeitet gegen [`scripts/_fake_discord.py`](../scripts/_fake_discord.py).

Das CI-Badge im [README](../README.md) zeigt erst einen Status, sobald die
Datei unter `.github/workflows/` liegt.
