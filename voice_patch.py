"""
voice_patch.py - Zorgt voor stabiliteit met Discord Voice en DAVE protocol.
1. Behoudt Discord DAVE protocol version 1 (voorkomt WebSocket error 4017).
2. Ontsleutelt DAVE audio-payloads via DaveSession.decrypt.
3. Beveiligt PacketDecoder en PacketRouter tegen OpusError crashes zodat de bot nooit stopt met luisteren.
"""

import logging
import davey
import discord
from discord.opus import OpusError

logger = logging.getLogger("KeywordBot.VoicePatch")


def apply_voice_patches():
    # 1. Beveilig PacketDecoder tegen crashes en voeg DAVE decryptie toe
    try:
        from discord.ext.voice_recv.opus import PacketDecoder
        orig_decode = PacketDecoder._decode_packet

        def safe_decode_packet(self, packet):
            assert self._decoder is not None
            if packet:
                data = packet.decrypted_data
                vc = self.sink.voice_client

                # Probeer DAVE laag te ontsleutelen indien actief
                if vc and hasattr(vc, "_connection") and getattr(vc._connection, "dave_session", None):
                    dave = vc._connection.dave_session
                    user_id = self._cached_id
                    if not user_id and hasattr(vc, "_get_id_from_ssrc"):
                        user_id = vc._get_id_from_ssrc(self.ssrc)

                    if user_id:
                        try:
                            decrypted = dave.decrypt(user_id, davey.MediaType.audio, data)
                            if decrypted:
                                data = decrypted
                        except Exception:
                            # Als DAVE decryptie nog niet gereed is voor deze spreker
                            pass

                try:
                    pcm = self._decoder.decode(data, fec=False)
                    return packet, pcm
                except OpusError:
                    # Lever stilte-frame op bij tijdelijk onleesbaar pakket (voorkomt router crash)
                    try:
                        pcm = self._decoder.decode(None, fec=False)
                    except Exception:
                        pcm = b'\x00' * 3840
                    return packet, pcm

            return orig_decode(self, packet)

        PacketDecoder._decode_packet = safe_decode_packet
        logger.info("✅ PacketDecoder beveiligd en DAVE decryptie gekoppeld.")
    except Exception as e:
        logger.warning(f"Kon PacketDecoder niet patchen: {e}")

    # 2. Beveilig PacketRouter._do_run tegen ongevalideerde router crashes
    try:
        from discord.ext.voice_recv.router import PacketRouter

        def safe_do_run(self):
            while not self._end_thread.is_set():
                self.waiter.wait()
                with self._lock:
                    for decoder in list(self.waiter.items):
                        try:
                            data = decoder.pop_data()
                            if data is not None:
                                self.sink.write(data.source, data)
                        except Exception as e:
                            logger.debug(f"Pakketje genegeerd in router: {e}")

        PacketRouter._do_run = safe_do_run
        logger.info("✅ PacketRouter beveiligd tegen router-lus crashes.")
    except Exception as e:
        logger.warning(f"Kon PacketRouter niet patchen: {e}")
