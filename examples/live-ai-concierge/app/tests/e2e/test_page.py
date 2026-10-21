def test_page_loads(page, server_url):
    page.goto(server_url)
    assert "Concierge" in page.title()


def test_audio_player_in_browser(page, server_url):
    page.goto(server_url)
    res = page.evaluate("""async () => {
        const { AudioPlayer } = await import('/static/js/audio.js');
        const player = new AudioPlayer({ sampleRate: 24000 });

        // Test sample conversion
        const pcm16 = new Int16Array([-32768, 0, 32767]);
        const floats = AudioPlayer.pcm16ToFloat32(pcm16);
        const floatOk = Math.abs(floats[0] - (-1.0)) < 1e-4 && Math.abs(floats[1] - 0.0) < 1e-4 && Math.abs(floats[2] - 1.0) < 1e-4;

        // Test math helpers
        const countOk = AudioPlayer.sampleCount(100, 24000) === 2400;
        const durOk = AudioPlayer.durationMs(4800, 24000) === 100;

        // Test status update and events
        let started = false;
        let ended = false;
        player.onSpeakingStart(() => { started = true; });
        player.onSpeakingEnd(() => { ended = true; });

        // Generate 100ms of 24kHz 16-bit PCM (2400 samples)
        const chunk = new Int16Array(2400);
        for (let i = 0; i < 2400; i++) {
            chunk[i] = Math.round(1000 * Math.sin(2 * Math.PI * 440 * i / 24000));
        }

        player.playChunk(chunk.buffer);
        const stateDuring = document.getElementById('status').getAttribute('data-state');
        const isPlayingDuring = player.isPlaying;

        // Test stop
        player.stop();
        const stateAfterStop = document.getElementById('status').getAttribute('data-state');
        const isPlayingAfterStop = player.isPlaying;

        // Test volume & mute
        player.setVolume(75);
        const volOk = player.volume === 0.75;
        player.setMuted(true);
        const muteOk = player.muted === true;

        return {
            floatOk,
            countOk,
            durOk,
            started,
            ended,
            stateDuring,
            isPlayingDuring,
            stateAfterStop,
            isPlayingAfterStop,
            volOk,
            muteOk,
        };
    }""")
    assert res["floatOk"]
    assert res["countOk"]
    assert res["durOk"]
    assert res["started"]
    assert res["ended"]
    assert res["stateDuring"] == "speaking"
    assert res["isPlayingDuring"] is True
    assert res["stateAfterStop"] == "ready"
    assert res["isPlayingAfterStop"] is False
    assert res["volOk"]
    assert res["muteOk"]


def test_audio_player_playback_drains(page, server_url):
    page.goto(server_url)
    res = page.evaluate("""async () => {
        const { AudioPlayer } = await import('/static/js/audio.js');
        const player = new AudioPlayer({ sampleRate: 24000 });

        let started = false;
        let ended = false;
        player.onSpeakingStart(() => { started = true; });
        player.onSpeakingEnd(() => { ended = true; });

        // 50ms of 24kHz audio = 1200 samples
        const chunk = new Int16Array(1200);
        for (let i = 0; i < 1200; i++) {
            chunk[i] = Math.round(500 * Math.sin(2 * Math.PI * 440 * i / 24000));
        }

        player.playChunk(chunk.buffer);
        const stateDuring = document.getElementById('status').getAttribute('data-state');

        // Wait (up to 3 s under load) for the 50ms playback to finish and onended to trigger
        for (let waited = 0; !ended && waited < 3000; waited += 25) {
            await new Promise(r => setTimeout(r, 25));
        }
        await new Promise(r => setTimeout(r, 25));

        const stateAfterDrain = document.getElementById('status').getAttribute('data-state');
        const isPlayingAfterDrain = player.isPlaying;

        return {
            started,
            ended,
            stateDuring,
            stateAfterDrain,
            isPlayingAfterDrain,
        };
    }""")
    assert res["started"]
    assert res["stateDuring"] == "speaking"
    assert res["ended"]
    assert res["stateAfterDrain"] == "ready"
    assert res["isPlayingAfterDrain"] is False
