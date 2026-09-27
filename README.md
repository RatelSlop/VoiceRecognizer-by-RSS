# 🎙️ VoiceRecognizer by RSS

Een geavanceerde, modulaire Discord voice bot die meeluistert in spraakkanalen en automatisch een geluidje (sound effect) afspeelt zodra iemand (of specifiek je vriend) een bepaald trefwoord zegt 

---

## ✨ Features

- **⚡ Real-time Spraakherkenning**: 
  - **Google Speech Recognition** (standaard, ultrasnel ~0,4s en 100% accurate Nederlandse spraakherkenning).
  - **OpenAI Whisper Base** (lokale fallback).
  - **Vosk** (offline Kaldi fallback).
- **🛡️ Discord DAVE E2EE Ondersteuning**: Volledig compatibel met Discord's nieuwste DAVE E2EE voice protocol (geen 4017 errors of Opus crashes).
- **🎙️ Geavanceerde VAD & AGC**: Automatische versterking van zachte microfoons (Automatic Gain Control) en slimme pauzedetectie zodat woorden nooit half worden afgekapt.
- **🎯 Vriend-Targeting**: Laat de bot reageren op **iedereen** in de call, of stel specifiek je vriend in via `/target set @vriend` zodat de bot alleen bij hem afgaat!
- **➕ Flexibele Trefwoorden**: Voeg op elk gewenst moment nieuwe trefwoorden toe via Discord commando's (`/keyword add ...`) of via `config.json`.
- **🔊 Aparte Geluiden per Trefwoord**: Koppel verschillende trefwoorden aan verschillende geluiden (bijv. `"hoi"` ➔ `ploep.mp3`).
- **🎵 Eenvoudig Eigen Geluiden Toevoegen**: Plaats simpelweg jouw eigen `.mp3` of `.wav` bestanden in de map `sounds/`.
- **🖥️ Cross-Platform & Pterodactyl Ready**: Draait soepel op zowel Windows als Linux.

---

## 🚀 Snelle Start

### 1. Bot Token Configureren
Maak een bestand genaamd `.env` aan (of kopieer `.env.example`):
```env
DISCORD_TOKEN=jouw_discord_bot_token_hier
```

### 2. Discord Bot Permissies
Zorg ervoor dat jouw bot in het [Discord Developer Portal](https://discord.com/developers/applications):
1. Onder **Bot**:
   - **Privileged Gateway Intents**: Vink `Message Content Intent` aan.
2. In de OAuth2 URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Embed Links`.

### 3. De Bot Starten (Windows)
Dubbelklik op `start_bot.bat` of draai:
```powershell
pip install -r requirements.txt
python bot.py
```

### 4. Draaien op Pterodactyl
1. Upload alle bestanden naar de server via de Pterodactyl File Manager.
2. Zorg dat `requirements.txt` geïnstalleerd wordt.
3. Stel het startbestand in op `bot.py`.
4. De bot gebruikt slechts ~70MB - 120MB RAM!

---

## 🕹️ Commando's in Discord

| Commando | Beschrijving |
| :--- | :--- |
| **`/join`** | Laat de bot meedoen in jouw huidige spraakkanaal en activeert het luisteren. |
| **`/leave`** | Laat de bot het kanaal verlaten en stopt met luisteren. |
| **`/status`** | Toont actuele status, actieve trefwoorden, target persoon en volume. |
| **`/debug`** | Toont realtime audio diagnostics en de laatst door de bot gehoorde woorden. |
| **`/keyword add`** | Voeg een nieuw trefwoord toe (bijv. `/keyword add woord:bro geluid:ploep.mp3`). |
| **`/keyword remove`** | Verwijder een trefwoord (met handige autocomplete). |
| **`/keyword list`** | Geeft een overzicht van alle ingestelde trefwoorden en hun gekoppelde geluiden. |
| **`/target set`** | Stel een specifieke vriend in (bijv. `/target set gebruiker:@Kees`). |
| **`/target clear`** | Reset het filter, zodat de bot weer naar iedereen luistert. |
| **`/target view`** | Bekijk wie er momenteel gemonitord wordt. |
| **`/sounds`** | Geeft een lijst van alle beschikbare geluiden in de `sounds/` map. |
| **`/test_sound`** | Speel direct een geluid af in het kanaal om je volume en geluiden te testen. |
| **`/settings`** | Pas het volume, cooldown, chatnotificaties of de herkennings-engine aan. |

---

## ⚙️ Configuratie (`config.json`)

```json
{
  "keywords": {
    "hoi": "ploep.mp3",
    "kaas": "ploep.mp3",
    "bro": "ploep.mp3"
  },
  "default_sound": "ploep.mp3",
  "target_user_ids": [],
  "cooldown_seconds": 1.5,
  "volume": 0.85,
  "stt_engine": "google",
  "notify_in_chat": true
}
```

---

## 🎧 Eigen Geluiden Toevoegen
1. Open de map: `sounds/`
2. Plaats je gewenste `.mp3` of `.wav` geluidsfragmenten in deze map.
3. Koppel ze via Discord met `/keyword add woord:test geluid:jouwgeluid.mp3` of pas `config.json` aan.
