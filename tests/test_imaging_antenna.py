from bsc_radio.antenna import AntennaClock
from bsc_radio.audio import duration, make_tone
from bsc_radio.db import Database
from bsc_radio.imaging import ElevenLabsCreativeAudioProvider, ImagingProducer
from bsc_radio.providers import MockVoiceProvider
from tests.helpers import settings


class MockCreativeAudio:
    def sound_effect(self, prompt, output, duration_seconds, *, loop=False):
        del prompt, loop
        make_tone(output, seconds=duration_seconds, frequency=660)

    def music(self, prompt, output, duration_seconds):
        del prompt
        make_tone(output, seconds=min(duration_seconds, 2), frequency=330)


def test_elevenlabs_creative_provider_uses_sound_and_music_endpoints(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "elevenlabs_api_key", "protected-test-key")
    calls = []

    class Response:
        content = b"ID3-generated"

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(
        "bsc_radio.imaging.httpx.post",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response(),
    )
    provider = ElevenLabsCreativeAudioProvider(cfg)
    provider.sound_effect("Radio sweep", tmp_path / "sfx.mp3", 3)
    provider.music("Original instrumental", tmp_path / "music.mp3", 60)

    assert calls[0][0].endswith("/v1/sound-generation")
    assert calls[0][1]["json"]["model_id"] == "eleven_text_to_sound_v2"
    assert calls[1][0].endswith("/v1/music")
    assert calls[1][1]["json"]["force_instrumental"] is True
    assert calls[1][1]["json"]["sign_with_c2pa"] is True
    assert all(call[1]["headers"]["xi-api-key"] == "protected-test-key" for call in calls)


def test_imaging_producer_builds_spoken_effect_and_music_assets(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    monkeypatch.setattr(
        cfg.__class__,
        "antenna",
        property(
            lambda self: {
                "clock": ["station_ids", "episode", "stingers", "music"],
                "sound_effects": [
                    {
                        "id": "sweep",
                        "category": "stingers",
                        "prompt": "Sweep",
                        "duration_seconds": 1,
                    }
                ],
                "spoken": [
                    {
                        "id": "ident",
                        "category": "station_ids",
                        "character": "presenter",
                        "text": "Houblon FM.",
                        "bed_prompt": "Bed",
                        "bed_duration_seconds": 2,
                    }
                ],
                "music": [
                    {
                        "id": "track",
                        "title": "Track",
                        "prompt": "Music",
                        "duration_seconds": 2,
                    }
                ],
            }
        ),
    )
    producer = ImagingProducer(cfg, MockVoiceProvider(), MockCreativeAudio())

    result = producer.generate()

    assert len(result["generated"]) == 3
    for relative in ("stingers/sweep.mp3", "station_ids/ident.mp3", "music/track.mp3"):
        path = cfg.data_dir / "antenna" / relative
        assert path.is_file()
        assert duration(path) > 0


def test_antenna_clock_rotates_assets_and_episodes(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    monkeypatch.setattr(
        cfg.__class__,
        "antenna",
        property(lambda self: {"enabled": True, "clock": ["station_ids", "episode", "music"]}),
    )
    db = Database(cfg.database)
    db.migrate()
    ident = cfg.data_dir / "antenna" / "station_ids" / "ident.mp3"
    music = cfg.data_dir / "antenna" / "music" / "track.mp3"
    ident.parent.mkdir(parents=True)
    music.parent.mkdir(parents=True)
    ident.write_bytes(b"ID3-ident")
    music.write_bytes(b"ID3-music")
    episode_id = db.execute(
        "INSERT INTO episode(slug,title,format,station,status) VALUES(?,?,?,?,?)",
        ("journal", "Le Journal", "journal", "brasserie", "audio"),
    )
    audio_id = db.execute(
        "INSERT INTO audio(episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?)",
        (episode_id, str(cfg.data_dir / "audio" / "journal.mp3"), 10, "x"),
    )

    clock = AntennaClock(cfg, db)
    first = clock.next()
    second = clock.next()
    third = clock.next()
    assert [first.kind, second.kind, third.kind] == ["station_ids", "episode", "music"]
    clock.mark_started(second)
    assert db.one("SELECT audio_id FROM airing ORDER BY id DESC LIMIT 1")["audio_id"] == audio_id
    assert clock.now()["title"] == "Le Journal"
