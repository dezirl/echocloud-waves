#!/usr/bin/env python3
import os, re, time, sqlite3, tempfile, unicodedata, difflib, requests, numpy as np

_CYRILLIC = re.compile(r'[Ѐ-ӿ]')

def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC.search(text or ""))

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
MIN_PLAYS    = 5_000
MIN_PLAYS_RU = 500     # lower bar for Russian-language tracks (pinned artists included)
MAX_PER_RUN  = 1_500
MAX_WALL_SECONDS = 4500  # 75 min hard stop so DB uploads before GitHub timeout

# ── Profiles ──────────────────────────────────────────────────────────────────
# Three databases, built by three parallel jobs of the same workflow. Which one
# this run produces comes from WAVES_PROFILE.
#
# "mixed" keeps the original behaviour: charts + keyword searches + the pinned
# list, i.e. a broad net. The other two are CURATED — they crawl nothing but the
# artists named below, so what lands in them is decided by hand, not guessed from
# genre tags (SoundCloud's are unreliable, and "rap" there covers everything).
#
# ↓ To add an artist: put their SoundCloud display name in the right list. Names
#   go through SC search with fuzzy matching, and a weak match is skipped rather
#   than guessed at. If the log reports SKIPPED or an empty profile, paste the
#   full "https://soundcloud.com/<permalink>" URL instead — that path resolves
#   directly and never guesses.
ARTISTS_SC_RAP = [
    # SoundCloud / underground rap
    "королевский XVII", "юпи", "101 poza", "сумка33", "лампочка34",
    "xylary3g", "3umph", "ak0", "allme", "angelgrind", "anonymous ember",
    "aquakey", "Arigameh", "ARLEKIN40000", "athysue", "benjamingotbenz",
    "bluetoothgod", "buldojke", "CLONNEX", "code10", "cowboyclicker",
    "data404", "deathlain", "dope17", "eelijahh", "elox1m", "erdo",
    "euro91", "evilbenz", "fakemink", "FONFORINO", "fortuna812",
    "gitarakuru", "greyrock", "helios812", "HOODGOTH", "ivycraft",
    "kantaa", "kudokushi", "kugakrewceo", "lovecult", "madk1d",
    "morphineee", "nahojku", "negative_creeper", "phreshboyswag",
    "p1gthg", "rmw", "road2god", "rspls", "silver_gloria", "suffocated",
    "TEWIQ", "tuborosho", "vertico", "waunty", "woee33", "yung yreezy",
    "урал гайсин", "БытьРомантикомАхуенно",
    "Дмитрий Уткин",
    "#снялцепи",
    "темный принц", "первый король", "федор яйцо", "хестон", "цццц",
    "голодный", "паранойя",
    "0tune", "excm", "qwy1nx", "3goth2002", "fendiglock",
    "jequya / saikto", "pittkiid", "соня абрикосова", "7ai",
    "pri3rakkr0vj", "@продалдушу", "leilahimikat", "gothcorp", "xe1to",
    "cardinparis", "валюта скуратов", "эра_павших", "hood goth",
    "tenshi", "fleurnothappy", "lilsemmi (Семик)", "хулагу 3g #gcn $vsc",
    "akiko!", "XDdata", "haru matsui", "yandme",
]

ARTISTS_RU_RAP = [
    "OG Buda", "Big Baby Tape", "Kizaru",
    "Aarne", "Toxi$", "Saluki", "MAYOT", "OBLADAET", "Платина",
    "SEEMEE", "SODA LUV", "Кишлак", "ICEGERGERT", "PHARAOH", "MORGENSHTERN",
    "Boulevard Depo", "BATO", "Kai Angel", "9mice", "ANIKV", "Heronwater",
    "Friendly Thug 52 NGG", "163ONMYNECK", "Scally Milano", "Yanix",
    "Bushido Zho", "Alblak 52", "MyDee52", "LOVV66",
    "madk1d", "JDFLAG", "huzzy b",
    "tewiq", "Темный принц",
    "хочуспать", "Slava Marlow",
    "Gone.Fludd",
    "Hofmannita", "Jeembo",
    "Lizer",
    "Mnogoznaal", "Moneyken", "Mozee Montana", "Mr. Bruce", "Murda Killa",
    "Murovei", "Mutabor", "N’Pans", "Natan", "Lovv66", "Matrang",
    "T-Fest", "Xcho", "SLIMUS", "104", "Feduk", "Anikv", "Брутто",
    "Blago White", "Cakeboy", "Coldcloud", "Code80", "DK", "Deep-Ex-Sense",
    "DooMee", "EIGHTEEN", "ERSHOV", "FLESH", "GSPD", "HAFASA", "H1GH",
    "IROH", "JABO", "KADI", "Kavabanga Depo Kolibri", "Krestall / Courier",
    "LILDRUGHILL", "LIL KRYSTALLL", "LIL MORTY", "METAN", "METOX",
    "MOZEE MONTANA", "N1NT3ND0", "OG MINAY", "ONINO", "PLOHOYPAREN",
    "Qurt", "Rakhim", "RAM", "Raskol", "ROCKET", "RYZE", "SAINTLY",
    "SEVEN", "SHAMI", "SOULOUD", "TEMNEE", "THRILL PILL", "TINI LIN",
    "TUMANIYO", "TVETH", "VIA", "VERBEE", "WHITE GALLOWS", "YUNG TRAPPA",
    "ZVEN", "ZLOY", "Baby Cute", "Baby Melo", "Cheeef",
    "Dequine", "Elvira T", "FEDUK", "GONE.Fludd", "Hammali & Navai",
    "JONY",
    "ЛСП", "Эндшпиль", "Янго", "Fendiglock",
]


def _dedupe(names):
    """Drop repeats (case-insensitive) — the lists are hand-maintained, and every
    duplicate costs a search request plus a full catalogue fetch for nothing."""
    seen, out = set(), []
    for n in names:
        k = n.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


ARTISTS_SC_RAP = _dedupe(ARTISTS_SC_RAP)
ARTISTS_RU_RAP = _dedupe(ARTISTS_RU_RAP)

PROFILES = {
    "mixed": {"db": "tracks.sqlite",        "artists": None},
    "screp": {"db": "tracks-screp.sqlite",  "artists": ARTISTS_SC_RAP},
    "rurap": {"db": "tracks-rurap.sqlite",  "artists": ARTISTS_RU_RAP},
}

PROFILE = os.environ.get("WAVES_PROFILE", "mixed").strip().lower()
if PROFILE not in PROFILES:
    raise SystemExit(f"Unknown WAVES_PROFILE={PROFILE!r}; expected one of {sorted(PROFILES)}")

CURATED_ARTISTS = PROFILES[PROFILE]["artists"]
DB_PATH      = PROFILES[PROFILE]["db"]

# Pinned artists — always crawled regardless of charts position
PINNED_RU_ARTISTS = [
    # ── Russian / CIS ──────────────────────────────────────────────────────────
    "MACAN", "Miyagi", "Andy Panda", "Баста", "OG Buda", "Big Baby Tape",
    "Bushido Zho", "Kizaru", "Friendly Thug 52 Ngg", "Markul", "Aarne",
    "Toxi$", "Saluki", "MAYOT", "OBLADAET", "Yanix", "LSP", "Boulevard Depo",
    "Платина", "Heronwater", "LOVV66", "SEEMEE", "THRILL PILL", "SODA LUV",
    "104", "Кишлак", "WHITE GALLOWS", "ICEGERGERT", "Jakone", "A.V.G",
    "SCIRENA", "PHARAOH", "FACE", "MORGENSHTERN", "Элджей", "Feduk",
    "Скриптонит", "Truwer", "Niman", "Jah Khalib", "Ramil'", "Xcho",
    "Navai", "HammAli", "MOT", "Гуф", "Slimus", "Птаха", "Кравц",
    "Noize MC", "Oxxxymiron", "ATL", "Horus", "Заточка", "Рем Дигга",
    "Каста", "Влади", "Шым", "Змей", "Murovei", "GONE.Fludd", "Rocket",
    "Tveth", "JEEMBO", "Basic Boy", "LIZER", "ST", "Loc-Dog", "T-Fest",
    "Mnogoznaal", "Pyrokinesis", "Дора", "МЭЙБИ БЭЙБИ", "CMH", "DK",
    "Mzlff", "Паша Техник", "Слава КПСС", "Замай", "Грязный Рамирес",
    "Velial Squad", "Хаски", "Nkeeei", "Uniqe", "ARTEM SHILOVETS",
    "Aikko", "MATRANG", "N1NT3ND0", "Каспийский Груз", "ВесЪ", "Брутто",
    "TEMNEE", "Lil Krystalll", "Asik", "JANAGA", "RAIKAHO", "Goro",
    "i61", "CAKEBOY", "Tanya Tekis", "Thomas Mraz", "OFFMi", "Dopeclvb",
    "FORTUNA812", "Madk1d", "Темный принц", "excm", "QWY1NX", "3goth2002",
    "снялцепи", "эрапавших", "0tune", "zer0tune", "ноль", "Тимати",
    "Клава Кока", "Егор Крид", "9mice", "VIPERR", "Kai Angel", "Zavet",
    "Pittkiid", "Fendiglock", "Урал Гайсин",

    # ── Hip-Hop / Rap (US) ─────────────────────────────────────────────────────
    "Drake", "Kendrick Lamar", "Travis Scott", "J. Cole", "Future",
    "Lil Baby", "Lil Uzi Vert", "Playboi Carti", "21 Savage", "Gunna",
    "Young Thug", "Roddy Ricch", "A$AP Rocky", "Tyler, the Creator",
    "Metro Boomin", "Don Toliver", "NBA YoungBoy", "Polo G", "Lil Durk",
    "Jack Harlow", "Cardi B", "Nicki Minaj", "Megan Thee Stallion",
    "City Girls", "Quavo", "Offset", "Takeoff", "Chance the Rapper",
    "Mac Miller", "Logic", "Big Sean", "Wiz Khalifa", "Kid Cudi",
    "Juice WRLD", "Lil Peep", "XXXTentacion", "Post Malone", "Witt Lowry",
    "NF", "Eminem", "Kanye West", "Jay-Z", "Nas", "50 Cent",

    # ── Pop / R&B ──────────────────────────────────────────────────────────────
    "The Weeknd", "Billie Eilish", "Ariana Grande", "Dua Lipa",
    "Harry Styles", "Olivia Rodrigo", "Doja Cat", "SZA", "H.E.R.",
    "Summer Walker", "Jhené Aiko", "Daniel Caesar", "Frank Ocean",
    "Bruno Mars", "Charlie Puth", "Ed Sheeran", "Sam Smith",
    "Shawn Mendes", "Justin Bieber", "Selena Gomez", "Taylor Swift",
    "Beyoncé", "Rihanna", "Lady Gaga", "Adele", "Lizzo",

    # ── Electronic / Dance ─────────────────────────────────────────────────────
    "Calvin Harris", "Marshmello", "Illenium", "Kygo", "Odesza",
    "Flume", "Kaytranada", "Disclosure", "Four Tet", "Jamie xx",
    "Fred Again..", "Skrillex", "Diplo", "Zedd", "Martin Garrix",
    "Deadmau5", "Eric Prydz", "Solomun", "Fisher", "Chris Lake",
    "John Summit", "Dom Dolla", "Lane 8", "Petit Biscuit", "Monolink",
    "Bicep", "Bonobo", "Nicolas Jaar", "Caribou", "Jon Hopkins",

    # ── Alternative / Indie / Rock ─────────────────────────────────────────────
    "Arctic Monkeys", "Radiohead", "The 1975", "Tame Impala",
    "Clairo", "Rex Orange County", "Phoebe Bridgers", "Hozier",
    "Glass Animals", "Alt-J", "Vampire Weekend", "Foster the People",
    "Lana Del Rey", "Halsey", "Paramore", "Twenty One Pilots",
    "Imagine Dragons", "Coldplay", "Linkin Park", "Panic! at the Disco",

    # ── Lo-Fi / Chill ──────────────────────────────────────────────────────────
    "Nujabes", "J Dilla", "Madlib", "Flying Lotus", "Knxwledge",
    "Sango", "Kiefer", "Mndsgn", "Louis Cole", "Thundercat",
]

# Fast lookup set — everything before the international section
_ru_split = PINNED_RU_ARTISTS.index("Drake")
PINNED_RU_SET = {n.lower() for n in PINNED_RU_ARTISTS[:_ru_split]}

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

HITMOS_BASE = "https://rus.hitmos.fm"

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

def hitmos_search(artist, title):
    """Search hitmos.fm for an alternative audio URL when SC stream is unavailable."""
    query = f"{artist} {title}".strip()
    try:
        r = requests.get(
            f"{HITMOS_BASE}/search",
            params={"q": query},
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ru,en;q=0.8",
            },
        )
        if r.status_code != 200:
            return None
        html = r.text
        matches = re.findall(r'/get/music/[^"<\s]+\.mp3', html)
        if not matches:
            return None
        tokens = set(query.lower().replace("-", " ").replace("_", " ").split())
        def score(href):
            h = href.lower().replace("_", " ").replace("-", " ")
            return sum(1 for tok in tokens if tok in h)
        best = max(matches, key=score)
        return HITMOS_BASE + best
    except Exception:
        return None

def hitmos_download(url):
    """Download first 500 KB from a hitmos CDN URL (follows 302 redirect)."""
    try:
        r = requests.get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            stream=True,
        )
        if r.status_code == 200 and "audio" in r.headers.get("content-type", ""):
            data = bytearray()
            for chunk in r.iter_content(65536):
                data.extend(chunk)
                if len(data) >= 500_000:
                    break
            return bytes(data)
        return None
    except Exception:
        return None

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

        bpm = float(es.PercivalBpmEstimator(sampleRate=44100)(audio))
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

def detect_language(title, description, tags, artist=""):
    # Pinned RU artist → always Russian
    if artist and artist.lower() in PINNED_RU_SET:
        return "ru"
    # Cyrillic characters in title/tags → Russian
    if has_cyrillic(f"{title} {tags or ''}"):
        return "ru"
    if not LANG_OK:
        return "unknown"
    text = f"{title} {description or ''} {tags or ''}".strip()
    if len(text) < 8:
        return "unknown"
    try:
        lang = detect_lang(text)
        # langdetect often confuses short CIS text as 'ro', 'bg', 'mk' etc.
        # If title has any Cyrillic treat as ru regardless
        if lang in ("ro", "bg", "mk", "sr", "uk", "be") and has_cyrillic(title):
            return "ru"
        return lang
    except:
        return "unknown"


def fix_ru_language_in_db(conn):
    """Patch existing DB rows: force language='ru' for pinned RU artists and Cyrillic titles."""
    print("Patching language field for RU tracks in DB...")
    updated = 0

    # Pass 1: artist name in PINNED_RU_SET (case-insensitive)
    # SQLite LOWER() is ASCII-only so compare in Python
    rows = conn.execute(
        "SELECT sc_id, artist, title, tags FROM tracks WHERE language != 'ru'"
    ).fetchall()

    to_fix = []
    for sc_id, artist, title, tags in rows:
        if artist and artist.lower() in PINNED_RU_SET:
            to_fix.append(sc_id)
        elif has_cyrillic(f"{title or ''} {tags or ''}"):
            to_fix.append(sc_id)

    if to_fix:
        conn.executemany(
            "UPDATE tracks SET language='ru' WHERE sc_id=?",
            [(sc_id,) for sc_id in to_fix]
        )
        conn.commit()
        updated = len(to_fix)

    print(f"  Fixed {updated} tracks → language='ru'")

def fetch_charts(genre, kind="trending", max_pages=8):
    params = {"kind": kind, "genre": genre, "limit": 100}
    tracks = []
    url = "/charts"
    cur_params = params

    for page in range(max_pages):
        result = sc_get(url, cur_params)
        if not result:
            break
        for item in result.get("collection", []):
            track = item.get("track", item)
            if (track.get("playback_count") or 0) >= MIN_PLAYS:
                tracks.append(track)
        next_href = result.get("next_href")
        if not next_href:
            break
        url = next_href
        cur_params = {}
        if page < max_pages - 1:
            time.sleep(0.4)
    return tracks


def search_tracks(query, genre_tag=None, limit=100, min_plays=None):
    """Extra source: keyword/genre search to discover tracks beyond charts."""
    params = {"q": query, "limit": limit, "filter.duration": "medium"}
    if genre_tag:
        params["genres"] = genre_tag
    result = sc_get("/search/tracks", params)
    if not result:
        return []
    threshold = min_plays if min_plays is not None else MIN_PLAYS
    tracks = []
    for t in result.get("collection", []):
        if (t.get("playback_count") or 0) >= threshold and t.get("streamable"):
            tracks.append(t)
    return tracks

MATCH_THRESHOLD = 0.72   # below this a search hit is treated as "not the artist"

def norm_name(s):
    """Fold a display name down to something comparable: case, accents, ё/е, and
    the decoration people put in SoundCloud names (#tags, $, emoji, spacing)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ё", "е").replace("’", "'")
    s = re.sub(r"[^0-9a-zа-я]+", "", s)
    return s

def name_variants(name):
    """Query forms to try, most specific first. Hand-typed names carry junk the
    search index doesn't have: a collab slash, a parenthetical, a crew tag."""
    out = []
    def push(v):
        v = v.strip(" -_/|")
        if v and v not in out:
            out.append(v)
    push(name)
    push(re.sub(r"\s*\([^)]*\)", "", name))            # "lilsemmi (Семик)" -> "lilsemmi"
    if "/" in name:                                     # "jequya / saikto" -> both halves
        for part in name.split("/"):
            push(part)
    push(re.sub(r"[#$@][^\s]*", "", name))              # "хулагу 3g #gcn $vsc" -> "хулагу 3g"
    push(re.sub(r"[#@]", "", name))                     # "#снялцепи" -> "снялцепи"
    return out

def score_user(target_norm, user):
    """How well a search result matches what we asked for. Permalink counts too —
    it's the stable identity, display names get renamed."""
    best = 0.0
    for field in (user.get("username"), user.get("permalink"), user.get("full_name")):
        cand = norm_name(field)
        if not cand:
            continue
        if cand == target_norm:
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, target_norm, cand).ratio())
    return best

def find_artist_id(name, verbose=True):
    """Resolve an artist entry to a SoundCloud user id.

    Accepts a display name, a permalink slug, or a full profile URL. A weak match
    is REJECTED rather than accepted: taking search's first result silently filled
    the database with the wrong artist, which is worse than a gap you can see in
    the log and fix in the list.
    """
    entry = (name or "").strip()
    if not entry:
        return None

    # Explicit profile URL / "soundcloud.com/slug" — no guessing needed.
    m = re.search(r"soundcloud\.com/([^/\s?#]+)", entry)
    if m:
        r = sc_get("/resolve", {"url": f"https://soundcloud.com/{m.group(1)}"})
        if r and r.get("kind") == "user" and r.get("id"):
            return r["id"]
        if verbose:
            print(f"  [{name}] URL did not resolve to a user")
        return None

    target = norm_name(entry)
    best_user, best_score, seen_ids = None, 0.0, {}

    for variant in name_variants(entry):
        result = sc_get("/search/users", {"q": variant, "limit": 10})
        for u in (result or {}).get("collection", []):
            if not u.get("id"):
                continue
            s = score_user(target, u)
            seen_ids[u["id"]] = (u, max(s, seen_ids.get(u["id"], (None, 0))[1]))
            if s > best_score:
                best_user, best_score = u, s
        if best_score >= 0.999:
            break
        time.sleep(0.25)

    if best_user and best_score >= MATCH_THRESHOLD:
        if verbose and best_score < 0.999:
            print(f"  [{name}] ~ matched \"{best_user.get('username')}\" "
                  f"(/{best_user.get('permalink')}, {best_score:.2f})")
        return best_user["id"]

    if verbose:
        if not seen_ids:
            print(f"  [{name}] NOT FOUND — no search results. Check the spelling, "
                  f"or put the profile URL in the list instead.")
        else:
            top = sorted(seen_ids.values(), key=lambda x: -x[1])[:3]
            opts = ", ".join(f"{u.get('username')} (/{u.get('permalink')}, {s:.2f})" for u, s in top)
            print(f"  [{name}] SKIPPED — best match too weak ({best_score:.2f}). "
                  f"Candidates: {opts}")
            print(f"      -> if one of these is right, replace the name in the list with "
                  f"its soundcloud.com/<permalink> URL")
    return None

def fetch_user_tracks(user_id, limit=100, min_plays=None):
    """Fetch top tracks for a specific user — used to pull Russian artist catalogues.

    min_plays=0 takes the whole catalogue: for a hand-picked artist the play count
    says nothing we care about, the name was already the filter.
    """
    result = sc_get(f"/users/{user_id}/tracks", {"limit": limit, "linked_partitioning": 1})
    if not result:
        return []
    threshold = MIN_PLAYS_RU if min_plays is None else min_plays
    tracks = []
    for t in result.get("collection", []):
        if (t.get("playback_count") or 0) >= threshold and t.get("streamable"):
            tracks.append(t)
    return tracks

def search_ru_artists(query, limit=10):
    """Search for Russian artists by keyword and return their user IDs."""
    result = sc_get("/search/users", {"q": query, "limit": limit})
    if not result:
        return []
    return [u["id"] for u in result.get("collection", []) if u.get("id")]

def main():
    print("EchoWaves Analyzer")
    print(f"Essentia: {'OK' if ESSENTIA_OK else 'metadata only'} | LangDetect: {'OK' if LANG_OK else 'no'}")

    conn = init_db()
    fix_ru_language_in_db(conn)
    existing = {r[0] for r in conn.execute("SELECT sc_id FROM tracks")}
    print(f"Existing in DB: {len(existing)}")

    candidates = {}   # new tracks to fully analyze
    stat_updates = {} # existing tracks seen in charts → refresh play/likes counts

    # Curated profile: crawl ONLY the named artists. No charts, no keyword search —
    # the whole point of these databases is that their contents were chosen, so
    # letting discovery bleed in would defeat it.
    if CURATED_ARTISTS is not None:
        print(f"Profile {PROFILE}: resolving {len(CURATED_ARTISTS)} curated artists...")
        # Resolve every name BEFORE crawling, so the report of what failed is one
        # block at the top of the log instead of scattered through an hour of output.
        resolved, unresolved = [], []
        seen_uids = {}
        for name in CURATED_ARTISTS:
            uid = find_artist_id(name)
            if not uid:
                unresolved.append(name)
                continue
            if uid in seen_uids:
                # Two spellings of one profile — crawling twice would double the calls.
                print(f"  [{name}] same profile as \"{seen_uids[uid]}\", skipping")
                continue
            seen_uids[uid] = name
            resolved.append((name, uid))
            time.sleep(0.2)

        print(f"Resolved {len(resolved)}/{len(CURATED_ARTISTS)}"
              + (f", {len(unresolved)} unresolved: {', '.join(unresolved)}" if unresolved else ""))
        if not resolved:
            raise SystemExit(f"Profile {PROFILE}: not a single artist resolved — "
                             f"check SC_CLIENT_ID / SC_OAUTH_TOKEN before blaming the list.")

        print(f"Crawling {len(resolved)} artists...")
        empty = []
        for name, uid in resolved:
            found = 0
            total = 0
            for t in fetch_user_tracks(uid, limit=200, min_plays=0):
                total += 1
                sc_id = str(t.get("id", ""))
                if not sc_id:
                    continue
                if sc_id in existing:
                    stat_updates[sc_id] = t
                else:
                    candidates[sc_id] = t
                    found += 1
            if total == 0:
                empty.append(name)
            print(f"  [{name}] -> {found} new / {total} on profile")
            time.sleep(0.4)

        # An artist that resolved but has no streamable tracks is usually a wrong
        # match (a listener account with the same handle), so surface it too.
        if empty:
            print(f"WARNING: resolved but empty (likely wrong profile): {', '.join(empty)}")

    if CURATED_ARTISTS is None:
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

        # ── Pinned RU artists — guaranteed to be crawled ────────────────────────────
        # De-duplicate list preserving order
        seen_names = set()
        unique_pinned = [n for n in PINNED_RU_ARTISTS if not (n in seen_names or seen_names.add(n))]
        print(f"Crawling {len(unique_pinned)} pinned RU artists...")
        for name in unique_pinned:
            uid = find_artist_id(name)
            if not uid:
                print(f"  [{name}] not found")
                continue
            tracks_found = 0
            for t in fetch_user_tracks(uid, limit=100):
                sc_id = str(t.get("id", ""))
                if not sc_id:
                    continue
                if sc_id in existing:
                    stat_updates[sc_id] = t
                else:
                    candidates[sc_id] = t
                    tracks_found += 1
            print(f"  [{name}] → {tracks_found} new tracks")
            time.sleep(0.4)

        # RU keyword searches — primary focus, lower play threshold
        RU_QUERIES = [
            ("хип хоп бит", "hiphoprap"),
            ("лирический рэп", "hiphoprap"),
            ("трэп бит", "trap"),
            ("дип хаус", "deephouse"),
            ("техно музыка", "techno"),
            ("хаус музыка", "house"),
            ("транс музыка", "trance"),
            ("электронная музыка", "electronic"),
            ("атмосферная музыка", "ambient"),
            ("русский r&b", "rbsoul"),
            ("ритм энд блюз", "rbsoul"),
            ("инди рок россия", "indie"),
            ("russian lo-fi", None),
            ("moscow hip hop", None),
            ("russian electronic", "electronic"),
            ("russian trap", "trap"),
        ]
        for query, tag in RU_QUERIES:
            print(f"Searching RU: '{query}'...")
            for t in search_tracks(query, tag, min_plays=MIN_PLAYS_RU):
                sc_id = str(t.get("id", ""))
                if not sc_id:
                    continue
                if sc_id in existing:
                    stat_updates[sc_id] = t
                else:
                    candidates[sc_id] = t
            time.sleep(0.4)

        # EN keyword searches (supplementary)
        EN_QUERIES = [
            ("deep house mix", "deephouse"),
            ("hip hop 2024", "hiphoprap"),
            ("electronic music", "electronic"),
            ("lofi chill beats", None),
            ("trap beat", "trap"),
            ("ambient soundscape", "ambient"),
            ("techno set", "techno"),
            ("drum and bass", "drumbass"),
            ("indie rock", "indie"),
        ]
        for query, tag in EN_QUERIES:
            print(f"Searching: '{query}'...")
            for t in search_tracks(query, tag):
                sc_id = str(t.get("id", ""))
                if not sc_id:
                    continue
                if sc_id in existing:
                    stat_updates[sc_id] = t
                else:
                    candidates[sc_id] = t
            time.sleep(0.4)

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

    def ru_priority_key(t):
        plays  = (t.get("playback_count") or 0)
        artist = (t.get("user", {}).get("username") or "").lower()
        tags   = (t.get("tag_list") or "").lower()
        genre  = (t.get("genre") or "").lower()
        is_ru_artist = artist in PINNED_RU_SET or any(a in artist for a in PINNED_RU_SET)
        is_ru_meta   = any(w in tags or w in genre for w in ["россия", "russian", "рус", "ru"])
        score = 2 if is_ru_artist else (1 if is_ru_meta else 0)
        return (score, plays)

    to_analyze = sorted(candidates.values(), key=ru_priority_key, reverse=True)[:MAX_PER_RUN]

    print(f"New tracks to analyze: {len(to_analyze)}")

    wall_start = time.time()
    for i, track in enumerate(to_analyze):
        if time.time() - wall_start > MAX_WALL_SECONDS:
            print(f"  Wall-time limit reached at track {i+1}/{len(to_analyze)}, stopping.")
            break
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

        if features is None and artist and title:
            hm_url = hitmos_search(artist, title)
            if hm_url:
                print(f"  → hitmos fallback: {hm_url.split('/')[-1]}")
                hm_audio = hitmos_download(hm_url)
                if hm_audio:
                    features = analyze_audio(hm_audio)

        if features is None:
            features = {
                "bpm": None, "key": None, "energy": None, "loudness": None,
                "danceability": None, "valence": None, "acousticness": None,
                "instrumentalness": None, "speechiness": None,
                "brightness": None, "warmth": None, "mfcc": [None] * 13,
            }

        tags     = track.get("tag_list", "")
        language = detect_language(title, track.get("description", ""), tags, artist)
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