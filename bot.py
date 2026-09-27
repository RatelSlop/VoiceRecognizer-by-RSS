"""
bot.py - Discord Bot voor Keyword Sound Triggers in Voice Calls
Luistert naar spraakkanalen en speelt automatisch een geluid af wanneer
een specifiek trefwoord (zoals 'kanker') wordt uitgesproken.
"""

import os
import sys
import json
import time
import logging
import asyncio
import re
from typing import Dict, Any, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, voice_recv
from dotenv import load_dotenv

from audio_sink import KeywordAudioSink
from voice_patch import apply_voice_patches

# Activeer voice patches voor Discord DAVE protocol en packet router stabiliteit
apply_voice_patches()

# Console output direct tonen
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# Logging configuratie (Console + bot.log)
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BOT_DIR, "bot.log")

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

console_h = logging.StreamHandler(sys.stdout)
console_h.setFormatter(log_formatter)

file_h = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_h.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[console_h, file_h])
logger = logging.getLogger("KeywordBot")

# Laad omgevingsvariabelen
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BOT_DIR, ".env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CONFIG_FILE = os.path.join(BOT_DIR, "config.json")
SOUNDS_DIR = os.path.join(BOT_DIR, "sounds")

# Zorg dat de sounds directory bestaat
os.makedirs(SOUNDS_DIR, exist_ok=True)

# -------------------------------------------------------------
# Opus DLL Initialisatie (Essentieel voor Windows Voice)
# -------------------------------------------------------------
def init_opus():
    if discord.opus.is_loaded():
        return

    candidate_paths = [
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so",
        os.path.join(BOT_DIR, "opus.dll"),
        r"C:\Program Files\FreeCAD 1.0\bin\opus.dll",
        r"C:\Windows\System32\opus.dll",
        r"C:\Windows\SysWOW64\opus.dll",
        "opus.dll",
        "libopus-0.dll"
    ]

    for path in candidate_paths:
        if os.path.exists(path) or path.endswith(".dll"):
            try:
                discord.opus.load_opus(path)
                if discord.opus.is_loaded():
                    logger.info(f"Opus bibliotheek succesvol geladen vanaf: {path}")
                    return
            except Exception:
                continue

    logger.warning("Waarschuwing: Opus kon niet geladen worden. Spraakfuncties werken mogelijk niet.")

init_opus()

# -------------------------------------------------------------
# Configuratie Beheer
# -------------------------------------------------------------
DEFAULT_CONFIG = {
    "keywords": {
        "kanker": "kanker.mp3",
        "hoi": "ploep.mp3",
        "kaas": "ploep.mp3",
        "bro": "ploep.mp3"
    },
    "default_sound": "ploep.mp3",
    "target_user_ids": [],
    "cooldown_seconds": 3.0,
    "volume": 0.85,
    "stt_engine": "google",
    "notify_in_chat": True
}

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Voeg ontbrekende default keys toe
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        logger.error(f"Fout bij lezen van {CONFIG_FILE}: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]) -> None:
    global config
    config = cfg
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Fout bij opslaan van {CONFIG_FILE}: {e}")

config = load_config()

def get_current_config() -> Dict[str, Any]:
    global config
    config = load_config()
    return config

def get_available_sounds() -> List[str]:
    """Haalt alle audiobestanden op uit de sounds/ map."""
    valid_exts = (".wav", ".mp3", ".ogg", ".m4a", ".flac")
    if not os.path.exists(SOUNDS_DIR):
        return []
    return [f for f in os.listdir(SOUNDS_DIR) if f.lower().endswith(valid_exts)]

# -------------------------------------------------------------
# Discord Bot Client Setup
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Track actieve sinks en tekstkanalen voor notificaties
active_sinks: Dict[int, KeywordAudioSink] = {}
active_text_channels: Dict[int, discord.TextChannel] = {}


def play_sound_in_vc(guild_id: int, sound_file: str) -> bool:
    """Speelt een audiobestand af in het spraakkanaal van de guild."""
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return False

    vc = guild.voice_client
    if not vc.is_connected() or vc.is_playing():
        return False

    sound_path = os.path.join(SOUNDS_DIR, sound_file)
    if not os.path.exists(sound_path):
        # Fallback naar default sound
        sound_path = os.path.join(SOUNDS_DIR, config.get("default_sound", "buzzer.wav"))
        if not os.path.exists(sound_path):
            sounds = get_available_sounds()
            if sounds:
                sound_path = os.path.join(SOUNDS_DIR, sounds[0])
            else:
                logger.error("Geen enkel geluidsbestand gevonden in de sounds/ map!")
                return False

    try:
        vol = float(config.get("volume", 0.85))
        audio_source = discord.FFmpegPCMAudio(sound_path)
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=vol)
        vc.play(volume_source)
        return True
    except Exception as e:
        logger.error(f"Fout bij afspelen van geluid: {e}")
        return False


def on_keyword_detected(user: discord.User, keyword: str, sound_file: str):
    """Callback wanneer een trefwoord gehoord is in een voice channel."""
    # Deze functie wordt aangeroepen vanuit een worker thread,
    # dus we plannen de actie in op de asyncio event loop van de bot.
    asyncio.run_coroutine_threadsafe(
        _handle_keyword_async(user, keyword, sound_file),
        bot.loop
    )


async def _handle_keyword_async(user: discord.User, keyword: str, sound_file: str):
    guild = None
    if isinstance(user, discord.Member):
        guild = user.guild
    else:
        # Zoek in welke guild de gebruiker met de bot in een voice call zit
        for g in bot.guilds:
            if g.voice_client and g.voice_client.is_connected():
                if user in g.voice_client.channel.members:
                    guild = g
                    break

    if not guild:
        return

    # Speel het geluid af
    played = play_sound_in_vc(guild.id, sound_file)
    if not played:
        return

    # Optioneel notificatiebericht sturen
    if config.get("notify_in_chat", True):
        text_channel = active_text_channels.get(guild.id)
        if text_channel:
            embed = discord.Embed(
                title="🚨 Trefwoord Gedetecteerd!",
                description=f"**{user.display_name}** zei **'{keyword}'**!\n🔊 Geluid afgespeeld: `{sound_file}`",
                color=discord.Color.red()
            )
            embed.set_footer(text="Keyword Voice Bot")
            try:
                await text_channel.send(embed=embed)
            except Exception:
                pass


# -------------------------------------------------------------
# Slash Commands Autocompletion
# -------------------------------------------------------------
async def sound_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    sounds = get_available_sounds()
    return [
        app_commands.Choice(name=s, value=s)
        for s in sounds if current.lower() in s.lower()
    ][:25]


async def keyword_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    kws = list(config.get("keywords", {}).keys())
    return [
        app_commands.Choice(name=k, value=k)
        for k in kws if current.lower() in k.lower()
    ][:25]


# -------------------------------------------------------------
# Slash Commands
# -------------------------------------------------------------
@bot.tree.command(name="join", description="Laat de bot meedoen in je spraakkanaal en start met luisteren.")
@app_commands.describe(kanaal="Optioneel spraakkanaal (standaard het kanaal waar jij in zit)")
async def cmd_join(interaction: discord.Interaction, kanaal: Optional[discord.VoiceChannel] = None):
    target_channel = kanaal
    if not target_channel:
        if interaction.user.voice and interaction.user.voice.channel:
            target_channel = interaction.user.voice.channel
        else:
            await interaction.response.send_message(
                "❌ Je zit niet in een spraakkanaal! Ga eerst in een kanaal zitten of kies een kanaal met de optie.",
                ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=False)

    guild_id = interaction.guild_id
    current_vc = interaction.guild.voice_client

    if current_vc and current_vc.is_connected():
        if current_vc.channel.id == target_channel.id:
            await interaction.followup.send(f"✅ Ik zit al in **{target_channel.name}** en luister mee!")
            return
        else:
            await current_vc.move_to(target_channel)
    else:
        try:
            vc = await target_channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            await interaction.followup.send(f"❌ Kon niet verbinden met spraakkanaal: {e}")
            return

    # Sla tekstkanaal op voor eventuele meldingen
    if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
        active_text_channels[guild_id] = interaction.channel

    # Start AudioSink
    vc = interaction.guild.voice_client
    if vc and hasattr(vc, "listen"):
        if hasattr(vc, "is_listening") and vc.is_listening():
            vc.stop_listening()

        sink = KeywordAudioSink(
            bot=bot,
            config=config,
            on_keyword_detected=on_keyword_detected,
            get_config_func=get_current_config
        )
        active_sinks[guild_id] = sink
        vc.listen(sink)

    kws = list(config.get("keywords", {}).keys())
    kw_str = ", ".join([f"`{k}`" for k in kws]) if kws else "Geen"
    engine = config.get("stt_engine", "vosk").upper()

    embed = discord.Embed(
        title="🎙️ Verbonden en aan het luisteren!",
        description=f"De bot luistert nu in **{target_channel.name}**.",
        color=discord.Color.green()
    )
    embed.add_field(name="Actieve Trefwoorden", value=kw_str, inline=False)
    embed.add_field(name="Spraakherkenning Engine", value=f"`{engine}`", inline=True)
    embed.add_field(name="Standaard Geluid", value=f"`{config.get('default_sound', 'buzzer.wav')}`", inline=True)

    targets = config.get("target_user_ids", [])
    if targets:
        target_mentions = [f"<@{uid}>" for uid in targets]
        embed.add_field(name="Target Vriend(en)", value=", ".join(target_mentions), inline=False)
    else:
        embed.add_field(name="Target", value="Iedereen in call (behalve bots)", inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="leave", description="Laat de bot het spraakkanaal verlaten en stop met luisteren.")
async def cmd_leave(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client

    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ Ik zit momenteel in geen enkel spraakkanaal.", ephemeral=True)
        return

    # Schoon de sink op
    sink = active_sinks.pop(guild_id, None)
    if sink:
        sink.cleanup()

    if hasattr(vc, "stop_listening") and vc.is_listening():
        vc.stop_listening()

    await vc.disconnect()
    active_text_channels.pop(guild_id, None)

    await interaction.response.send_message("👋 Spraakkanaal verlaten en gestopt met luisteren!")


@bot.tree.command(name="status", description="Bekijk de actuele status en instellingen van de bot.")
async def cmd_status(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    is_connected = vc and vc.is_connected()
    is_listening = vc and hasattr(vc, "is_listening") and vc.is_listening()

    embed = discord.Embed(
        title="🤖 Keyword Voice Bot Status",
        color=discord.Color.blue()
    )

    if is_connected:
        status_text = f"🟢 Verbonden met **{vc.channel.name}**"
        if is_listening:
            status_text += " (Aan het luisteren 🎙️)"
    else:
        status_text = "🔴 Niet verbonden met een spraakkanaal"

    embed.add_field(name="Verbinding", value=status_text, inline=False)
    embed.add_field(name="Spraak Engine", value=f"`{config.get('stt_engine', 'vosk').upper()}`", inline=True)
    embed.add_field(name="Volume", value=f"`{int(config.get('volume', 0.85) * 100)}%`", inline=True)
    embed.add_field(name="Cooldown", value=f"`{config.get('cooldown_seconds', 3.0)}s`", inline=True)

    # Keywords lijst
    kws = config.get("keywords", {})
    if kws:
        kw_lines = [f"• **{k}** -> `{s}`" for k, s in kws.items()]
        embed.add_field(name=f"Trefwoorden ({len(kws)})", value="\n".join(kw_lines), inline=False)
    else:
        embed.add_field(name="Trefwoorden", value="*Geen trefwoorden geconfigureerd*", inline=False)

    # Target gebruikers
    targets = config.get("target_user_ids", [])
    if targets:
        mentions = [f"<@{uid}>" for uid in targets]
        embed.add_field(name="Target Vriend(en)", value=", ".join(mentions), inline=False)
    else:
        embed.add_field(name="Target", value="Iedereen in call", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="debug", description="Toont live diagnostische data: audio packets, microfoon en laatste spraak.")
async def cmd_debug(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client
    is_connected = vc and vc.is_connected()
    is_listening = vc and hasattr(vc, "is_listening") and vc.is_listening()
    sink = active_sinks.get(guild_id)

    embed = discord.Embed(
        title="🔍 Voice & Audio Diagnostics",
        color=discord.Color.orange()
    )

    if not is_connected:
        embed.description = "❌ De bot is momenteel niet verbonden met een spraakkanaal. Gebruik `/join`."
    else:
        channel_name = vc.channel.name if vc.channel else "Onbekend"
        members = [m for m in vc.channel.members if not m.bot] if vc.channel else []
        member_names = ", ".join([m.display_name for m in members]) or "Niemand"
        embed.add_field(name="Kanaal", value=f"`{channel_name}`\nSprekers: {member_names}", inline=False)
        embed.add_field(name="Luisterstatus", value="🟢 Actief" if is_listening else "🔴 Inactief", inline=True)

        if sink:
            now = time.time()
            sec_since = f"{int(now - sink.last_packet_time)}s geleden" if sink.last_packet_time > 0 else "Nog niets"
            embed.add_field(name="Ontvangen Pakketjes", value=f"`{sink.total_packets_received}`", inline=True)
            embed.add_field(name="Laatste Audio", value=sec_since, inline=True)
            embed.add_field(name="Actieve Workers", value=f"`{len(sink.user_threads)}`", inline=True)
            last_text = sink.last_transcription or "*Nog geen spraak gedetecteerd*"
            embed.add_field(name="Laatst Gehoorde Woorden", value=f"```\n{last_text}\n```", inline=False)
            last_kw = sink.last_detected_keyword or "*Geen*"
            embed.add_field(name="Laatst Getriggerd", value=f"`{last_kw}`", inline=True)
        else:
            embed.add_field(name="Sink", value="Geen actieve AudioSink geregistreerd", inline=False)

    await interaction.response.send_message(embed=embed)


# -------------------------------------------------------------
# Keyword Commando's (Beheer van trefwoorden)
# -------------------------------------------------------------
keyword_group = app_commands.Group(name="keyword", description="Beheer de trefwoorden en gekoppelde geluiden")

@keyword_group.command(name="add", description="Voeg een nieuw trefwoord toe met een geluid of upload direct een mp3.")
@app_commands.describe(
    woord="Het woord waarop de bot moet reageren (bijv. 'kanker', 'bro', 'kut')",
    geluid="Kies een bestaand geluid uit de lijst (optioneel)",
    bestand="Upload direct een nieuw geluidsbestand (.mp3, .wav, etc.) (optioneel)"
)
@app_commands.autocomplete(geluid=sound_autocomplete)
async def cmd_keyword_add(
    interaction: discord.Interaction,
    woord: str,
    geluid: Optional[str] = None,
    bestand: Optional[discord.Attachment] = None
):
    clean_word = woord.strip().lower()
    if not clean_word:
        await interaction.response.send_message("❌ Ongeldig woord opgegeven.", ephemeral=True)
        return

    valid_exts = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
    chosen_sound = None

    # Optie 1: Er is direct een audiobestand meegestuurd
    if bestand:
        ext = os.path.splitext(bestand.filename)[1].lower()
        if ext not in valid_exts:
            await interaction.response.send_message(
                f"❌ Ongeldig bestandstype (`{ext}`). Alleen {', '.join(valid_exts)} zijn toegestaan!",
                ephemeral=True
            )
            return

        if bestand.size > 15 * 1024 * 1024:
            await interaction.response.send_message("❌ Het audiobestand is te groot (max 15MB)!", ephemeral=True)
            return

        base_name = os.path.splitext(bestand.filename)[0]
        safe_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_name).strip('_') or f"sound_{clean_word}"
        filename = f"{safe_base}{ext}"
        dest_path = os.path.join(SOUNDS_DIR, filename)

        try:
            await bestand.save(dest_path)
            chosen_sound = filename
            logger.info(f"Nieuw audiobestand geüpload via /keyword add: {dest_path}")
        except Exception as e:
            await interaction.response.send_message(f"❌ Fout bij opslaan van bestand: {e}", ephemeral=True)
            return

    # Optie 2: Er is een bestaand geluid gekozen
    elif geluid:
        sounds = get_available_sounds()
        if geluid not in sounds:
            await interaction.response.send_message(
                f"⚠️ Geluid `{geluid}` niet gevonden in `sounds/`. Beschikbare geluiden: {', '.join(sounds) or 'Geen'}",
                ephemeral=True
            )
            return
        chosen_sound = geluid

    # Optie 3: Standaard geluid
    else:
        chosen_sound = config.get("default_sound", "ploep.mp3")

    config["keywords"][clean_word] = chosen_sound
    save_config(config)

    embed = discord.Embed(
        title="✅ Trefwoord Toegevoegd!",
        description=f"Als iemand **'{clean_word}'** zegt, speelt de bot `{chosen_sound}` af.",
        color=discord.Color.green()
    )
    if bestand:
        embed.add_field(name="Nieuw Geluid Geüpload", value=f"`{chosen_sound}` ({bestand.size // 1024} KB)", inline=False)
    else:
        embed.add_field(name="Gekoppeld Geluid", value=f"`{chosen_sound}`", inline=False)

    await interaction.response.send_message(embed=embed)


@keyword_group.command(name="remove", description="Verwijder een bestaand trefwoord.")
@app_commands.describe(woord="Het woord dat verwijderd moet worden")
@app_commands.autocomplete(woord=keyword_autocomplete)
async def cmd_keyword_remove(interaction: discord.Interaction, woord: str):
    clean_word = woord.strip().lower()
    if clean_word in config.get("keywords", {}):
        del config["keywords"][clean_word]
        save_config(config)
        await interaction.response.send_message(f"🗑️ Trefwoord **'{clean_word}'** is verwijderd!", ephemeral=False)
    else:
        await interaction.response.send_message(f"❌ Trefwoord **'{clean_word}'** staat niet in de lijst.", ephemeral=True)


@keyword_group.command(name="list", description="Bekijk alle actieve trefwoorden en hun geluiden.")
async def cmd_keyword_list(interaction: discord.Interaction):
    kws = config.get("keywords", {})
    if not kws:
        await interaction.response.send_message("Er zijn momenteel geen trefwoorden ingesteld.", ephemeral=True)
        return

    lines = [f"• **{k}** ➔ `{s}`" for k, s in kws.items()]
    embed = discord.Embed(
        title="📋 Actieve Trefwoorden",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Totaal: {len(kws)} trefwoord(en)")
    await interaction.response.send_message(embed=embed)

bot.tree.add_command(keyword_group)


# -------------------------------------------------------------
# Target Commando's (Alleen luisteren naar specifieke vriend)
# -------------------------------------------------------------
target_group = app_commands.Group(name="target", description="Stel in naar wie de bot moet luisteren")

@target_group.command(name="set", description="Stel een specifieke vriend in naar wie de bot moet luisteren.")
@app_commands.describe(gebruiker="De vriend die gemonitord moet worden")
async def cmd_target_set(interaction: discord.Interaction, gebruiker: discord.Member):
    config["target_user_ids"] = [gebruiker.id]
    save_config(config)

    embed = discord.Embed(
        title="🎯 Target Vriend Ingesteld!",
        description=f"De bot zal nu **alleen** reageren als **{gebruiker.mention}** een trefwoord zegt!",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


@target_group.command(name="clear", description="Zorg dat de bot weer naar iedereen in het kanaal luistert.")
async def cmd_target_clear(interaction: discord.Interaction):
    config["target_user_ids"] = []
    save_config(config)

    embed = discord.Embed(
        title="👥 Target Gereset",
        description="De bot luistert nu weer naar **iedereen** in het spraakkanaal (behalve bots).",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)


@target_group.command(name="view", description="Bekijk wie er momenteel getarget wordt.")
async def cmd_target_view(interaction: discord.Interaction):
    targets = config.get("target_user_ids", [])
    if not targets:
        await interaction.response.send_message("👥 De bot luistert naar **iedereen** in de call.")
    else:
        mentions = [f"<@{uid}>" for uid in targets]
        await interaction.response.send_message(f"🎯 De bot luistert specifiek naar: {', '.join(mentions)}")

bot.tree.add_command(target_group)


# -------------------------------------------------------------
# Audio & Geluid Test Commando's
# -------------------------------------------------------------
@bot.tree.command(name="test_sound", description="Test direct een geluid in het spraakkanaal.")
@app_commands.describe(geluid="Het geluid dat je wilt testen")
@app_commands.autocomplete(geluid=sound_autocomplete)
async def cmd_test_sound(interaction: discord.Interaction, geluid: Optional[str] = None):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ De bot moet eerst in een spraakkanaal zitten (`/join`)!", ephemeral=True)
        return

    chosen_sound = geluid if geluid else config.get("default_sound", "buzzer.wav")
    sounds = get_available_sounds()
    if chosen_sound not in sounds:
        await interaction.response.send_message(f"❌ Geluid `{chosen_sound}` niet gevonden.", ephemeral=True)
        return

    played = play_sound_in_vc(interaction.guild_id, chosen_sound)
    if played:
        await interaction.response.send_message(f"🔊 Geluid `{chosen_sound}` wordt afgespeeld!", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ Kon geluid niet afspelen (is de bot al iets aan het afspelen?).", ephemeral=True)


@bot.tree.command(name="sounds", description="Bekijk alle beschikbare geluidsbestanden in de sounds/ map.")
async def cmd_sounds(interaction: discord.Interaction):
    sounds = get_available_sounds()
    if not sounds:
        await interaction.response.send_message("Er zijn nog geen geluidsbestanden geplaatst in de `sounds/` map.", ephemeral=True)
        return

    lines = [f"• `{s}`" for s in sorted(sounds)]
    embed = discord.Embed(
        title="🎵 Beschikbare Geluiden",
        description="\n".join(lines),
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"Plaats eigen .mp3 of .wav bestanden in de 'sounds/' map!")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="upload_sound", description="Upload een nieuw geluidsbestand (.mp3, .wav) naar de bot.")
@app_commands.describe(bestand="Het audiobestand dat je wilt uploaden (.mp3, .wav, etc.)")
async def cmd_upload_sound(interaction: discord.Interaction, bestand: discord.Attachment):
    valid_exts = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
    ext = os.path.splitext(bestand.filename)[1].lower()
    if ext not in valid_exts:
        await interaction.response.send_message(
            f"❌ Ongeldig bestandstype (`{ext}`). Alleen {', '.join(valid_exts)} zijn toegestaan!",
            ephemeral=True
        )
        return

    if bestand.size > 15 * 1024 * 1024:
        await interaction.response.send_message("❌ Het audiobestand is te groot (max 15MB)!", ephemeral=True)
        return

    base_name = os.path.splitext(bestand.filename)[0]
    safe_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_name).strip('_') or "sound"
    filename = f"{safe_base}{ext}"
    dest_path = os.path.join(SOUNDS_DIR, filename)

    try:
        await bestand.save(dest_path)
        logger.info(f"Nieuw audiobestand opgeslagen via /upload_sound: {dest_path}")
        embed = discord.Embed(
            title="🎵 Geluid Succesvol Geüpload!",
            description=f"Bestand **`{filename}`** is opgeslagen in de sounds map!\nJe kunt dit geluid nu koppelen met `/keyword add` of uittesten met `/test_sound`.",
            color=discord.Color.purple()
        )
        embed.add_field(name="Bestandsnaam", value=f"`{filename}`", inline=True)
        embed.add_field(name="Grootte", value=f"`{bestand.size // 1024} KB`", inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fout bij opslaan van bestand: {e}", ephemeral=True)


# -------------------------------------------------------------
# Instellingen Commando
# -------------------------------------------------------------
@bot.tree.command(name="settings", description="Pas instellingen van de bot aan.")
@app_commands.describe(
    engine="Kies de spraakherkenning engine (vosk = lokaal & snel, google = cloud)",
    volume="Volume tussen 0.1 en 2.0 (standaard 0.85)",
    cooldown="Cooldown in seconden tussen geluiden (standaard 3.0)",
    notificaties="Stuur een chatbericht wanneer een trefwoord gehoord wordt"
)
@app_commands.choices(engine=[
    app_commands.Choice(name="Whisper (OpenAI, beste accuratesse & ruisonderdrukking)", value="whisper"),
    app_commands.Choice(name="Google Speech (Cloud, snel & hoge accuratesse)", value="google"),
    app_commands.Choice(name="Vosk (Lokaal, realtime streaming)", value="vosk")
])
async def cmd_settings(
    interaction: discord.Interaction,
    engine: Optional[app_commands.Choice[str]] = None,
    volume: Optional[float] = None,
    cooldown: Optional[float] = None,
    notificaties: Optional[bool] = None
):
    changed = []
    if engine:
        config["stt_engine"] = engine.value
        changed.append(f"Spraak Engine ➔ `{engine.value}`")
    if volume is not None:
        v = max(0.05, min(2.0, volume))
        config["volume"] = v
        changed.append(f"Volume ➔ `{int(v * 100)}%`")
    if cooldown is not None:
        cd = max(0.5, min(60.0, cooldown))
        config["cooldown_seconds"] = cd
        changed.append(f"Cooldown ➔ `{cd}s`")
    if notificaties is not None:
        config["notify_in_chat"] = notificaties
        changed.append(f"Chat Notificaties ➔ `{'Aan' if notificaties else 'Uit'}`")

    if changed:
        save_config(config)
        embed = discord.Embed(
            title="⚙️ Instellingen Aangepast",
            description="\n".join(changed),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("Geen wijzigingen opgegeven.", ephemeral=True)


# -------------------------------------------------------------
# Event Handlers
# -------------------------------------------------------------
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Detecteer of de bot zelf ontkoppeld wordt of alleen achterblijft."""
    if member.id == bot.user.id and before.channel and not after.channel:
        guild_id = member.guild.id
        sink = active_sinks.pop(guild_id, None)
        if sink:
            sink.cleanup()
        active_text_channels.pop(guild_id, None)
        logger.info(f"Bot ontkoppeld van spraakkanaal in {member.guild.name}.")


@bot.event
async def on_ready():
    logger.info("=" * 50)
    logger.info(f"Ingelogd als bot: {bot.user.name} ({bot.user.id})")
    logger.info(f"Actieve trefwoorden: {list(config.get('keywords', {}).keys())}")
    logger.info(f"STT Engine: {config.get('stt_engine', 'vosk')}")
    logger.info("=" * 50)

    # Synchroniseer direct naar alle servers waar de bot in zit (geen 1 uur vertraging)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced_guild = await bot.tree.sync(guild=guild)
            logger.info(f"Direct gesynchroniseerd naar server '{guild.name}' ({guild.id}): {len(synced_guild)} commando's.")
        except Exception as e:
            logger.error(f"Fout bij synchroniseren naar guild {guild.name}: {e}")

    try:
        synced = await bot.tree.sync()
        logger.info(f"Globale slash commando's gesynchroniseerd: {len(synced)} commando's actief.")
    except Exception as e:
        logger.error(f"Fout bij synchroniseren van globale slash commando's: {e}")


def main():
    if not DISCORD_TOKEN or "jouw_" in DISCORD_TOKEN.lower():
        print("FOUT: Geen geldige DISCORD_TOKEN gevonden in .env!")
        print("Vul je token in binnen C:\\RatelSlop Studios\\keyword_bot\\.env")
        sys.exit(1)

    print("Keyword Sound Bot wordt opgestart...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
