#!/usr/bin/env python3
"""Wörter-Harvester: fragt ein OpenAI-kompatibles Modell systematisch nach Wörtern.

Drei Strategien (--mode):
    free    Alphabet durchlaufen, freien Recall pro Buchstabe (mehrere Runden,
            hohe Temperatur -> unterschiedliche Wörter)
    suggest Bekannte Wörter zeigen, Modell darf Vorschläge zum Erweitern machen
    gap     Zwei Nachbarn aus der sortierten Liste nehmen und gezielt fragen,
            welche Wörter alphabetisch DAZWISCHEN liegen (füllt Lücken)

Dazu: eingebauter Startbestand (Seeds pro Buchstabe), siehe --seed.

Konfiguration per Umgebungsvariable:
    MODEL_BASE_URL   z. B. http://home-server:11434/v1  (Ollama)
                                http://home-server:8000/v1  (vLLM)
                                oder leer lassen für api.openai.com
    MODEL_API_KEY    API-Key (bei Ollama egal, aber muss gesetzt sein)
    MODEL_NAME       Modellname, z. B. kolibri-1, llama3.1
    DICT_API_URL     http://127.0.0.1:8000
    DICT_API_KEY     X-API-Key für Schreibzugriffe an die Wörterbuch-API

Aufruf-Beispiele:
    python3 harvester.py --seed --language de
    python3 harvester.py --mode suggest --language de --letters all --rounds 5
    python3 harvester.py --mode gap --language de --letters all
"""
import argparse
import json
import os
import random
import re
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

MODEL_BASE_URL = os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1")
# Cloud-Quota-Fallback: bei 429 (Kontingent aufgebraucht) wechselt chat()
# fuer den Rest dieses Prozesses auf dieses Modell. Die Pipeline setzt es
# nur fuer Sprachen, die das Fallback-Modell beherrscht (de en es fr it tr).
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "")


def post_chat_completions(body: dict, timeout: int) -> dict:
    """POST /chat/completions mit 429-Backoff und Quota-Fallback.

    429-Semantik des Gateways ist zweigeteilt:
    * Lanes (':laptop'/':workstation'/':schleppi'): Modell startet gerade
      -> Backoff-Signal: mit Retry-After/ab 20 s Abstand wiederholen.
    * Cloud (mistral-large-4:cloud & Co.): anhaltende 429 = Kontingent
      aufgebraucht -> nach ~3 Fehlversuchen auf FALLBACK_MODEL umschalten;
      der Prozess bleibt danach prozessweit auf dem Fallback.
    409 (modelhub_busy) wird wie Lane-429 gebackofft, gilt aber NIE als
    Cloud-Quota-Signal (kein Fallback-Trigger).
    Andere Fehler werden sofort weitergeworfen."""
    global MODEL_NAME
    lane_markers = (":laptop", ":workstation", ":schleppi")
    if FALLBACK_MODEL and MODEL_NAME == FALLBACK_MODEL:
        # Prozessweit persistieren: Caller mit eigenem Modell-Parameter
        # (z. B. fix_def_lang) laufen nicht zurueck auf die Cloud.
        body["model"] = FALLBACK_MODEL
    retries = 0
    while True:
        req = urllib.request.Request(
            f"{MODEL_BASE_URL}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {MODEL_API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 409):
                raise
            retries += 1
            _cloud = (e.code == 429
                      and not any(t in str(body.get("model", ""))
                                  for t in lane_markers))
            if (_cloud and FALLBACK_MODEL
                    and body.get("model") != FALLBACK_MODEL and retries >= 3):
                print(f"  Cloud-429 anhaltend — Fallback: {FALLBACK_MODEL}")
                body["model"] = FALLBACK_MODEL
                MODEL_NAME = FALLBACK_MODEL
                continue
            if retries >= 6:
                raise
            wait = 20
            try:
                wait = max(5, int(e.headers.get("Retry-After") or 20))
            except (TypeError, ValueError):
                pass
            time.sleep(wait)
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
# Langsame Lanes (dichte 32B auf CPU: 1–2,5 t/s) brauchen je Aufruf
# Minuten statt Sekunden — pro Lane hochsetzen, z. B. MODEL_TIMEOUT=900.
MODEL_TIMEOUT = int(os.environ.get("MODEL_TIMEOUT", "300"))
DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")

LANG_NAMES = {
    "de": "deutsche",
    "en": "englische",
    "fr": "französische",
    "es": "spanische",
    "it": "italienische",
    "ar": "arabische (Hocharabisch)",
    "tn": "tunesisch-arabische (Derja)",
    "ru": "russische",
    "zh": "chinesische (Mandarin, vereinfachte Schriftzeichen)",
    "ja": "japanische",
    "hi": "Hindi",
    "ko": "koreanische",
    "tr": "türkische",
    "th": "thailändische",
    "bg": "bulgarische",
    "uk": "ukrainische",
}

# Arabisches Alphabet (28 Buchstaben) für free/suggest in ar/tn
AR_LETTERS = list("ابتثجحخدذرزسشصضطظعغفقكلمنهوي")
# Kyrillisches Alphabet (33 Buchstaben) für ru.
RU_LETTERS = list("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
# Gojūon (Basiszeichen; Dakuten/Handakuten werden für den Index gefaltet).
JA_LETTERS = list("あいうえおかきくけこさしすせそたちつてとなにぬねの"
                  "はひふへほまみむめもやゆよらりるれろわをん")
# Devanagari: Vokale + Konsonanten (lange Vokale werden gefaltet).
HI_LETTERS = list("अइउएओकखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह")
# Pinyin-Anfangslaute (zh): Buchstaben-Modus nutzt lateinische
# Anfangsbuchstaben des Pinyin statt eines Zeichen-Alphabets.
ZH_PINYIN_LETTERS = list("abcdefghijklmnopqrstuvwxyz")
# Bulgarisch (kyrillisch, ohne ё; ъ/ь als eigene Indexbuchstaben).
BG_LETTERS = list("абвгдежзийклмнопрстуфхцчшщъьюя")
# Ukrainisch (kyrillisch + і ї є ґ, ohne ё/ъ/ы/э).
UK_LETTERS = list("абвгдеєжзиіїйклмнопрстуфхцчшщьюяґ")
# Türkisch: lateinisches Alphabet (Faltung über TR_FOLD unten).
TR_LETTERS = list(string.ascii_lowercase)
# Koreanisch: 19 Anlaute (Chosŏng). Die Laute stehen nie am Wort —
# Wörter sind Hangul-Silben; der Prompt steuert, der Filter prüft nur
# die Schrift (Zwei-Ebenen-Prüfung wie zh).
KO_INITIALS = list("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ")
# Thailändisch: 44 Anfangskonsonanten (Zwei-Ebenen-Prüfung wie zh).
TH_CONSONANTS = list("กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ")

# Dakuten/Handakuten → stimmlose Basiszeichen (nur Index-Faltung,
# niemals in der gespeicherten Lemmaform).
JA_FOLD = {}
for _base, _voiced in [("か", "がぎぐげご"), ("さ", "ざじずぜぞ"),
                       ("た", "だぢづでど"), ("は", "ばびぶべぼぱぴぷぺぽ")]:
    JA_FOLD.update(zip(_voiced, [_base] * len(_voiced)))
# Devanagari: lange Vokale unter dem Kurzvokal einsortieren.
HI_FOLD = dict(zip("आईऊऐऔ", "अइउएओ"))

# Sprachen, in denen Akzente Diakritika auf dem Basisbuchstaben sind
# (é → e). Im Deutschen sind ä/ö/ü eigene Buchstaben — dort NICHT falten.
ACCENT_FOLD_LANGS = {"en", "fr", "es", "it"}

# Hamza- und Alef-Varianten werden im Arabischen üblicherweise unter
# ا einsortiert, ة nahe ت/h, ى unter ي. NUR für die Index-Sortierung
# (_first_letter) — NIEMALS in der gespeicherten Lemmaform: سأل (fragen)
# und سال (fließen) sind verschiedene Wörter!
AR_FOLD = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي", "ؤ": "و"}

# Türkisch: gepunktetes I und die diakritisierten Buchstaben werden auf
# die Basisbuchstaben gefaltet (ç->c usw.) — nur Index, nie Lemmaform.
TR_FOLD = {"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
           "â": "a", "î": "i", "û": "u"}

# Harakat/Tanwin (U+064B–0652), Dolch-Alif (U+0670), koranische Zeichen
# (U+06D6–06ED) und Tatweel/Kashida (U+0640) — wie in arabischen
# Wörterbüchern üblich wird das Lemma unvokalisiert gespeichert.
# Die Hamza-Schreibweise BLEIBT erhalten (s. AR_FOLD oben).
AR_DIACRITICS = set("ًٌٍَُِّْٖ") | {chr(c) for c in range(0x06D6, 0x06EE)}
AR_TATWEEL = "ـ"


def normalize_ar(word: str) -> str:
    """Kanonicalform für Arabisch/Tunesisch: NFKC, ohne Vokalzeichen und
    Tatweel — Hamza- und Alef-Varianten bleiben unverändert."""
    w = unicodedata.normalize("NFKC", word)
    w = "".join(
        c for c in w
        if c not in AR_DIACRITICS and c != AR_TATWEEL
    )
    return " ".join(w.split())


def _first_letter(word: str, language: str) -> str:
    """Anfangsbuchstabe für den Buchstaben-Filter, ggf. akzentgefaltet."""
    c = word[0].lower()
    if language in ACCENT_FOLD_LANGS:
        c = unicodedata.normalize("NFD", c)[0]
    elif language in ("ar", "tn"):
        c = AR_FOLD.get(c, c)
    elif language == "ja":
        c = JA_FOLD.get(c, c)
    elif language == "hi":
        c = HI_FOLD.get(c, c)
    elif language == "tr":
        c = TR_FOLD.get(c, c)
    elif language == "ru":
        if c == "ё":
            c = "е"
    return c


def _matches_letter(word: str, letter: str, language: str) -> bool:
    """Buchstaben-Filter. Für Chinesisch, Koreanisch und Thailändisch ist
    der 'Buchstabe' ein Pinyin-Anfangslaut bzw. ein Anlaut/Konsonant, der
    sich aus dem Schriftzeichen nicht ableiten lässt — darum dort
    Zwei-Ebenen-Prüfung (Zeichen gültig? + Modell wurde nach X gefragt und
    liefert nur passende Wörter)."""
    if not word:
        return False
    if language == "zh":
        # Nur echte Schriftzeichen annehmen — keine Pinyin-/Lateinwörter,
        # keine Interpunktion. Modell-Prompt sorgt für den Pinyin-Filter.
        return any("一" <= ch <= "鿿" for ch in word)
    if language == "ko":
        # Hangul-Silben (auch ältere Jamo-Kompatibilität)
        return any("가" <= ch <= "힯" or "ㄱ" <= ch <= "ㅎ"
                   for ch in word)
    if language == "th":
        return any("ก" <= ch <= "๛" for ch in word)
    return _first_letter(word, language) == letter

# ---------- Prompts ----------

PROMPT_FREE = (
    "Liste genau {n} {lang} Wörter (Grundformen, Gemeinsprache, keine "
    "Eigennamen, keine Abkürzungen), die mit dem Buchstaben \"{letter}\" "
    "beginnen. NUR Wörter, die in dieser Sprache tatsächlich gebräuchlich "
    "sind — reine Fremdsprachenwörter (z. B. englische Wörter ohne "
    "Duden-Eintrag) und falsch geschriebene Wörter strikt ausschließen. "
    "Substantive orthografisch korrekt (im Deutschen großgeschrieben). "
    "Antworte NUR mit einem JSON-Array von Strings, keine "
    "Erklärung, kein Markdown."
)

PROMPT_SUGGEST = (
    "Hier sind bekannte {lang} Wörter, die mit \"{letter}\" beginnen:\n"
    "{known}\n"
    "Nenne genau {n} weitere {lang} Wörter mit \"{letter}\", die NICHT in "
    "der Liste stehen (Grundformen, Gemeinsprache, keine Eigennamen, nur "
    "tatsächlich in dieser Sprache gebräuchliche Wörter — keine reinen "
    "Fremdsprachenwörter, korrekte Rechtschreibung). "
    "Antworte NUR mit einem JSON-Array von Strings."
)

PROMPT_GAP_AR = (
    "اذكر بالضبط {n} كلمات عربية شائعة (صيغ أساسية، لغة عامية فصيحة، "
    "بدون أسماء علم، بدون اختصارات) تأتي في الترتيب الأبجدي العربي بعد "
    "كلمة \"{a}\" وقبل كلمة \"{b}\". أجب فقط بمصفوفة JSON من النصوص، "
    "بدون شرح وبدون Markdown."
)


PROMPT_GAP = (
    "Welche {lang} Wörter liegen im Wörterbuch-alphabetisch SORTIERT "
    "zwischen \"{a}\" und \"{b}\"? Nenne genau {n} Treffer (Grundformen, "
    "Gemeinsprache, nur tatsächlich gebräuchliche Wörter dieser Sprache, "
    "korrekte Rechtschreibung). Antworte NUR mit einem JSON-Array von Strings."
)

PROMPT_GAP_ZH = (
    "Bekannte chinesische Wörter in Pinyin-Sortierung: \"{a}\" kommt direkt "
    "vor \"{b}\". Welche häufigen chinesischen Wörter (vereinfachte "
    "Schriftzeichen, Grundformen, Alltagssprache) könnten dazwischen fehlen? "
    "Nenne genau {n} Treffer. Antworte NUR mit einem JSON-Array von Strings "
    "(nur die Schriftzeichen, kein Pinyin, kein Markdown)."
)


def _gap_prompt(lang: str, language: str, a: str, b: str,
                per_round: int) -> str:
    """Gap-Prompt: für Arabisch/Tunesisch auf Arabisch fragen — Jais denkt
    bei deutschsprachigen Meta-Fragen zu lange nach und bricht ab."""
    if language in ("ar", "tn"):
        return _with_script(
            PROMPT_GAP_AR.format(lang=lang, a=a, b=b, n=per_round),
            language)
    if language == "zh":
        # Ganze Zeichen statt Pinyin-Platzhaltern in der Nachbarliste —
        # das Modell sortiert selbst nach Pinyin.
        return _with_script(
            PROMPT_GAP_ZH.format(a=a, b=b, n=per_round), language)
    return _with_script(
        PROMPT_GAP.format(lang=lang, a=a, b=b, n=per_round), language)

PROMPT_DEFINE = (
    "Definiere die folgenden {lang} Wörter jeweils in höchstens 15 Wörtern "
    "(Wortart + Kernbedeutung, keine Beispielsätze, keine Ausschmückung). "
    "Fremdwörter und Anglizismen: Herkunftssprache und das Originalwort in "
    "Klammern angeben, z. B. 'Substantiv (aus englisch to bail out …): …'. "
    "Antworte NUR mit einem JSON-Objekt, das jedes Wort auf seine Definition "
    "abbildet, keine Erklärung, kein Markdown.\n"
    "Wörter: {words}"
)

# ---------- Startbestand (Seeds) ----------

SEEDS = {
    "de": {
        "a": ["Apfel", "Arm", "Abend", "Auge"], "b": ["Berg", "Buch", "Baum", "Birne"],
        "c": ["Computer", "Club", "Chemie", "Café"], "d": ["Dorf", "Dach", "Donner", "Decke"],
        "e": ["Eltern", "Essen", "Elefant", "Ecke"], "f": ["Fenster", "Fisch", "Feld", "Frage"],
        "g": ["Garten", "Gras", "Gabel", "Glocke"], "h": ["Haus", "Hand", "Hafen", "Himmel"],
        "i": ["Insel", "Idee", "Instrument", "Igel"], "j": ["Jahr", "Jacke", "Joghurt", "Journal"],
        "k": ["Katze", "Kind", "Kirche", "Kuchen"], "l": ["Lampe", "Land", "Liebe", "Lehrer"],
        "m": ["Mutter", "Mond", "Mauer", "Musik"], "n": ["Nacht", "Name", "Natur", "Nase"],
        "o": ["Osten", "Ofen", "Obst", "Ort"], "p": ["Papier", "Pferd", "Politik", "Pflanze"],
        "q": ["Qualität", "Quelle", "Quittung", "Quark"], "r": ["Regen", "Radio", "Rose", "Recht"],
        "s": ["Sonne", "Stadt", "Sand", "Stuhl"], "t": ["Tisch", "Tag", "Topf", "Traum"],
        "u": ["Uhr", "Ufer", "Uniform", "Urlaub"], "v": ["Vater", "Volk", "Vulkan", "Vogel"],
        "w": ["Wasser", "Wald", "Woche", "Wind"], "x": ["Xylophon", "Xenon"],
        "y": ["Yacht", "Yoga", "Yucca"], "z": ["Zeit", "Zelt", "Zucker", "Zwiebel"],
    },
    "en": {
        "a": ["apple", "arm", "autumn", "anchor"], "b": ["book", "bread", "bridge", "beach"],
        "c": ["chair", "cloud", "castle", "candle"], "d": ["door", "dream", "dinner", "desert"],
        "e": ["earth", "eagle", "engine", "evening"], "f": ["fire", "forest", "friend", "flower"],
        "g": ["garden", "gold", "grape", "guitar"], "h": ["house", "horse", "harbor", "hammer"],
        "i": ["island", "idea", "iron", "insect"], "j": ["jungle", "jacket", "jewel", "journal"],
        "k": ["kitchen", "kite", "kettle", "kingdom"], "l": ["lamp", "ladder", "lemon", "library"],
        "m": ["moon", "mountain", "market", "mirror"], "n": ["night", "needle", "noodle", "nation"],
        "o": ["ocean", "olive", "orchard", "orange"], "p": ["paper", "palace", "pillow", "pepper"],
        "q": ["queen", "quiet", "quarrel", "quill"], "r": ["river", "rainbow", "rocket", "ribbon"],
        "s": ["stone", "shadow", "storm", "silver"], "t": ["table", "thunder", "tunnel", "timber"],
        "u": ["umbrella", "uncle", "uniform", "union"], "v": ["valley", "velvet", "village", "violin"],
        "w": ["window", "winter", "wheat", "wagon"], "x": ["xenon", "xylophone"],
        "y": ["yellow", "yarn", "yard", "yogurt"], "z": ["zebra", "zipper", "zenith", "zephyr"],
    },
    "ar": {
        "ا": ["أسد", "أمل", "أخذ", "أرض"], "ب": ["بيت", "باب", "بحر", "بطيخ"],
        "ت": ["تمر", "تفاح", "تلميذ", "تموز"], "ث": ["ثلج", "ثعلب", "ثورة"],
        "ج": ["جبل", "جميل", "جمل", "جدار"], "ح": ["حبل", "حديقة", "حليب", "حجر"],
        "خ": ["خبز", "خيمة", "خاتم", "خروف"], "د": ["دار", "دفتر", "دواء", "دجاجة"],
        "ذ": ["ذهب", "ذرة", "ذيل"], "ر": ["رجل", "رز", "رسم", "رعد"],
        "ز": ["زهرة", "زيتون", "زرافة", "زجاج"], "س": ["سماء", "سمك", "سكر", "ساعة"],
        "ش": ["شمس", "شجرة", "شاي", "شهر"], "ص": ["صحراء", "صاروخ", "صباح", "صندوق"],
        "ض": ["ضوء", "ضفدع", "ضيف"], "ط": ["طائر", "طبيب", "طريق", "طاولة"],
        "ظ": ["ظهر", "ظرف", "ظل"], "ع": ["عين", "علم", "عسل", "عمل"],
        "غ": ["غيمة", "غابة", "غزال", "غداء"], "ف": ["فم", "فانوس", "فراشة", "فنجان"],
        "ق": ["قلم", "قمر", "قهوة", "قلعة"], "ك": ["كتاب", "كلب", "كرسي", "كعكة"],
        "ل": ["ليل", "لعبة", "ليمون", "لحم"], "م": ["ماء", "مدرسة", "مفتاح", "مزهرية"],
        "ن": ["نجم", "نافذة", "نهر", "نحلة"], "ه": ["هدية", "هرم", "هواء", "هدهد"],
        "و": ["وردة", "وقت", "ولد", "وزن"], "ي": ["يد", "يمامة", "يد"],
    },
    "tn": {
        "ا": ["أكل", "أم", "أب"], "ب": ["بابور", "برشة", "باهي"],
        "ج": ["جربة", "جامع"], "ش": ["شنوة", "شاي"], "ف": ["فورمة", "فطور"],
        "ق": ["قهوة عربي", "قصر"], "ص": ["صاحبي", "صغيرة"], "ي": ["يسر", "يوم"],
    },
    "ru": {
        "а": ["апельсин", "арбуз", "автобус"], "б": ["банан", "брат", "берёза"],
        "в": ["вода", "врач", "весна"], "г": ["город", "гриб", "гора"],
        "д": ["дом", "дерево", "друг"], "е": ["ель", "еда"],
        "ж": ["журнал", "жизнь", "железо"], "з": ["зима", "зонт", "звезда"],
        "и": ["игра", "история", "имя"], "й": ["йогурт", "йод"],
        "к": ["книга", "кошка", "картофель"], "л": ["лимон", "лес", "лампа"],
        "м": ["молоко", "мама", "море"], "н": ["ночь", "нога", "небо"],
        "о": ["окно", "огонь", "осень"], "п": ["папа", "песок", "письмо"],
        "р": ["река", "рука", "работа"], "с": ["солнце", "стол", "собака"],
        "т": ["трава", "телефон", "тигр"], "у": ["утро", "улица", "ухо"],
        "ф": ["фрукт", "фотография"], "х": ["хлеб", "холод", "художник"],
        "ц": ["цветок", "цирк"], "ч": ["часы", "чай", "человек"],
        "ш": ["школа", "шапка", "шум"], "щ": ["щётка", "щука"],
        "э": ["этаж", "электричество"], "ю": ["юг", "юбка"],
        "я": ["яблоко", "язык", "январь"],
    },
    "ja": {
        "あ": ["あさ", "あめ", "あか"], "い": ["いぬ", "いえ", "いす"],
        "う": ["うみ", "うた", "うし"], "え": ["えき", "えんぴつ"],
        "お": ["おかね", "おちゃ", "おとこ"],
        "か": ["かさ", "かわ", "かぎ"], "き": ["きつね", "きのう", "きって"],
        "く": ["くも", "くつ", "くすり"], "け": ["けしき", "けさ"],
        "こ": ["こども", "こころ", "ごはん"],
        "さ": ["さかな", "さくら", "さとう"], "し": ["しろ", "しま", "しごと"],
        "す": ["すし", "すみれ"], "せ": ["せんせい", "せかい"],
        "そ": ["そら", "そば", "そつぎょう"],
        "た": ["たべもの", "たまご", "たなばた"], "ち": ["ちず", "ちから"],
        "つ": ["つき", "つめ"], "て": ["てがみ", "てんき"],
        "と": ["とり", "とけい", "ともだち"],
        "な": ["なつ", "なみ", "なか"], "に": ["にわ", "にく"],
        "ぬ": ["ぬの"], "ね": ["ねこ", "ねむり"], "の": ["のり", "のはら"],
        "は": ["はな", "はし", "はる"], "ひ": ["ひと", "ひみつ"],
        "ふ": ["ふね", "ふゆ"], "へ": ["へや"], "ほ": ["ほし", "ほん"],
        "ま": ["まど", "まち", "みち"], "み": ["みず", "みかん", "みみ"],
        "む": ["むし"], "め": ["めがね"], "も": ["もり", "もち"],
        "や": ["やま", "やすみ"], "ゆ": ["ゆき", "ゆび"],
        "よ": ["よる", "よこ"], "ら": ["らくだ", "らっぱ"],
        "り": ["りんご", "りゅう"], "る": ["るすばん"],
        "れ": ["れきし"], "ろ": ["ろうそく"], "わ": ["わたし", "わに"],
        "を": [], "ん": [],
    },
    "hi": {
        "अ": ["अमर", "अनार"], "इ": ["इमारत", "इच्छा"], "उ": ["उपहार", "उन्नति"],
        "ए": ["एकता"], "ओ": ["ओस"],
        "क": ["किताब", "कमरा", "कलम"], "ख": ["खिड़की", "खाना"],
        "ग": ["घर", "गमला", "गाड़ी"], "घ": ["घड़ी", "घोड़ा"], "ङ": [],
        "च": ["चाय", "चित्र"], "छ": ["छाता", "छत"],
        "ज": ["जग", "जंगल"], "झ": ["झरना", "झूला"], "ञ": [],
        "ट": ["टमाटर", "ट्रेन"], "ठ": ["ठंडा"], "ड": ["डिब्बा"],
        "ढ": ["ढक्कन"], "ण": [],
        "त": ["तारा", "तालाब"], "थ": ["थैला", "थाली"],
        "द": ["दरवाज़ा", "दूध", "दोस्त"], "ध": ["धूप", "धन्यवाद"],
        "न": ["नदी", "नाम", "नाश्ता"],
        "प": ["पानी", "पेड़", "पक्षी"], "फ": ["फूल", "फल"],
        "ब": ["बच्चा", "बादल", "बिल्ली"], "भ": ["भालू", "भोजन"],
        "म": ["मकान", "मछली", "माँ"], "य": ["यमुना"],
        "र": ["रास्ता", "रात", "रंग"], "ल": ["लड़का", "लकड़ी", "लाल"],
        "व": ["वक्त", "विद्यालय"], "श": ["शहर", "शेर", "शुक्रिया"],
        "ष": [], "स": ["सूरज", "सड़क", "सेब"], "ह": ["हाथी", "हवा", "हरा"],
    },
    "zh": {
        "a": ["爱", "安", "按"], "b": ["不", "把", "白", "百"],
        "c": ["茶", "吃", "车", "从"], "d": ["大", "的", "得", "对"],
        "e": ["二", "饿", "鹅"], "f": ["飞", "分", "风", "饭"],
        "g": ["国", "工", "高", "个"], "h": ["好", "和", "花", "回"],
        "j": ["家", "见", "叫", "九"], "k": ["看", "口", "开", "可以"],
        "l": ["了", "来", "老", "六"], "m": ["马", "猫", "美", "没有"],
        "n": ["你", "年", "能", "女"], "o": ["偶"], "p": ["朋友", "票", "跑"],
        "q": ["七", "去", "前", "请"], "r": ["人", "日", "热", "肉"],
        "s": ["三", "是", "说", "水"], "t": ["他", "天", "听", "同"],
        "w": ["我", "五", "问", "为"], "x": ["下", "小", "学", "喜欢"],
        "y": ["一", "有", "月", "也"], "z": ["在", "走", "子", "字"],
    },
}


# ---------- API-Hilfen ----------

def _strip_harmony(content: str) -> str:
    """llm-jp (Harmony/gpt-oss-Format) liefert die Analysis IM content;
    die eigentliche Antwort steht im final-Channel."""
    if content and "<|channel|>" in content:
        marker = "<|channel|> final<|message|>"
        idx = content.rfind(marker)
        if idx < 0:
            return ""   # analysis-only, kein final-Channel
        return content[idx + len(marker):].split("<|")[0]
    return content


def chat(prompt: str, temperature: float) -> str:
    """Ein Chat-Request gegen einen OpenAI-kompatiblen Endpunkt (nur stdlib).

    enable_thinking:false — Qwen3 würde sonst per Default erst Reasoning
    produzieren und den eigentlichen Inhalt erst nach tausenden Tokens
    liefern. llama-server übergibt das Kwarg nur an Templates, die es
    kennen; Kolibri/Mistral/Jais ignorieren es schadlos."""
    global MODEL_NAME

    # Lane-Modell-Namen tragen einen Host-Suffix — fuer sie gelten
    # Lane-Regeln (max_tokens runter: ohne Grenze generiert jede Lane
    # bis zur Cap; kein enable_thinking-Kwarg: leert Lane-Templates).
    # Lane-Modell-Namen tragen einen Host-Suffix — fuer sie gelten
    # Lane-Regeln (max_tokens runter: ohne Grenze generiert jede Lane
    # bis zur Cap; kein enable_thinking-Kwarg: leert Lane-Templates).
    _lane = any(t in MODEL_NAME
                for t in (":laptop", ":workstation", ":schleppi"))
    body = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        # 4096 statt 1600: denkende Lanes (t-pro-it!) verbrauchen
        # 2-5k Tokens Reasoning BEVOR die Antwort beginnt — mit 1600
        # bleibt content leer und die Nachlade-Schleife frisst ~1-2 h
        # CPU (beobachtet: ru-define hing 104 min im Leerlauf-Retry).
        # Nicht-denkende Lanes stoppen am EOS, Mehrkosten ~0.
        "max_tokens": 4096 if _lane else 8000,
    }
    if not _lane:
        body["chat_template_kwargs"] = {"enable_thinking": False}

    last_err: Exception | None = None
    for versuch in range(4):
        try:
            # post_chat_completions wechselt bei 429 selbst aufs Fallback
            data = post_chat_completions(body, MODEL_TIMEOUT)
            break
        except urllib.error.HTTPError as e:
            # 502/503 (ModelHub-Upstream flackert gelegentlich) — warten
            # und erneut versuchen; 4xx sofort weiterwerfen.
            last_err = e
            if e.code < 500 or versuch == 3:
                raise
            time.sleep(5 * (versuch + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if versuch == 3:
                raise
            time.sleep(5 * (versuch + 1))
    else:
        raise last_err or RuntimeError("chat unbekannter Fehler")
    content = _strip_harmony(data["choices"][0]["message"]["content"])
    # Jais & Co. denken trotz enable_thinking und können bei kniffligen
    # Prompts das komplette Budget verbrauchen — dann ist content leer.
    # Strategisch nachladen: zweiter Versuch mit doppeltem Budget.
    # NICHT 4x bei Cloud-Modellen: ein Fehlversuch frisst dort bis zu
    # 32k Tokens des bezahlten Kontingents (s. en-gap 26.11.).
    base_max = body["max_tokens"]
    faktoren = (2, 4) if _lane else (2,)
    for faktor in faktoren:
        if content and content.strip():
            break
        body2 = dict(body)
        body2["max_tokens"] = base_max * faktor
        try:
            data = post_chat_completions(body2, MODEL_TIMEOUT)
            content = _strip_harmony(
                data["choices"][0]["message"]["content"])
        except Exception:
            break
    if not content or not content.strip():
        raise RuntimeError("Leere Antwort (Reasoning-Budget aufgebraucht?) — Retry")
    return content


def parse_words(raw: str) -> list[str]:
    """JSON-Array aus der Modellantwort kratzen (tolerant gegen Markdown-Fences).

    Fallback: kleine Lanes (z. B. Fantastica) halten das JSON-Format nicht
    immer ein — dann komma-/zeilengetrennte Plain-Liste nehmen. Der
    Letter-Filter und die DB-Dedupe fangen den Rest."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    items = None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(0))
            except json.JSONDecodeError:
                items = None
    if isinstance(items, list):
        return [str(w).strip() for w in items if isinstance(w, (str, int))]
    parts = re.split(r"[,;\n•]| - ", raw)
    out = []
    for p in parts:
        w = p.strip().strip("\"'-*•· ")
        w = re.sub(r"^\d+[.)]\s*", "", w)
        if w and len(w) <= 40 and not any(t in w for t in (" ", "{", "}")):
            out.append(w)
    return out[:60]


def parse_definitions(raw: str) -> dict[str, str]:
    """JSON-Objekt {Wort: Definition} aus der Modellantwort kratzen."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k).strip(): str(v).strip()
            for k, v in obj.items()
            if str(k).strip() and str(v).strip()}


DICT_API_KEY = os.environ.get("DICT_API_KEY")  # für POST/DELETE an die API


def api_get(path: str):
    with urllib.request.urlopen(f"{DICT_API_URL}{path}", timeout=30) as resp:
        return json.loads(resp.read())


def api_post(path: str, payload: dict):
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def api_put(path: str, payload: dict):
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def api_patch(path: str, payload: dict):
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="PATCH",
    )
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last = e
            time.sleep(2 * attempt)
    raise last


def letter_words(language: str, letter: str) -> list[str]:
    """Alle bekannten Wortformen eines Buchstabens, analysiert oder offen."""
    out: list[str] = []
    offset = 0
    while True:
        page = api_get(
            "/word-forms?" + urllib.parse.urlencode({
                "language": language, "prefix": letter,
                "limit": 1000, "offset": offset,
            })
        )
        if not page:
            break
        out += [e["word"] for e in page]
        offset += len(page)
    return sorted(set(out), key=str.lower)


def insert(language: str, words: list[str]) -> int:
    if not words:
        return 0
    if language in ("ar", "tn"):
        words = [normalize_ar(w) for w in words]
    # API-Payload-Grenzen beachten (Wort max. 100 Zeichen)
    words = [w for w in words if w and len(w) <= 100]
    if not words:
        return 0
    try:
        result = api_post("/word-forms/batch", {
            "forms": [
                {"word": w, "language": language, "source": f"model:{MODEL_NAME}"}
                for w in words
            ]
        })
        return result["inserted"]
    except urllib.error.HTTPError as e:
        # Einzelne ungültige Wörter nicht den ganzen Batch kosten lassen
        print(f"Batch-Fehler {e.code} — versuche einzeln", file=sys.stderr)
        added = 0
        for w in words:
            try:
                api_post("/word-forms/batch", {"forms": [{
                    "word": w, "language": language,
                    "source": f"model:{MODEL_NAME}",
                }]})
                added += 1
            except urllib.error.HTTPError as single:
                if single.code == 409:
                    continue  # Duplikat
                print(f"  übersprungen ({single.code}): {w!r}", file=sys.stderr)
        return added


# ---------- Strategien ----------

def _with_script(base: str, language: str) -> str:
    """Für Nicht-Lateinschriften die Zielschrift verlangen — sonst
    transkribiert das Modell und der Buchstaben-Filter verwirft alles."""
    if language == "ar":
        return base + ("\nWichtig: Alle Wörter ausschließlich in arabischer "
                       "Schrift (keine lateinische Umschrift).")
    if language == "tn":
        return base + ("\nWichtig: Alle Wörter ausschließlich in arabischer "
                       "Schrift, tunesisch (keine lateinische Umschrift).")
    if language == "ru":
        return base + ("\nWichtig: Alle Wörter ausschließlich in kyrillischer "
                       "Schrift (keine lateinische Umschrift).")
    if language == "ja":
        return base + ("\nWichtig: Alle Wörter ausschließlich in japanischer "
                       "Schrift (Kanji/Kana, keine Rōmaji-Umschrift).")
    if language == "hi":
        return base + ("\nWichtig: Alle Wörter ausschließlich in Devanagari "
                       "(keine lateinische Umschrift).")
    if language == "zh":
        return base + ("\nWichtig: Alle Wörter ausschließlich als "
                       "vereinfachte chinesische Schriftzeichen (keine "
                       "Pinyin-Angabe, keine traditionellen Zeichen).")
    return base


# Chinesisch hat kein Alphabet: free/suggest fragen per Pinyin-Anfangslaut.
PROMPT_FREE_ZH = (
    "Liste genau {n} häufige chinesische Wörter (Grundformen, "
    "Alltagssprache, vereinfachte Schriftzeichen, keine Eigennamen, keine "
    "Abkürzungen), deren Pinyin mit dem Buchstaben \"{letter}\" beginnt. "
    "NUR Wörter, die im modernen Mandarin tatsächlich gebräuchlich sind. "
    "Antworte NUR mit einem JSON-Array von Strings (nur die Schriftzeichen, "
    "kein Pinyin, kein Markdown)."
)

PROMPT_SUGGEST_ZH = (
    "Bekannte chinesische Wörter (Schriftzeichen):\n{known}\n"
    "Nenne genau {n} weitere häufige chinesische Wörter (vereinfachte "
    "Schriftzeichen, Grundformen, Alltagssprache, keine Eigennamen), deren "
    "Pinyin mit dem Buchstaben \"{letter}\" beginnt und die NICHT in der "
    "Liste stehen. Antworte NUR mit einem JSON-Array von Strings (nur die "
    "Schriftzeichen, kein Pinyin, kein Markdown)."
)


def _free_prompt(lang: str, language: str, letter: str, per_round: int) -> str:
    if language == "zh":
        return _with_script(PROMPT_FREE_ZH.format(
            n=per_round, letter=letter), language)
    return _with_script(PROMPT_FREE.format(
        n=per_round, lang=lang, letter=letter), language)


def _suggest_prompt(lang: str, language: str, letter: str,
                    known: list[str], per_round: int) -> str:
    if language == "zh":
        return _with_script(PROMPT_SUGGEST_ZH.format(
            letter=letter, known=", ".join(known) if known
            else "(noch keine bekannt)", n=per_round), language)
    return _with_script(PROMPT_SUGGEST.format(
        lang=lang, letter=letter,
        known=", ".join(known) if known else "(noch keine bekannt)",
        n=per_round), language)


def harvest_free(language, letters, rounds, per_round, temperature, delay):
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    for letter in letters:
        known = set(letter_words(language, letter))
        before = len(known)
        for rnd in range(1, rounds + 1):
            try:
                cand = parse_words(chat(
                    _free_prompt(lang, language, letter, per_round),
                    temperature))
            except Exception as e:
                print(f"[{letter}] Runde {rnd}: Modell-Fehler: {e}", file=sys.stderr)
                time.sleep(delay * 3)
                continue
            new = [w for w in cand
                   if _matches_letter(w, letter, language)
                   and w not in known]
            added = insert(language, new)
            known.update(new)
            print(f"[{letter}] Runde {rnd}: +{added} neu")
            time.sleep(delay)
        print(f"=> {letter}: {len(known) - before} neue Wörter ({len(known)} gesamt)")


def harvest_suggest(language, letters, max_rounds, per_round, temperature, delay):
    """Bekannte Wörter zeigen und erweitern lassen — bis nichts mehr kommt."""
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    for letter in letters:
        rnd = 0
        while rnd < max_rounds:
            rnd += 1
            known = letter_words(language, letter)
            sample = random.sample(known, min(10, len(known))) if known else []
            prompt = _suggest_prompt(lang, language, letter, sample, per_round)
            try:
                cand = parse_words(chat(prompt, temperature))
            except Exception as e:
                print(f"[{letter}] Runde {rnd}: Modell-Fehler: {e}", file=sys.stderr)
                time.sleep(delay * 3)
                break
            known_set = set(known)
            new = [w for w in cand
                   if _matches_letter(w, letter, language)
                   and w not in known_set]
            added = insert(language, new)
            print(f"[{letter}] Runde {rnd}: +{added} neu ({len(known)} vorher bekannt)")
            if added == 0:
                break
            time.sleep(delay)


def harvest_gap(language, letters, max_rounds, per_round, temperature, delay, pairs_per_round):
    """Gezielt Lücken schließen: Wörter zwischen zwei Nachbarn erfragen."""
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    if language in ("zh", "ko", "th"):
        # Kein zeichenbasierter Index (Pinyin-/Anlaut-Steuerung statt
        # Präfix): ein Durchlauf über den ganzen Bestand, Kandidaten
        # werden nicht auf Unicode-Position geprüft.
        letters = [""]
    for letter in letters:
        rnd = 0
        while rnd < max_rounds:
            rnd += 1
            words = letter_words(language, letter)
            if len(words) < 2:
                print(f"[{letter}] weniger als 2 bekannte Wörter — "
                      f"erst Seeds oder suggest nutzen")
                break
            idx = random.sample(range(len(words) - 1),
                                k=min(pairs_per_round, len(words) - 1))
            new_total = 0
            for i in sorted(idx):
                a, b = words[i], words[i + 1]
                prompt = _gap_prompt(lang, language, a, b, per_round)
                try:
                    cand = parse_words(chat(prompt, temperature))
                except Exception as e:
                    print(f"[{letter}] Paar {a}|{b}: Modell-Fehler: {e}",
                          file=sys.stderr)
                    time.sleep(delay * 3)
                    continue
                if language == "zh":
                    # Unicode-Order ≠ Pinyin-Order: Between-Check entfällt,
                    # das Modell liefert nur passende Zwischenwörter.
                    gap = [w for w in cand if _matches_letter(w, letter, language)]
                else:
                    gap = [w for w in cand
                           if w and a.lower() < w.lower() < b.lower()]
                new_total += insert(language, gap)
                time.sleep(delay)
            print(f"[{letter}] Runde {rnd}: +{new_total} neu "
                  f"({len(words)} vorher bekannt)")
            if new_total == 0:
                break


def _define_batch(batch: list[dict], lang: str, temperature: float,
                  depth: int = 0) -> int:
    """Definiert einen Batch in einem Modellaufruf.

    Schlägt der Aufruf oder das JSON-Parsen fehl, wird der Batch halbiert
    und erneut versucht (bis Tiefe 3) — große Blöcke verlieren so bei
    einem einzelnen Fehler nicht gleich alle Wörter.
    """
    words = [e["word"] for e in batch]
    try:
        defs = parse_definitions(chat(
            _with_script(
                PROMPT_DEFINE.format(lang=lang, words=", ".join(words)),
                batch[0].get("language", "") if batch else ""),
            temperature))
    except Exception as e:
        if len(batch) > 5 and depth < 3:
            half = len(batch) // 2
            print(f"  Batch mit {len(batch)} Wörtern fehlgeschlagen ({e}) — "
                  f"teile in {half}+{len(batch) - half}", file=sys.stderr)
            return (_define_batch(batch[:half], lang, temperature, depth + 1)
                    + _define_batch(batch[half:], lang, temperature, depth + 1))
        print(f"  Batch-Fehler ({len(batch)} Wörter): {e}", file=sys.stderr)
        return 0
    added = 0
    for e in batch:
        d = defs.get(e["word"])
        if not d or len(d) > 1000:
            continue
        try:
            if e.get("analyzed", 1):
                api_patch(f"/entries/{e['id']}", {"definition": d})
            else:
                api_post("/lexemes/analyze", {
                    "form_id": e["form_id"],
                    "lemma": e["word"],
                    "definition": d,
                    "source": f"model-analysis:{MODEL_NAME}",
                })
            added += 1
        except Exception as e2:
            print(f"  {e['word']!r}: PATCH-Fehler: {e2}", file=sys.stderr)
    return added


def harvest_define(language: str, batch_size: int, delay: float,
                   temperature: float = 0.3, limit: int = 0) -> None:
    """Definitionen für alle Einträge ohne definition nachrüsten.

    Holt die undefinierten Einträge einmalig als Snapshot (seitenweise) und
    definiert sie in Batches — ein Modellaufruf pro batch_size Wörter,
    ein PATCH pro Wort. Bereits definierte Einträge werden nie angefasst.
    """
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    todo: list[dict] = []
    offset = 0
    while True:
        page = api_get(
            "/entries?" + urllib.parse.urlencode({
                "language": language, "undefined": "true",
                "limit": 1000, "offset": offset,
            })
        )
        if not page:
            break
        todo += page
        offset += len(page)
    # Reine Schreibweisen werden erst durch diesen regulären
    # Definitions-/Analyse-Schritt zu Lexemen und haben Vorrang vor
    # bereits analysierten, aber noch definitionslosen Resten.
    existing_without_definition = todo
    todo = []
    offset = 0
    while True:
        page = api_get(
            "/word-forms?" + urllib.parse.urlencode({
                "language": language, "unanalyzed": "true",
                "limit": 1000, "offset": offset,
            })
        )
        if not page:
            break
        todo += page
        offset += len(page)
    todo += existing_without_definition
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} Einträge ohne Definition")
    total = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        added = _define_batch(batch, lang, temperature)
        total += added
        print(f"Batch {i // batch_size + 1}: +{added} Definitionen "
              f"({total}/{len(todo)})")
        time.sleep(delay)
    print(f"define fertig: {total} von {len(todo)} definiert")


def seed(language: str) -> None:
    seeds = SEEDS.get(language)
    if not seeds:
        print(f"Keine Seeds für '{language}' eingebaut — bitte selbst per API eintragen.")
        return
    words = [w for letter_words_list in seeds.values() for w in letter_words_list]
    result = api_post("/word-forms/batch", {
        "forms": [
            {"word": w, "language": language, "source": "seed"} for w in words
        ]
    })
    print(f"Seeds {language}: {result}")


# ---------- CLI ----------

def parse_letters(spec: str) -> list[str]:
    if spec == "all":
        return list(string.ascii_lowercase) + ["ä", "ö", "ü", "ß"]
    if spec == "ar":
        return list(AR_LETTERS)
    if spec == "ru":
        return list(RU_LETTERS)
    if spec == "ja":
        return list(JA_LETTERS)
    if spec == "hi":
        return list(HI_LETTERS)
    if spec == "zh":
        # Pinyin-Anfangslaute statt Zeichen-Alphabet
        return list(ZH_PINYIN_LETTERS)
    if spec == "bg":
        return list(BG_LETTERS)
    if spec == "uk":
        return list(UK_LETTERS)
    if spec == "tr":
        return list(TR_LETTERS)
    if spec == "ko":
        return list(KO_INITIALS)
    if spec == "th":
        return list(TH_CONSONANTS)
    if re.fullmatch(r"[a-z]-[a-z]", spec):
        return list(string.ascii_lowercase[
            string.ascii_lowercase.index(spec[0]):
            string.ascii_lowercase.index(spec[2]) + 1
        ])
    return [l.strip() for l in spec.split(",") if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Wörter aus einem LLM in die Wörterbuch-API harvesten")
    ap.add_argument("--mode", choices=["free", "suggest", "gap", "define"],
                    default="suggest")
    ap.add_argument("--language", default="de")
    ap.add_argument("--letters", default="a-z", help="'a-z', 'a-f', 'all', 'ä,ö,ü,ß'")
    ap.add_argument("--rounds", type=int, default=3, help="Runden pro Buchstabe (gap/suggest: Abbruch bei 0 neu)")
    ap.add_argument("--per-round", type=int, default=80,
                    help="Wörter pro Modellaufruf (define: Batchgröße)")
    ap.add_argument("--pairs", type=int, default=4, help="Paare pro gap-Runde")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0,
                    help="maximale Arbeitseinheiten (0 = unbegrenzt)")
    ap.add_argument("--seed", action="store_true", help="Startbestand einspielen und beenden")
    args = ap.parse_args()

    if args.seed:
        seed(args.language)
        return

    if args.mode == "define":
        print(f"Modus: define | Modell: {MODEL_NAME} @ {MODEL_BASE_URL}")
        harvest_define(args.language, args.per_round, args.delay,
                       limit=args.limit)
        print(f"\nFertig. DB-Stand: {api_get('/stats')}")
        return

    letters = parse_letters(args.letters)
    print(f"Modus: {args.mode} | Modell: {MODEL_NAME} @ {MODEL_BASE_URL}\n"
          f"Buchstaben: {''.join(letters)}")

    if args.mode == "free":
        harvest_free(args.language, letters, args.rounds, args.per_round,
                     args.temperature, args.delay)
    elif args.mode == "suggest":
        harvest_suggest(args.language, letters, args.rounds, args.per_round,
                        args.temperature, args.delay)
    else:
        harvest_gap(args.language, letters, args.rounds, args.per_round,
                    args.temperature, args.delay, args.pairs)

    print(f"\nFertig. DB-Stand: {api_get('/stats')}")


if __name__ == "__main__":
    main()
