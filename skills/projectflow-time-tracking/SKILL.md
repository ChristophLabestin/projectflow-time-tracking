---
name: projectflow-time-tracking
description: Richtet automatische Prompt-Zeiterfassung für Codex und Claude Code im aktuellen Repository ein – mit einem vorhandenen oder neuen ProjectFlow-Projekt oder sofort nur lokal (ohne Connector, Token oder Projekt; späterer CSV-Upload). Für die erstmalige Einrichtung, lokale Aktivierung oder einen ausdrücklich gewünschten Projektwechsel.
---

# ProjectFlow-Zeiterfassung

## Lokale Erfassung läuft immer

Sobald das Repository verknüpft ist – mit Projekt oder ohne –, erfassen die Hooks jeden Prompt lokal. Ob und wann
hochgeladen wird, ist davon getrennt: automatisch nur bei Online-Verknüpfung mit Token und aktivierter Erfassung im
Projekt. Fehlt etwas davon (kein Connector, kein Token, Server nicht erreichbar, Erfassung im Projekt aus), bleiben die
Einträge lokal und lassen sich exportieren. Ein Eintrag geht entweder automatisch hoch oder per Datei, nie doppelt.
Die Einrichtung deshalb nie an Connector, Token oder Projekt scheitern lassen:

- **Projekt bekannt, aber kein Connector/Token in diesem Account:** IDs vom Nutzer (ProjectFlow → Projekteinstellungen →
  Agent-Zeit, Verknüpfungsblock) und direkt verknüpfen:
  ```sh
  python3 <skill>/scripts/agent_time.py link --offline --cwd <repo> --tenant-id <tenant> --project-id <project>
  ```
- **Kein Projekt gewünscht oder noch keins vorhanden** („nur lokal“, Connector fehlt und keine IDs genannt): nicht nach
  einem Projekt fragen, sondern sofort `python3 <skill>/scripts/agent_time.py link --offline --cwd <repo>`.
- Danach in beiden Fällen `python3 <skill>/scripts/agent_time.py install --client <client>` (`claude` in Claude Code,
  `codex` in Codex). Ab dem nächsten, neu gestarteten Prompt wird erfasst; Claude Code ggf. Hooks über `/hooks` bestätigen.
- Bestehende Verknüpfung nie stillschweigend ersetzen: nachfragen, dann `--replace`.
- Später online mit denselben IDs verknüpfen (`setup.py configure`, siehe unten) lädt auch die bisher lokal erfassten,
  noch nicht exportierten Einträge automatisch hoch.

Hochladen ohne Connector: `python3 <skill>/scripts/agent_time.py export --out <ordner>` erzeugt je Projekt bzw.
Repository eine CSV (`agent-time-<projekt|repo>-<zeitpunkt>.csv`). Import in ProjectFlow → Projekt → Timer-Symbol im
Projektkopf (Agentenzeit) → „Vergangene Zeiten importieren“ (Erfassung in den Projekteinstellungen aktiviert). Erneut exportieren nur mit `--all`; doppelter
Import fügt dank `id`-Spalte nichts hinzu. Stand: `agent_time.py status`.
## Projekt wählen

Wenn noch kein Ziel genannt wurde, zuerst fragen und die Antwort abwarten:
**„Welches Projekt aus ProjectFlow möchtest du mit diesem Repository verknüpfen? Nenne das Projekt, sag ‚Neues Projekt erstellen‘ oder ‚nur lokal erfassen‘.“**
Bei „nur lokal“ oder ohne verfügbaren Connector den passenden Weg aus „Lokale Erfassung läuft immer“ nehmen.

- Bereits genannte ID oder eindeutigen Namen verwenden; nicht erneut fragen.
- Über ProjectFlow-MCP `whoami` und `search`/`get_project` das Ziel prüfen. Bei mehreren Treffern passende Titel/IDs zur Auswahl zeigen.
- Bei „neu“ nur den fehlenden Namen erfragen. Vor `create_project` gleichnamige Projekte prüfen, um unbeabsichtigte Duplikate zu vermeiden. Nur auf diesen Nutzerauftrag hin anlegen.
- Werkzeugpräfixe unterscheiden sich zwischen Codex und Claude. Die unten genannten verfügbaren Werkzeuge nutzen. Fehlen sie, Katalogaktualisierung/Neuauswahl des Plugins anfordern, keine unbekannten Aufrufe erfinden.

## Einrichtung

1. `resolve_project_binding(projectId)` lesen: Titel, Mandant, `enabled`, `canManage`. Das aktuelle Git-Repository verwenden, niemals das Skill-Verzeichnis. IDs aus der Antwort übernehmen.
2. Lokalen Zustand prüfen; `<skill>` ist das Verzeichnis dieser SKILL.md:
   ```sh
   python3 <skill>/scripts/setup.py inspect --cwd <repo> --tenant-id <tenant> --project-id <project>
   ```
   Bei anderer Bindung zuerst den Wechsel klären. `--replace` nur für einen ausdrücklich gewünschten Wechsel. Alte Zeitdaten behalten ihr Ziel. Bei fehlendem lokalem Zugang siehe unten.
3. Falls noch aus: `set_agent_time_tracking(projectId, enabled=true)`. Die gewünschte Einrichtung autorisiert diese Aktivierung. Fehlende Owner-/Admin-Rechte nicht umgehen. Mit `resolve_project_binding` den gespeicherten Zustand bestätigen.
4. Repository und beide Client-Hooks einrichten:
   ```sh
   python3 <skill>/scripts/setup.py configure --cwd <repo> --tenant-id <tenant> --project-id <project>
   ```
   Scheitert das an fehlendem Token oder Server, nicht abbrechen: mit denselben IDs `agent_time.py link --offline …`
   (siehe oben), damit lokal sofort erfasst wird; den Token-Schritt als offen nennen.
   Das Skript prüft den Server, schreibt `.projectflow/project.json`, registriert den Git-Stamm inkl. Worktrees lokal und ergänzt beide Hook-Konfigurationen unter Erhalt bestehender Hooks. Wenn nur ein Client gewünscht ist, `--client codex` bzw. `--client claude` verwenden.
5. Codex verlangt Vertrauen in die exakten Hooks über seine unterstützte `/hooks`-Verwaltung. Bestehendes Trust weiterverwenden. Keine Hashes oder globalen Bypass-Schalter setzen. Ist neue Freigabe erforderlich, die konkret angezeigten Hooks freigeben lassen und erklären, dass die Anforderung von Codex stammt. Claude Code über `/hooks` prüfen; bestehende Clients bei Bedarf neu starten.

## Lokaler Zugang, falls nötig

Die MCP-Verbindung gibt dem Upload-Helfer keine Zugangsdaten. Vorhandene Helfer-Zugangsdaten für dieses API-Ziel/Workspace oder `PROJECTFLOW_API_TOKEN` aus der Umgebung nutzen. Niemals Tokens anzeigen, im Chat erfragen oder aus fremden Client-/Browser-Konfigurationen auslesen.

Fehlt der Zugang, benötigt der Nutzer einmalig einen persönlichen ProjectFlow-API-Token mit `projects:read` und `agent-time:write`, mit Zugriff auf die beabsichtigten Projekte. Nach Aktivierung diesen fertig ausgefüllten Befehl **im eigenen Terminal** ausführen lassen:
```sh
python3 <skill>/scripts/setup.py configure --cwd <repo> --tenant-id <tenant> --project-id <project> --prompt-token
```
Die Eingabe ist verdeckt; niemals im Agenten-Tool, Chat oder Shell-Verlauf eingeben. Speicherung mit Modus 0600 außerhalb des Repos. Bei 401/403 Berechtigung und Projektbindung des Tokens klären, nicht endlos wiederholen. Der Helfer speichert einen Zugang pro API-Ziel/Workspace. Ein neuer Token ersetzt dort den bisherigen: bei projektbeschränkten Tokens bestehende Repository-Bindungen berücksichtigen, damit deren Uploads weiter funktionieren. Keine Tokens automatisch erzeugen oder Rechte still erweitern.

## Prüfung und Übergabe

- `setup.py inspect` wiederholen: richtige Bindung, `registered: true`, Zugang vorhanden, vier Codex-/drei Claude-Handler für die gewählten Clients.
- Server-Opt-in, lokale Einrichtung, Hook-Trust und echten Upload getrennt ausweisen. Erst ein neu gestarteter echter Folgeprompt mit bestätigtem `sent`-Intervall belegt die automatische Erfassung. Keine Testzeit erfinden. Bei fehlendem Trust/Zugang als ausstehend kennzeichnen.
- Projekt, Repository, Clients und ggf. verbleibenden Schritt knapp nennen. Keine globalen AGENTS.md-/CLAUDE.md-Anweisungen ergänzen oder ohne Auftrag committen.
- Der Skill läuft nur zur Einrichtung. Danach native Hooks ohne zusätzlichen Modellaufruf im Normalfall: Start lokal, ein Batch pro abgeschlossenem Prompt, eine Policy-Abfrage je neuer Sitzung. Zwischen Prompts läuft keine Uhr. Bruttolaufzeit inkl. möglicher Wartezeiten; keine automatische persönliche Arbeitszeit/Abrechnung. Keine Prompt-Inhalte oder vollständigen Antworten speichern. Aus dem Session-Transkript liest der Stop-Hook nur Token-Zählwerte (Input, Output, Cache, Modell) des Prompts. Ein kurzer Hook-Kontext fordert pro Prompt eine Abschlusszeile `ProjectFlow: <Tätigkeit>` (5–15 Wörter, höchstens 160 Zeichen, Sprache des Nutzers). Nur tatsächlich erledigte Arbeit nennen, offene/fehlgeschlagene Schritte ehrlich kennzeichnen; keine Geheimnisse oder privaten Pfade. Der Stop-Hook übernimmt ausschließlich diese Zeile als Beschreibung in den bestehenden Batch. Fehlt sie, fordert er sie höchstens einmal nach; die Uhr endet bereits beim ersten Stop. Bleibt sie aus, hält er den Eintrag lokal zur Prüfung statt eine Beschreibung zu erfinden. Abgebrochene Läufe können ohne Abschlussbeschreibung bleiben.
