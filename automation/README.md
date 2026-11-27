# Daily readings and homily

Google Cloud Run job `voiceagent-daily-homily` in `voiceagent-ios-dj-20261112`, `us-west1`.
Cloud Scheduler starts it daily at **04:00 America/Los_Angeles**, including weekends. This follows Pacific daylight saving changes and does not depend on the Mac.

The job obtains the dated main readings from the official Syro-Malabar Commission for Liturgy calendar. It generates an original 1,350–1,450-word English homily and a natural Malayalam adaptation, then records both with **Gemini 3.8 Flash TTS**. Malayalam uses Google's native Malayalam male library voice `ml-in-concierge-1`, described in Google's catalog as a low-pitched, slightly husky man in his mid-fifties from Kozhikode. Its delivery prompt requests warm, thoughtful senior Syro-Malabar priestly narration. This is an AI voice, not a recording or impersonation of a real priest. English uses Leda. Voice IDs, reader labels, and delivery instructions are in [narration.json](narration.json). The complete Scripture passages play first. Malayalam Scripture uses the licensed Malayalam 1910 contemporary-orthography edition (TFBF, CC BY-SA 4.0); English uses the public-domain World English Bible in Catholic book order. These are study translations, not a claim to reproduce the official Malayalam POC lectionary wording. Attribution accompanies the reading files and feed. A missing official calendar, unsupported verse range, or unavailable licensed passage stops publication rather than substituting invented readings.

The scheduled writing prompt is [homily_prompt.txt](homily_prompt.txt). It uses the pastoral perspective of a Kerala Syro-Malabar priest and Bible expert with forty years of experience without claiming real clerical authorship or invented ministry experiences. The homily chapter in each language has a **hard ten-minute maximum; the preceding Bible readings are additional**. The publisher measures actual narration, tightens an overlong script and regenerates its audio when necessary, and checks the encoded MP3 chapter before publishing. It retains the 1,350–1,450-word English requirement. It never cuts off the recording mid-sentence. The metadata records the prompt hash, homily duration, and final word count.

The Malayalam Scripture text and the corresponding synthesized reading are shared under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); the adaptation adds spoken introductions and Google AI narration. When a calendar reference specifies part of a verse (a/b/c), the study recording includes the entire affected verse, with this expansion disclosed in its reading text. The official reference remains in the metadata. Both licensed editions were checked against every primary assignment from November 27 through December 31, 2026. The Malayalam edition lacks deuterocanonical books; a future assignment requiring one will stop until a suitable licensed text is provided.

Each date has two combined MP3s, two homily texts, two reading texts, two Podcasting 2.0 chapter JSON files, and one metadata JSON. The metadata includes the measured homily start, actual voice/model, recording SHA-256, Scripture provenance, and generation time. Feed enclosure revisions let VoiceAgent replace a re-recorded episode on the same date. The iPhone caches audio and the skip point together for offline and locked-screen playback.

The job makes normal commits containing only that day's generated files and feed/index updates. It preserves repository history and does not execute the old toolkit's repository-reset recipe. A private GCS lease prevents concurrent daily publishers. Existing complete episodes are skipped unless `--force` is explicitly supplied.

Secrets are injected at runtime: the existing `voiceagent-openai-key` and a repository-only GitHub deploy key stored as `voiceagent-homily-github`. No credentials belong in this repository. The runtime service account supplies Google model authorization. The scheduler can invoke only this job through its job-level grant.

```sh
python -m unittest test_publisher -v
python publisher.py --check
python publisher.py --publish
```

For a deliberate replacement, run the job with `--publish --force --date YYYY-MM-DD`. Never run `--force` on a repeating schedule. A local preview may use `--site /path/to/checkout --reuse-text --force` without `--publish`.

To re-record only Malayalam, use `--publish --force --reuse-text --language Malayalam --date YYYY-MM-DD`. This verifies and preserves the English audio checksum and metadata, reuses the existing homily scripts, and regenerates Malayalam reading/chapter metadata with the new recording. A script can be tightened if required to meet the ten-minute homily limit.

If a previous external 4:54 AM routine is still active, pause it when this scheduler takes over; otherwise that older publisher could overwrite these recordings and the feed.
