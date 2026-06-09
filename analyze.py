#!/usr/bin/env python3
import os, time, sqlite3, tempfile, requests, numpy as np

try:
    import essentia.standard as es
    ESSENTIA_OK = True
except ImportError:
    print("Essentia not available - metadata only mode")
    ESSENTIA_OK = False

try:
    from langdetect import detect as detect_lang
    LANG_OK = True
except ImportError:
    LANG_OK = False

SC_API      = "https://api-v2.soundcloud.com"
CLIENT_ID   = os.environ.get("SC_CLIENT_ID", "")
OAUTH_TOKEN = os.environ.get("SC_OAUTH_TOKEN", "")
MIN_PLAYS   = 10_000
MAX_PER_RUN = 500
DB_PATH     = "tracks.sqlite"

# Only use genre slugs that SC charts API actually accepts
GENRES = [
    "soundcloud:genres:all-music",
    "soundcloud:genres:electronic",
    "soundcloud:genres:hiphoprap",
    "soundcloud:genres:rbsoul",
    "soundcloud:genres:pop",
    "soundcloud:genres:alternativerock",
    "soundcloud:genres:ambient",
    "soundcloud:genres:danceedm",
    "soundcloud:genres:deephouse",
    "soundcloud:genres:house",
    "soundcloud:genres:techno",
    "soundcloud:genres:trance",
    "soundcloud:genres:trap",
    "soundcloud:genres:drumbass",
    "soundcloud:genres:dubstep",
    "soundcloud:genres:indie",
    "soundcloud:genres:rock",
    "soundcloud:genres:metal",
    "soundcloud:genres:classical",
    "soundcloud:genres:jazzblues",
    "soundcloud:genres:reggae",
    "soundcloud:genres:latin",
    "soundcloud:genres:dancehall",
    "soundcloud:genres:triphop",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Authorization": f"OAuth {OAUTH_TOKEN}",
})

def sc_get(url, params=None, retries=3):
    p = dict(params or {})
    p["client_id"] = CLIENT_ID
    if not url.startswith("http"):
        url = SC_API + url
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=p, timeout=30)
            if r.status_code == 404:
                return None  # skip silently, genre might not exist
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"    Rate limited - waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"    Request failed: {e}")
                return None
            time.sleep(5 * (attempt + 1))
    return None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            sc_id            TEXT PRIMARY KEY,
            title            TEXT,
            artist           TEXT,
            artwork_url      TEXT,
            sc_url           TEXT,
            genre            TEXT,
            tags             TEXT,
            play_count       INTEGER,
            likes_count      INTEGER,
            duration         INTEGER,
            language         TEXT,
            bpm              REAL,
            key_note         TEXT,
            energy           REAL,
            loudness         REAL,
            danceability     REAL,
            valence          REAL,
            acousticness     REAL,
            instrumentalness REAL,
            speechiness      REAL,
            brightness       REAL,
            warmth           REAL,
            mfcc_0  REAL, mfcc_1  REAL, mfcc_2  REAL, mfcc_3  REAL,
            mfcc_4  REAL, mfcc_5  REAL, mfcc_6  REAL, mfcc_7  REAL,
            mfcc_8  REAL, mfcc_9  REAL, mfcc_10 REAL, mfcc_11 REAL,
            mfcc_12 REAL,
            analyzed_at      INTEGER
        )
    """)
    conn.commit()
    return conn

def pick_transcoding(track):
    for t in track.get("media", {}).get("transcodings", []):
        fmt = t.get("format", {})
        if fmt.get("protocol") == "progressive" and "mpeg" in fmt.get("mime_type", ""):
            return t["url"]
    for t in track.get("media", {}).get("transcodings", []):
        if t.get("format", {}).get("protocol") == "hls":
            return t["url"]
    return None

def get_cdn_url(transcoding_url):
    r = sc_get(transcoding_url)
    return r.get("url") if r else None

def download_segment(cdn_url):
    try:
        if ".m3u8" in cdn_url:
            r = requests.get(cdn_url, timeout=20)
            r.raise_for_status()
            segments = [l.strip() for l in r.text.splitlines()
                        if l.strip() and not l.startswith("#")]
            data = bytearray()
            base = cdn_url.rsplit("/", 1)[0] + "/"
            for seg in segments[:3]:
                if not seg.startswith("http"):
                    seg = base + seg
                sr = requests.get(seg, timeout=20)
                data.extend(sr.content)
            return bytes(data)
        else:
            r = requests.get(cdn_url, stream=True, timeout=20,
                             headers={"Range": "bytes=0-500000"})
            return r.content
    except Exception as e:
        print(f"    Download error: {e}")
        return None

def analyze_audio(audio_bytes):
    if not ESSENTIA_OK or not audio_bytes:
        return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name

        audio = es.MonoLoader(filename=tmp, sampleRate=44100)()
        os.unlink(tmp); tmp = None

        if len(audio) < 44100:
            return None
        audio = audio[:44100 * 30]

        bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
        key, scale, _   = es.KeyExtractor()(audio)

        energy      = float(es.Energy()(audio))
        energy_norm = min(1.0, energy / max(len(audio) * 0.001, 1))
        loudness    = float(es.Loudness()(audio))

        danceability, _ = es.Danceability()(audio)
        danceability    = float(danceability)

        windowing  = es.Windowing(type="hann")
        spectrum   = es.Spectrum()
        mfcc_algo  = es.MFCC(numberCoefficients=13)
        centroid   = es.SpectralCentroidTime()
        zcr_algo   = es.ZeroCrossingRate()
        pitch_algo = es.PitchSalience()

        mfccs, centroids, zcrs, pitches = [], [], [], []
        for frame in es.FrameGenerator(audio, frameSize=2048, hopSize=1024):
            spec = spectrum(windowing(frame))
            _, coeffs = mfcc_algo(spec)
            mfccs.append(coeffs)
            centroids.append(centroid(frame))
            zcrs.append(zcr_algo(frame))
            pitches.append(pitch_algo(spec))

        if not mfccs:
            return None

        mfcc_avg   = np.mean(mfccs, axis=0).tolist()
        brightness = min(1.0, float(np.mean(centroids)) / 22050.0)
        zcr_mean   = float(np.mean(zcrs))
        pitch_mean = float(np.mean(pitches))

        valence          = min(1.0, (0.6 if scale == "major" else 0.3) + energy_norm * 0.3)
        acousticness     = max(0.0, min(1.0, 1.0 - brightness - zcr_mean * 5))
        instrumentalness = max(0.0, min(1.0, 1.0 - pitch_mean))
        speechiness      = min(1.0, zcr_mean * 8)

        return {
            "bpm": float(bpm), "key": f"{key} {scale}",
            "energy": energy_norm, "loudness": loudness,
            "danceability": danceability, "valence": valence,
            "acousticness": acousticness, "instrumentalness": instrumentalness,
            "speechiness": speechiness,
            "brightness": brightness, "warmth": 1.0 - brightness,
            "mfcc": mfcc_avg,
        }
    except Exception as e:
        print(f"    Essentia error: {e}")
        if tmp:
            try: os.unlink(tmp)
            except: pass
        return None

def detect_language(title, description, tags):
    if not LANG_OK:
        return "unknown"
    text = f"{title} {description or ''} {tags or ''}".strip()
    if len(text) < 8:
        return "unknown"
    try:
        return detect_lang(text)
    except:
        return "unknown"

def fetch_charts(genre, kind="trending"):
    result = sc_get("/charts", {"kind": kind, "genre": genre, "limit": 200})
    if not result:
        return []
    tracks = []
    for item in result.get("collection", []):
        track = item.get("track", item)
        if track.get("playback_count", 0) >= MIN_PLAYS:
            tracks.append(track)
    return tracks

def main():
    print("EchoWaves Analyzer")
    print(f"Essentia: {'OK' if ESSENTIA_OK else 'metadata only'} | LangDetect: {'OK' if LANG_OK else 'no'}")

    conn = init_db()
    existing = {r[0] for r in conn.execute("SELECT sc_id FROM tracks")}
    print(f"Existing in DB: {len(existing)}")

    candidates = {}   # new tracks to fully analyze
    stat_updates = {} # existing tracks seen in charts → refresh play/likes counts

    # Trending + Top for all-music (guaranteed to work)
    for kind in ["trending", "top"]:
        print(f"Fetching all-music ({kind})...")
        for t in fetch_charts("soundcloud:genres:all-music", kind):
            sc_id = str(t.get("id", ""))
            if not sc_id:
                continue
            if sc_id in existing:
                stat_updates[sc_id] = t
            else:
                candidates[sc_id] = t
        time.sleep(0.5)

    # Genre-specific charts (skip on 404)
    for genre in GENRES[1:]:  # skip all-music, already fetched
        print(f"Fetching {genre}...")
        tracks = fetch_charts(genre)
        found = 0
        for t in tracks:
            sc_id = str(t.get("id", ""))
            if not sc_id:
                continue
            if sc_id in existing:
                stat_updates[sc_id] = t
            else:
                candidates[sc_id] = t
                found += 1
        if found == 0 and not tracks:
            print(f"  (not available, skipped)")
        time.sleep(0.3)

    # Refresh play_count / likes_count for tracks already in DB
    if stat_updates:
        print(f"Refreshing stats for {len(stat_updates)} existing tracks...")
        for sc_id, t in stat_updates.items():
            conn.execute(
                "UPDATE tracks SET play_count=?, likes_count=?, analyzed_at=? WHERE sc_id=?",
                (
                    t.get("playback_count", 0),
                    t.get("likes_count") or t.get("favoritings_count") or 0,
                    int(time.time()),
                    sc_id,
                )
            )
        conn.commit()
        print("Stats refreshed.")

    to_analyze = sorted(candidates.values(),
                        key=lambda t: t.get("playback_count", 0),
                        reverse=True)[:MAX_PER_RUN]

    print(f"New tracks to analyze: {len(to_analyze)}")

    for i, track in enumerate(to_analyze):
        sc_id  = str(track.get("id", ""))
        title  = track.get("title", "")
        artist = track.get("user", {}).get("username", "")
        print(f"[{i+1}/{len(to_analyze)}] {artist} - {title}")

        features = None
        t_url = pick_transcoding(track)
        if t_url:
            cdn = get_cdn_url(t_url)
            if cdn:
                audio = download_segment(cdn)
                features = analyze_audio(audio)

        if features is None:
            features = {
                "bpm": None, "key": None, "energy": None, "loudness": None,
                "danceability": None, "valence": None, "acousticness": None,
                "instrumentalness": None, "speechiness": None,
                "brightness": None, "warmth": None, "mfcc": [None] * 13,
            }

        tags     = track.get("tag_list", "")
        language = detect_language(title, track.get("description", ""), tags)
        artwork  = (track.get("artwork_url") or
                    track.get("user", {}).get("avatar_url", "") or "")
        if artwork:
            artwork = artwork.replace("-large", "-t500x500")

        mfcc = features.get("mfcc") or [None] * 13

        conn.execute("""
            INSERT OR REPLACE INTO tracks (
                sc_id, title, artist, artwork_url, sc_url,
                genre, tags, play_count, likes_count, duration, language,
                bpm, key_note, energy, loudness, danceability, valence,
                acousticness, instrumentalness, speechiness, brightness, warmth,
                mfcc_0, mfcc_1, mfcc_2, mfcc_3, mfcc_4, mfcc_5, mfcc_6,
                mfcc_7, mfcc_8, mfcc_9, mfcc_10, mfcc_11, mfcc_12,
                analyzed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sc_id, title, artist, artwork,
            track.get("permalink_url", ""),
            track.get("genre", ""), tags,
            track.get("playback_count", 0),
            track.get("likes_count") or track.get("favoritings_count") or 0,
            track.get("duration", 0) // 1000,
            language,
            features["bpm"], features["key"],
            features["energy"], features["loudness"],
            features["danceability"], features["valence"],
            features["acousticness"], features["instrumentalness"],
            features["speechiness"], features["brightness"], features["warmth"],
            *(mfcc[i] if i < len(mfcc) else None for i in range(13)),
            int(time.time()),
        ))
        conn.commit()
        time.sleep(1)

    total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    print(f"\nDone. Total in DB: {total}")
    conn.close()

if __name__ == "__main__":
    main()
