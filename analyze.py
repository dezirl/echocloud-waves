  #!/usr/bin/env python3
  """EchoWaves — builds track features database from SoundCloud charts."""

  import os, sys, time, sqlite3, tempfile, requests, numpy as np

  try:
      import essentia.standard as es
      ESSENTIA_OK = True
  except ImportError:
      print("Essentia not available — metadata only mode")
      ESSENTIA_OK = False

  try:
      from langdetect import detect as detect_lang
      LANG_OK = True
  except ImportError:
      LANG_OK = False

  # ── Config ─────────────────────────────────────────────────────────────────────

  SC_API      = "https://api-v2.soundcloud.com"
  CLIENT_ID   = os.environ.get("SC_CLIENT_ID", "")
  OAUTH_TOKEN = os.environ.get("SC_OAUTH_TOKEN", "")
  MIN_PLAYS   = 10_000
  MAX_PER_RUN = 500   # треков за один запуск (~2-3 часа)
  DB_PATH     = "tracks.sqlite"

  GENRES = [
      "soundcloud:genres:all-music",
      "soundcloud:genres:electronic",
      "soundcloud:genres:hiphoprap",
      "soundcloud:genres:r-b-soul",
      "soundcloud:genres:pop",
      "soundcloud:genres:alternative",
      "soundcloud:genres:ambient",
      "soundcloud:genres:danceedm",
      "soundcloud:genres:deephouse",
      "soundcloud:genres:house",
      "soundcloud:genres:techno-trance",
      "soundcloud:genres:trap",
      "soundcloud:genres:drumbass",
      "soundcloud:genres:dubstep",
      "soundcloud:genres:indie",
      "soundcloud:genres:rock",
      "soundcloud:genres:metal",
      "soundcloud:genres:classical-orchestral",
      "soundcloud:genres:jazz-blues",
      "soundcloud:genres:reggae",
  ]

  # ── HTTP ───────────────────────────────────────────────────────────────────────

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
              if r.status_code == 429:
                  wait = int(r.headers.get("Retry-After", 60))
                  print(f"    Rate limited — waiting {wait}s")
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

  # ── Database ───────────────────────────────────────────────────────────────────

  def init_db():
      conn = sqlite3.connect(DB_PATH)
      conn.execute("""
          CREATE TABLE IF NOT EXISTS tracks (
              sc_id        TEXT PRIMARY KEY,
              title        TEXT,
              artist       TEXT,
              artwork_url  TEXT,
              sc_url       TEXT,
              genre        TEXT,
              tags         TEXT,
              play_count   INTEGER,
              likes_count  INTEGER,
              duration     INTEGER,
              language     TEXT,
              bpm          REAL,
              key_note     TEXT,
              energy       REAL,
              brightness   REAL,
              warmth       REAL,
              mfcc_0  REAL, mfcc_1  REAL, mfcc_2  REAL, mfcc_3  REAL,
              mfcc_4  REAL, mfcc_5  REAL, mfcc_6  REAL, mfcc_7  REAL,
              mfcc_8  REAL, mfcc_9  REAL, mfcc_10 REAL, mfcc_11 REAL,
              mfcc_12 REAL,
              analyzed_at  INTEGER
          )
      """)
      conn.commit()
      return conn

  # ── Audio analysis ─────────────────────────────────────────────────────────────

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

  def download_segment(cdn_url, seconds=30):
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
              # ~500KB ≈ 30s at 128kbps
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

          audio = audio[:44100 * 30]  # max 30 seconds

          # BPM
          bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)

          # Key
          key, scale, _ = es.KeyExtractor()(audio)

          # Energy
          energy = float(es.Energy()(audio))
          energy_norm = min(1.0, energy / max(len(audio) * 0.001, 1))

          # MFCC + spectral centroid (frame-based)
          windowing = es.Windowing(type="hann")
          spectrum   = es.Spectrum()
          mfcc_algo  = es.MFCC(numberCoefficients=13)
          centroid   = es.SpectralCentroidTime()

          mfccs, centroids = [], []
          for frame in es.FrameGenerator(audio, frameSize=2048, hopSize=1024):
              spec = spectrum(windowing(frame))
              _, coeffs = mfcc_algo(spec)
              mfccs.append(coeffs)
              centroids.append(centroid(frame))

          if not mfccs:
              return None

          mfcc_avg   = np.mean(mfccs, axis=0).tolist()
          brightness = min(1.0, float(np.mean(centroids)) / 22050.0)

          return {
              "bpm":        float(bpm),
              "key":        f"{key} {scale}",
              "energy":     energy_norm,
              "brightness": brightness,
              "warmth":     1.0 - brightness,
              "mfcc":       mfcc_avg,
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

  # ── Main ───────────────────────────────────────────────────────────────────────

  def main():
      print(f"EchoWaves Analyzer")
      print(f"Essentia: {'✓' if ESSENTIA_OK else '✗ metadata only'} | "
            f"LangDetect: {'✓' if LANG_OK else '✗'}")

      conn = init_db()
      existing = {r[0] for r in conn.execute("SELECT sc_id FROM tracks")}
      print(f"Existing in DB: {len(existing)}")

      # Collect candidates from all genres
      candidates = {}
      for genre in GENRES:
          print(f"Fetching {genre}...")
          result = sc_get("/charts", {"kind": "trending", "genre": genre, "limit": 200})
          if not result:
              continue
          for item in result.get("collection", []):
              track = item.get("track", item)
              sc_id = str(track.get("id", ""))
              plays = track.get("playback_count", 0)
              if sc_id and plays >= MIN_PLAYS and sc_id not in existing:
                  candidates[sc_id] = track
          time.sleep(0.5)

      # Sort by play_count, take top MAX_PER_RUN
      to_analyze = sorted(candidates.values(),
                          key=lambda t: t.get("playback_count", 0),
                          reverse=True)[:MAX_PER_RUN]

      print(f"New tracks to analyze: {len(to_analyze)}")

      for i, track in enumerate(to_analyze):
          sc_id  = str(track.get("id", ""))
          title  = track.get("title", "")
          artist = track.get("user", {}).get("username", "")
          print(f"[{i+1}/{len(to_analyze)}] {artist} — {title}")

          # Audio analysis
          features = None
          t_url = pick_transcoding(track)
          if t_url:
              cdn = get_cdn_url(t_url)
              if cdn:
                  audio = download_segment(cdn)
                  features = analyze_audio(audio)

          if features is None:
              features = {"bpm": None, "key": None, "energy": None,
                          "brightness": None, "warmth": None, "mfcc": [None]*13}

          tags = track.get("tag_list", "")
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
                  bpm, key_note, energy, brightness, warmth,
                  mfcc_0, mfcc_1, mfcc_2, mfcc_3, mfcc_4, mfcc_5, mfcc_6,
                  mfcc_7, mfcc_8, mfcc_9, mfcc_10, mfcc_11, mfcc_12,
                  analyzed_at
              ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          """, (
              sc_id, title, artist, artwork,
              track.get("permalink_url", ""),
              track.get("genre", ""), tags,
              track.get("playback_count", 0),
              track.get("likes_count") or track.get("favoritings_count") or 0,
              track.get("duration", 0) // 1000,
              language,
              features["bpm"], features["key"],
              features["energy"], features["brightness"], features["warmth"],
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

  .github/workflows/analyze.yml:
  name: EchoWaves Analyzer

  on:
    schedule:
      - cron: '0 3 * * *'   # каждый день в 3:00 UTC
    workflow_dispatch:        # ручной запуск кнопкой

  jobs:
    analyze:
      runs-on: ubuntu-latest
      timeout-minutes: 340    # 5 часов 40 минут

      steps:
        - uses: actions/checkout@v4

        - uses: actions/setup-python@v5
          with:
            python-version: '3.11'

        - name: Install dependencies
          run: pip install essentia requests langdetect numpy

        - name: Download existing DB
          env:
            GH_TOKEN: ${{ github.token }}
          run: |
            gh release download latest-waves \
              --pattern "tracks.sqlite" \
              --output tracks.sqlite 2>/dev/null \
              && echo "Loaded existing DB" \
              || echo "No existing DB — starting fresh"

        - name: Run analyzer
          env:
            SC_CLIENT_ID:   ${{ secrets.SC_CLIENT_ID }}
            SC_OAUTH_TOKEN: ${{ secrets.SC_OAUTH_TOKEN }}
          run: python analyze.py

        - name: Upload updated DB
          env:
            GH_TOKEN: ${{ github.token }}
          run: |
            gh release delete latest-waves --yes 2>/dev/null || true
            gh release create latest-waves tracks.sqlite \
              --title "EchoWaves DB ($(date +%Y-%m-%d))" \
              --notes "Auto-updated track features" \
              --latest
