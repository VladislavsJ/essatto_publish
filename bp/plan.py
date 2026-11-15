"""Question -> query plan (JSON). Rule-based first (deterministic); returns None + reasons when unsure.
Plan schema:
 {"op": "count|sum|value|max|min|avg|list|group_count|group_sum",
  "kind": "<entity kind>", "attr": "area|length|volume|side_area|perimeter|null",
  "filters": [{"field": "...", "op": "eq|in|nin|match|true|gt|lt|gte|lte", "value": ...}],
  "group_by": null | "<attr>", "notes": ["interpretation decisions"]}"""
import json as _json, os as _os, re
from . import vocab as V

QUOTE = re.compile(r"""(?<!\w)['‘’„“«"]\s*(.+?)\s*['’“”»"](?=[\s,.?:;)!/-]|$)""")
# Case-sensitive on the apartment ID so prepositions ('apartment in', 'mieszkania w') never match as apartment IDs,
# plus explicit lowercase single-letter support after 'apt' / 'whg' ('how big is apt b?').
APT_RX = re.compile(
    r"(?i:\b(?:apartment|apts?\.?|appt?\.?|wohnung|whg\.?|wohneinheit|appartement|logement|appartamento|unit|asunto|asunnon"
    r"|huoneisto|huoneiston|lägenhet|lägenheten|korteri?|woning|wooneenheid|apartamento|vivienda|mieszkani[aeu]|lejlighed))\s+([A-Z0-9]{1,2})\b"
    r"|(?i:\b(?:apt|whg)\.?\s+)([a-zA-Z0-9])\b"
)
NUM_RX = re.compile(r"\b([A-Z]{0,2}\d{0,2}\.\d{2})\b")

# Unsupported properties, non-existent entity types, or restrictive architectural subtypes -> defer to planner
HAZARD_RX = re.compile(
    r"\b(?:height|höhe|hauteur|altezza|korkeus|höjd|kõrgus|hoogte|altura|wysokość"
    r"|ceiling|plafond|deckenhöhe|soffitto|sisäkatto|innertak|techo|sufit"
    r"|thickness|dicke|épaisseur|spessore|paksuus|tjocklek|paksus|dikte|espesor|grubość"
    r"|cost|kosten|coût|costo|kustannu\w*|kostnad\w*|price|preis|prix|prezzo|hinta|pris|prijs|precio|cena"
    r"|rent|miete|loyer|affitto|vuokra|hyra|huur|alquiler|czynsz|euro|euros|chf|usd|dollars?"
    r"|u-?value|u-?wert|transmittance|thermal|wärmedämm\w*|thermique"
    r"|garage\w*|garaje\w*|garaż\w*|autotalli\w*|parkering\w*|parking|aparcamiento\w*|parkeer\w*"
    r"|escalators?|rolltrepp\w*|liukuport\w*|rulltrapp\w*|eskalaator\w*|roltrap\w*"
    r"|swimming|pool|piscin\w*|schwimmbad\w*|uima-allas\w*|basen\w*|heliport\w*|helipad\w*|helicopter\w*"
    r"|load[- ]bearing|fire[- ]rat\w*|brandschutz\w*|coupe-feu|sliding|schiebetür\w*|coulissant\w*|scorrevol\w*|liukuov\w*|skjutdörr\w*|schuifdeur\w*|correder\w*|przesuwn\w*)\b",
    re.I,
)

UNSUPPORTED_UNIT_RX = re.compile(
    r"\b(?:inch(?:es)?|yards?|miles?|mm²|mm2|ft³|ft3|cubic\s+f(?:ee|oo)t|lit(?:er|re)s?|gallons?|tonnes?|tons?|kg|kilograms?)\b",
    re.I,
)

INCL_RX = re.compile(
    r"\b(?:including|incl\.?|inclusive\s+of|together\s+with|inklusive|einschliesslich|einschließlich|mit\s+balkon\w*"
    r"|y\s+compris|compris|compres[eioa]|inclus[eioa]|mukaan\s+lukien|sealhulgas|inklusiv\w*|inkluderande|inclusief"
    r"|incluyendo|incluid[oa]s?|włącznie\s+z|wliczając)\b",
    re.I,
)

LEADING_EXCL_RX = re.compile(
    r"^\s*(?:excluding|without|except(?:\s+for)?|not\s+counting|not\s+including|ohne|exklusive|sans(?:\s+compter)?|hors"
    r"|escludendo|senza(?:\s+contare)?|ilman|utan|exklusive|ilma|zonder|sin(?:\s+contar)?|excluyendo|bez|nie\s+licząc)\b([^,?]+),\s*",
    re.I,
)

EXCL_CLAUSE_RX = re.compile(
    r"(?:[,;—–-]\s*|\s+)(?:"
    r"(?:excl\.|exkl\.|v\.a\.)(?=\s|$)"
    r"|(?:except(?:\s+for)?|excluding|excl|without|apart\s+from|other\s+than|not\s+counting|not\s+including|leaving\s+out|ignoring|omitting)\b"
    r"|(?:hors|sans(?:\s+compter)?|hormis|sauf|à\s+l'exception|à\s+l'exclusion|excluant)\b"
    r"|(?:ohne|ausser|außer|ausgenommen|exklusive|abzüglich)\b"
    r"|(?:eccetto|ad\s+eccezione|esclus[eioa]|escludendo|senza(?:\s+contare)?|non\s+contando)\b"
    r"|(?:lukuun\s+ottamatta|ilman|paitsi|pois\s*lukien|jättäen)\b"
    r"|(?:välja\s+arvatud|ilma|jättes|arvestamata|kuid\s+ära\s+arvesta)\b"
    r"|(?:undtagen|uden|utan|utom|bortsett\s+från|borträknat|med\s+undantag\s+av|om\s+man\s+inte\s+räknar)\b"
    r"|(?:zonder|uitgezonderd|exclusief|met\s+uitzondering\s+van)\b"
    r"|(?:sin(?:\s+contar|\s+incluir)?|excepto|excluyendo|excluid[oa]s?|a\s+excepción\s+de)\b"
    r"|(?:bez|z\s+wyłączeniem|nie\s+licząc|z\s+pominięciem|pomijając|poza)\b"
    r").*"
    r"|[,;.]\s*(?:kun\b[^?]*\bei\s+lasketa|[^,;?]*\bniet\s+meegerekend|[^;?]*\bauszuschlie(?:ß|ss)en|[^;?]*\bdo\s+not\s+count|[^;?]*\bei\s+lasketa).*",
    re.I,
)

DOOR_LEAF_CLARIF_RX = re.compile(
    r"door\s*leaf|door\s*leaves|open\s+doorway|open\s+passage|cased\s+opening|pass[- ]through|ovileht|oviauk|ukseleht|ukseav|vantail|vantaux|baie\s+libre|ouverture"
    r"|türblatt|wandöffnung|durchg[aä]ng|\banta\b|passagg\w*\s+apert|varc\w*\s+apert|deurblad|deuropening|\bhoja\b"
    r"|hueco\w*\s+abiert|vano\w*\s+abiert|skrzydł|otw\w*\s+drzwiow|przejś\w*|dörrblad|öppning",
    re.I,
)

OUTDOOR_TARGET_RX = re.compile(
    r"balcon|balkon|loggia|loggie|terrass|terrazz|terrace|terrazas?|taras|outdoor|aussen|außen|extérieur|estern|all'aperto"
    r"|parvek|rõdu|välisruum|buiten|zewnętrzn|utomhus|ulkotil|altan",
    re.I,
)

SHAFT_TARGET_RX = re.compile(
    r"shaft|schächt|schacht|gaine|cavedi|void|vid\b|luftr|skakt|šaht|forometri|schachten|huecos|pustk|szyb|kuilu|schakt|trémie|vuot",
    re.I,
)

WC_ROOM_CUE_RX = re.compile(
    r"toilet\s+rooms?|wc[- ]rooms?|separate\s+wcs?|lavatory\s+rooms?|wc-tila|wc-huon|vessa|wc-r[aä]um|toilettenr[aä]um|gäste-wc"
    r"|locaux\s+wc|pièces?\s+(?:de\s+)?(?:wc|toilettes?)|cabinets?\s+de\s+toilettes?|local[ei]\s+(?:wc|toilette)|vani\s+wc|servizi\s+igienici"
    r"|wc-rum|toalettrum|toalettutrymme|wc-ruum|tualettruum|wc-ruimte|toiletruimte|aseos?\s+(?:independientes?|separados?)"
    r"|cuartos?\s+de\s+(?:aseo|wc)|recintos?\s+de\s+wc|locales?\s+de\s+wc|estancias?\s+de\s+inodoro"
    r"|pomieszcze\w*\s+(?:wc|ustępow)|(?:osobn\w*|wydzielon\w*|oddzieln\w*)\s+(?:wc|toalet)"
    r"|(?:separate|separat\w*|eraldi\w*|erilli\w*|apart[e]?\w*|afzonderlijk\w*|indipendent\w*|indépendant\w*|independient\w*|fristående|eigenständig\w*)\s+[a-zà-ž -]{0,20}\bwc\b",
    re.I,
)

COUNT_UNIT_FALSE_RX = re.compile(
    r"(?:how\s+many|wie\s+viele|combien(?:\s+de|\s+d')?|quant[ie]|kuinka\s+monta|montako|mitu|kui\s+palju|hur\s+många|hoeveel|cuánt[oa]s|cuant[oa]s|\bile)\s+"
    r"(?:total\s+|net\s+|linear\s+)?(?:m²|m2\b|sqm\b|sq\.?\s*m\b|square\s+met|square\s+f(?:ee|oo)t|sq\.?\s*ft|ft²|ft2|cm²|cm2|square\s+centimet"
    r"|quadratmet|quadratfuss|quadratfuß|mètres?\s+carrés?|pieds?\s+carrés?|metri\s+quadrati|neliömetr|kvadratmet|ruutmeet"
    r"|vierkante\s+meter|metros?\s+cuadrados?|pies?\s+cuadrados?|metr\w*\s+kwadratow|stop\w*\s+kwadratow"
    r"|m³|m3\b|cubic\s+met|kubikmet|mètres?\s+cubes?|metri\s+cubi|kuutiometr|kuupmeet|kubieke\s+meter|metros?\s+cúbicos?|metr\w*\s+sześcienn"
    r"|metres?\b|meters?\b|metern?\b|mètres?\b|metri\b|metriä\b|metreinä\b|metros\b|metrach\b|cm\b|mm\b|centimet|millimet|zentimet|feet\b|ft\b)",
    re.I,
)

PLURAL_ENTITY_RX = re.compile(
    r"\b(?:doors|windows|walls|columns|cols|beams|stairs|slabs|rooms|spaces|apts|apartments|toilets|wcs|bowls|sinks|washbasins"
    r"|bathtubs|tubs|showers|saunas|bedrooms|kitchens|bathrooms|balconies|storeys|stories|floors|fixtures|luminaires|lamps)\b",
    re.I,
)

_TV = set(_json.load(open(_os.path.join(_os.path.dirname(__file__), "template_vocab.json"), encoding="utf-8")))
for _f in ("vocab.py", "plan.py"):
    _TV |= set(re.findall(r"[^\W\d_]{3,}", open(_os.path.join(_os.path.dirname(__file__), _f), encoding="utf-8").read().lower()))
_TV |= {"big", "size", "square", "feet", "foot", "meters", "metres", "meter", "metre", "largest", "smallest", "average", "plan", "drawn", "shown", "floor"}

_STOP = set((
    "the a an of in on at to for and or is are was were be been being this that these those with without by from as it its "
    "their there here what which how many much any all each per does do did has have had into than then out off over under "
    "der die das den dem des ein eine einer eines einem einen und oder auf im in am an aus bei mit von zu zum zur für über "
    "wie ist sind war wird werden hat haben gibt es sich sie wir ich mir mich man alle aller allen alles dieser diese dieses "
    "diesem diesen jede jeder jedes le la les un une des du de au aux en sur dans par pour avec sans ou et est sont a ont "
    "ce cet cette ces il ils elle elles on nous vous je me qui que quoi dont où tous toute toutes tout il lo la i gli le un "
    "uno una di a da in con su per tra fra e ed o è sono ha hanno questo questa questi queste tutto tutta tutti tutte "
    "ja tai vai on ovat ei ole kuin kun jos sekä että niin tässä tähän tästä sen sitä ne he me te "
    "och eller är var har kan på i av för med till från om som den det de denna detta dessa här där alla allt "
    "ning või on ei ole kui ka see selle sellel selles need mis kes kus kõik kõigi "
    "de het een en of in op aan met voor van bij uit tot naar door over is zijn was heeft hebben er dit deze die dat "
    "el la los las un una unos unas de del al y e o u en con por para sin sobre entre es son está están hay este esta estos estas "
    "i w we na z ze do od po o u a oraz lub czy jest są ma mają ten ta to tym tej tego te tych"
).split())

_KNOWN_SPAN_RX = re.compile(
    "|".join([
        *(rx for _, rx in V.KINDS),
        *(rx for _, rx in V.RTYPE_IN_Q),
        V.EXISTS, V.COUNT, V.SIDE_AREA, V.AREA, V.LENGTH, V.VOLUME,
        V.LARGEST, V.SMALLEST, V.AVERAGE, V.TOTAL, V.INDOOR_TOTAL, V.OUTDOOR_WORDS, V.PHRASING_RX,
    ]),
    re.I,
)

_TYPO_TARGETS = (
    "windows", "window", "doors", "toilets", "toilet", "washbasins", "washbasin", "bathtubs", "bathtub",
    "showers", "shower", "apartments", "apartment", "bedrooms", "bedroom", "kitchens", "kitchen",
    "bathrooms", "bathroom", "balconies", "balcony", "columns", "column", "stairs", "saunas", "sauna",
    "corridors", "corridor", "storeys", "floors", "square", "metres", "meters", "centimetres", "millimetres",
    "excluding", "labelled",
)

def _fix_typos(s):
    """Conservative typo normalization on ASCII words not already in the rule vocabulary."""
    s = re.sub(r"\bhwo\b", "how", s, flags=re.I)
    s = re.sub(r"\bamny\b", "many", s, flags=re.I)
    s = re.sub(r"\bdorrs\b", "doors", s, flags=re.I)
    s = re.sub(r"\bflor(?=\s+(?:area|plan)\b)", "floor", s, flags=re.I)

    def _repl(m):
        w = m.group(0)
        wl = w.lower()
        if wl in _TV or wl in _STOP or wl in _TYPO_TARGETS:
            return w
        for tgt in _TYPO_TARGETS:
            # 1) Exact adjacent character transposition (length >= 5, identical character multiset)
            if len(wl) == len(tgt) and len(wl) >= 5 and sorted(wl) == sorted(tgt):
                for i in range(len(wl) - 1):
                    if wl[:i] + wl[i + 1] + wl[i] + wl[i + 2:] == tgt:
                        return tgt
            # 2) Single dropped or extra character on words >= 6 chars with matching start & end letter
            if len(tgt) >= 6 and len(wl) >= 5 and wl[0] == tgt[0] and wl[-1] == tgt[-1]:
                if len(wl) == len(tgt) - 1:
                    for i in range(1, len(tgt) - 1):
                        if tgt[:i] + tgt[i + 1:] == wl:
                            return tgt
                elif len(wl) == len(tgt) + 1:
                    for i in range(1, len(wl) - 1):
                        if wl[:i] + wl[i + 1:] == tgt:
                            return tgt
        return w

    return re.sub(r"\b[A-Za-z]{5,}\b", _repl, s)

def unknown_words(core):
    """Content words in core not recognised by _TV, _STOP, or vocab.py regexes."""
    cleaned = re.sub(r"(?:m²|m2|m³|m3)-tes\b", " ", core)
    rem = _KNOWN_SPAN_RX.sub(" ", cleaned)
    return sorted({w for w in re.findall(r"[^\W\d_]{3,}", rem) if w not in _TV and w not in _STOP})

def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def _resolve_unquoted_ifc_level(tl, levels_list):
    """Deterministically resolve unambiguous unquoted IFC level names (e.g. 'lvl 2' -> 'Level 2')."""
    if not levels_list:
        return None
    norm_map = {_norm(L): L for L in levels_list}
    m = re.search(r"\b(?:level|lvl\.?|lv\.?|storey|floor)\s*([0-9]+)\b", tl)
    if m:
        num = m.group(1)
        cands = [L for nl, L in norm_map.items() if nl in (f"level {num}", f"{num}. level", f"floor {num}", f"storey {num}")]
        if len(cands) == 1:
            return cands[0]
        return None
    # Exact whole-phrase match against multi-char level names (longest unique match)
    hits = []
    for nl, L in norm_map.items():
        if len(nl) >= 4 and re.search(rf"(?<!\w){re.escape(nl)}(?!\w)", tl):
            hits.append((len(nl), L))
    if hits:
        hits.sort(reverse=True)
        top_len = hits[0][0]
        top = [L for ln, L in hits if ln == top_len]
        if len(top) == 1:
            return top[0]
    return None

_NUM_VAL = r"([0-9]+(?:[.,][0-9]+)?)"
_UNIT_OPT = r"\s*(m²|m2|sqm|sq\.?\s*m|cm²|cm2|ft²|ft2|m³|m3|cm|mm|ft|m\b)?"

THRESHOLD_PATTERNS = [
    # >= N
    ("gte", re.compile(rf"(?:at\s+least|no\s+less\s+than|minimum\s+of|min\.?\s+of|mindestens|wenigstens|ab\s+|au\s+moins|almeno|vähintään|minst|vähemalt|minstens|ten\s+minste|al\s+menos|como\s+mínimo|co\s+najmniej|przynajmniej|>=|≥)\s*{_NUM_VAL}{_UNIT_OPT}", re.I)),
    ("gte", re.compile(rf"{_NUM_VAL}{_UNIT_OPT}\s+(?:or\s+(?:more|larger|bigger|greater|above)|and\s+(?:more|above|up)|oder\s+(?:mehr|größer|grösser)|und\s+mehr|ou\s+plus|o\s+più|tai\s+enemmän|eller\s+mer|või\s+rohkem|of\s+meer|o\s+más|lub\s+więcej)", re.I)),
    # <= N
    ("lte", re.compile(rf"(?:at\s+most|no\s+more\s+than|up\s+to|maximum\s+of|max\.?\s+of|höchstens|maximal|bis\s+zu|au\s+plus|al\s+massimo|enintään|högst|kuni|hoogstens|ten\s+hoogste|como\s+máximo|a\s+lo\s+sumo|co\s+najwyżej|maksymalnie|<=|≤)\s*{_NUM_VAL}{_UNIT_OPT}", re.I)),
    ("lte", re.compile(rf"{_NUM_VAL}{_UNIT_OPT}\s+(?:or\s+(?:less|smaller|fewer|below)|oder\s+(?:weniger|kleiner)|ou\s+moins|o\s+meno|tai\s+vähemmän|eller\s+mindre|või\s+vähem|of\s+minder|o\s+menos|lub\s+mniej)", re.I)),
    # > N
    ("gt", re.compile(rf"(?:(?:larger|bigger|greater|more|wider|longer)\s+than|\bover|\babove|mehr\s+als|grö(?:ß|ss)er\s+als|über\s+|plus\s+(?:de|grand\w*\s+que)|supérieur\w*\s+à|più\s+(?:di|grande\s+di)|superiore\s+a|\byli\b|suurempi\s+kuin|mer\s+än|större\s+än|\büle\b|suurem\s+kui|meer\s+dan|groter\s+dan|más\s+(?:de|grande\s+que)|mayor\s+(?:de|que)|ponad|większ\w*\s+niż|>)\s*{_NUM_VAL}{_UNIT_OPT}", re.I)),
    # < N
    ("lt", re.compile(rf"(?:(?:smaller|less|narrower|shorter)\s+than|\bunder|\bbelow|weniger\s+als|kleiner\s+als|unter\s+|moins\s+(?:de|grand\w*\s+que)|inférieur\w*\s+à|meno\s+di|inferiore\s+a|\balle\s+(?=\d)|pienempi\s+kuin|mindre\s+än|\balla\s+(?=\d)|väiksem\s+kui|minder\s+dan|kleiner\s+dan|menos\s+de|menor\s+(?:de|que)|poniżej|mniejsz\w*\s+niż|<)\s*{_NUM_VAL}{_UNIT_OPT}", re.I)),
]

def _extract_threshold(text, kind, attr):
    for fop, rx in THRESHOLD_PATTERNS:
        m = rx.search(text)
        if m:
            val = float(m.group(1).replace(",", "."))
            u = (m.group(2) or "").strip().lower()
            if u == "cm":
                val /= 100.0
            elif u == "mm":
                val /= 1000.0
            elif u == "ft":
                val /= 3.28084
            elif u in ("cm²", "cm2"):
                val /= 10000.0
            elif u in ("ft²", "ft2"):
                val /= 10.76391
            if re.search(r"wider|narrower", m.group(0), re.I):
                fld = "width"
            elif u in ("m³", "m3") or attr == "volume":
                fld = "volume"
            elif u in ("m²", "m2", "sqm", "sq m", "sq. m", "cm²", "cm2", "ft²", "ft2") or attr == "area" or kind == "room":
                fld = "area"
            else:
                fld = "length"
            return fld, fop, round(val, 4), m.group(0)
    return None

def parse(q, store):
    """returns (plan | None, reasons[])"""
    t_orig = q.strip()
    # Apply conservative typo fixes only outside quoted names
    q_spans = [(m.start(), m.end(), m.group(0)) for m in QUOTE.finditer(t_orig)]
    if q_spans:
        parts, pos = [], 0
        for s0, s1, qtxt in q_spans:
            parts.append(_fix_typos(t_orig[pos:s0]))
            parts.append(qtxt)
            pos = s1
        parts.append(_fix_typos(t_orig[pos:]))
        t = "".join(parts)
    else:
        t = _fix_typos(t_orig)

    tl = t.lower()
    tl_unq = QUOTE.sub(" ", tl)
    notes = []
    why_not = []

    if HAZARD_RX.search(tl_unq):
        return None, ["question asks about an unsupported property, object or subtype -> handled by the planner"]
    if UNSUPPORTED_UNIT_RX.search(tl_unq):
        return None, ["unsupported unit conversion requested -> handled by the planner"]

    # Separate parenthetical exclusion clauses from main text BEFORE quote resolution
    excl_chunks = []
    def _paren_split(m):
        p = m.group(0)
        p_unq = QUOTE.sub(" ", p.lower())
        if re.search(V.OUTDOOR_WORDS, p_unq) or re.search(r"\bno\s+(?:balcon|outdoor|loggia|terrac)", p_unq):
            excl_chunks.append(p)
            return " "
        return p

    work_t = re.sub(r"\([^)]*\)", _paren_split, t)
    m_lead = LEADING_EXCL_RX.match(work_t)
    if m_lead:
        excl_chunks.append(m_lead.group(1))
        work_t = work_t[m_lead.end():]
    m_excl = EXCL_CLAUSE_RX.search(work_t)
    if m_excl:
        excl_chunks.append(m_excl.group(0))
        main_t = work_t[:m_excl.start()]
    else:
        main_t = work_t
    excl_t = " ".join(excl_chunks)
    excl_tl = excl_t.lower()

    # Build 'core' from main_t with quotes, parentheticals, and inclusion clauses removed
    core = QUOTE.sub(" ", main_t.lower())
    core = re.sub(r"\([^)]*\)", " ", core)
    including = bool(INCL_RX.search( QUOTE.sub(" ", t.lower()) ))
    core = re.sub(rf"{INCL_RX.pattern}[^,;:?.]*", " ", core)
    core_norm = _norm(core)

    S = store.summary()

    # Questions needing grouping / listing / comparison / multi-fact reasoning go to the planner
    if re.search(
        r"\blist\b|of each|each type|per type|by type|by level|per level|breakdown|broken down|grouped|distribution"
        r"|which (?:\w+ ){0,3}types?|what (?:\w+ ){0,3}types?|types? of|compare|ratio|percentage|percent|share of"
        r"|difference between|which room|which rooms|which apartment|name the|largest .* and|\bbetween\b",
        tl_unq,
    ):
        return None, ["question needs grouping, listing or comparison -> handled by the planner"]
    # Non-numeric comparative ('more doors than windows') -> planner
    if re.search(r"\b(?:more|fewer|less)\s+[a-zäöüåéè]+\s+than\b(?!\s*\d)", tl_unq):
        return None, ["comparison between two sets -> handled by the planner"]
    if re.search(r"with their|on each|for each|each (?:floor|level|storey)|per (?:floor|level|storey)|what are all|floor slabs|roof slabs", tl_unq):
        return None, ["question asks for a listing / per-level breakdown -> handled by the planner"]
    # Multi-fact check: e.g. asking for both count and area or area and length simultaneously
    distinct_asks = sum([
        bool(re.search(r"\bnumber of\b|\bhow many\b", core_norm) and not COUNT_UNIT_FALSE_RX.search(core_norm)),
        bool(re.search(r"\barea\b", core_norm)),
        bool(re.search(r"\blength\b|\bperimeter\b", core_norm)),
        bool(re.search(r"\bvolume\b", core_norm)),
    ])
    if distinct_asks >= 2 and not THRESHOLD_PATTERNS[0][1].search(tl_unq) and not _extract_threshold(tl_unq, None, None):
        if re.search(r"\band\b", core_norm):
            return None, ["question asks for several facts at once -> handled by the planner (multi-part plan)"]

    # ---- quoted strings: resolve main_t quotes positively, excl_t quotes as exclusions --------------------
    main_quotes = QUOTE.findall(main_t)
    excl_quotes = QUOTE.findall(excl_t)
    labels = {_norm(x): x for x in store.labels("room")}
    levels = {_norm(x): x for x in S["levels"]}
    filters = []
    unresolved = []
    excl_labels = []

    model_toks = {
        w for m in (store.meta.get("model_names") or [])
        for w in re.findall(r"[a-z0-9]{4,}", _norm(m))
        if w not in ("default", "model", "project", "level", "floor", "storey", "building")
    }
    for qs in main_quotes:
        n = _norm(qs)
        pos_q = tl.find(n)
        before_q = tl[:pos_q].rstrip(" '‘’„“«" + chr(34)) if pos_q >= 0 else ""
        after_q = tl[pos_q + len(n):].lstrip(" '’“”»" + chr(34)) if pos_q >= 0 else ""
        q_toks = set(re.findall(r"[a-z0-9]{4,}", n))
        if n in levels:
            filters.append({"field": "level", "op": "eq", "value": levels[n]})
        elif n in labels:
            filters.append({"field": "label", "op": "eq", "value": labels[n]})
        elif any(n == _norm(e.get("name")) or n == _norm(e.get("label")) or n == _norm(e.get("number")) for e in store.of("room")):
            filters.append({"field": "name", "op": "match", "value": qs})
        elif store.meta.get("model_names") and any(n == _norm(m) for m in store.meta["model_names"]):
            notes.append(f"'{qs}' is the model/project name -> scope is the whole model")
        elif (
            re.search(r"(?:model|modell|modellen|mudel|mudelis|mudeli|modèle|maquette|modello|malli|mallin|mallissa|mallista|modelo|modelul|modelu|projekt|project|building|gebäude|bâtiment|edificio|bim)\w*\s*$", before_q)
            or re.match(r"^\s*(?:model|modell|modèle|maquette|modello|malli|mudel|modelo|projekt|project|building|bim)\b", after_q)
            or (store.fmt == "ifc" and (q_toks & model_toks) and not re.search(r"\b(?:on|auf|sur|su|op|på|na|room|space|raum|pièce|locale|geschoss|étage|livello|planta|kondygnacj|korrus\w*|kerrok\w*|taso\w*)\s*$", before_q))
            or (store.fmt == "ifc" and len(main_quotes) == 1 and re.search(r"\b(?:in|within|across|throughout|for|entire|whole|dans|im|nel|nello|nella|mallissa|mallista|mudelis|in\s+het|en\s+el|w)(?:\s+(?:the|entire|whole|complete|le|la|il|het|el|całym|hela|kogu))?\s*$", before_q) and not re.search(r"\b(?:room|space|level|floor|storey)\b", before_q[-25:]))
        ):
            notes.append(f"'{qs}' names the model -> scope is the whole model")
        else:
            unresolved.append(qs)

    for qs in excl_quotes:
        n = _norm(qs)
        if n in levels and not any(f["field"] == "level" for f in filters):
            filters.append({"field": "level", "op": "eq", "value": levels[n]})
        elif n in labels:
            excl_labels.append(labels[n])
        else:
            unresolved.append(qs)

    # Unquoted IFC level handling
    if store.fmt == "ifc" and not any(f["field"] == "level" for f in filters):
        lv_match = _resolve_unquoted_ifc_level(tl_unq, S["levels"])
        if lv_match:
            filters.append({"field": "level", "op": "eq", "value": lv_match})
            notes.append(f"resolved unquoted level reference to '{lv_match}'")
        elif not main_quotes and re.search(r"\b(?:level|lvl\.?|storey)\s+\w+|ground floor|first floor|second floor|third floor|basement|underground|attic|\bfloor\s+\d", tl_unq):
            return None, ["level named without quotes -> the planner maps it to the model's level names"]

    # ---- measure / aggregate ------------------------------------------------------------------------------
    full_norm = _norm(tl_unq)
    has_meas_word = bool(re.search(V.VOLUME, full_norm) or re.search(V.SIDE_AREA, full_norm) or re.search(V.AREA, full_norm) or re.search(V.LENGTH, full_norm))
    count_matches = [m.group(0).strip().lower() for m in re.finditer(V.COUNT, core_norm)]
    # In Finnish, 'laske' means both 'count' and 'calculate'; when paired with area/length/volume (and no threshold), it means calculate.
    only_ambig_calc = bool(count_matches) and all(c in ("laske", "loe") for c in count_matches) and has_meas_word and not _extract_threshold(main_t.lower(), None, None)
    raw_count = bool(count_matches) and not only_ambig_calc and not COUNT_UNIT_FALSE_RX.search(tl_unq)
    is_exists = bool(re.search(V.EXISTS, tl_unq)) and not raw_count
    is_count = raw_count or is_exists

    attr = None
    if not is_count:
        if re.search(V.VOLUME, full_norm):
            attr = "volume"
        elif re.search(V.SIDE_AREA, core_norm) or re.search(V.SIDE_AREA, full_norm):
            attr = "side_area"
        elif re.search(V.AREA, full_norm) and not (re.search(r"\blength|länge|\blongueur|\blunghezz|pituu|pikkus|längd|lengte|\blongitud|długoś", core_norm)):
            attr = "area"
        elif re.search(V.LENGTH, full_norm):
            attr = "perimeter" if re.search(r"perimeter|umfang|périmètre|perimetro|omkrets|omtrek|obwód", full_norm) else "length"

    # Terse plural count queries without explicit count verb ("doors lvl 2?", "wc bowls?", "Total windows drawn on this floor plan?")
    if not is_count and attr is None and PLURAL_ENTITY_RX.search(core_norm):
        if not re.match(r"^\s*(?:which|where|who|why)\b", core_norm):
            is_count = True
            notes.append("plural countable entity with no measurement attribute -> interpreted as count")

    if not is_count and attr is None:
        why_not.append("could not tell whether the question asks for a count, an area, a length or a volume")

    # ---- kind ---------------------------------------------------------------------------------------------
    kind, km = V.find_earliest(V.KINDS, core)
    # If 'apartment' matched as a locative/context word ('in apartment A', 'apartment plan', 'tässä asunnossa')
    # while a concrete building element / fixture kind is also present in core, the element kind wins.
    if kind == "apartment" and is_count:
        specific_kinds = [
            (k, rx) for k, rx in V.KINDS
            if k in ("toilet", "washbasin", "bathtub", "shower", "door", "window", "stair", "column", "beam", "slab", "wall", "sanitary", "light", "airterminal")
        ]
        sk, skm = V.find_earliest(specific_kinds, core)
        if sk:
            kind, km = sk, skm
        elif APT_RX.search(t):
            rk, rkm = V.find_earliest([x for x in V.KINDS if x[0] == "room"], core)
            if rk:
                kind, km = rk, rkm

    rq, rm = V.find_earliest(V.RTYPE_IN_Q, core)
    fixture_kinds = ("toilet", "washbasin", "bathtub", "shower")
    if rq and is_count and kind in fixture_kinds and not WC_ROOM_CUE_RX.search(core):
        if not re.search(r"\brooms?\b|räume|pièces|locaux|locali|stanze|vani|huone|tila|rum\b|ruum|ruimte|habitaci|estancia|cuarto|pomieszcze", core):
            rq = None
    if rq and (attr in ("area", "perimeter") or (is_count and rq not in ("wc",)) or WC_ROOM_CUE_RX.search(main_t.lower())):
        if kind in ("toilet", "washbasin", "bathtub", "shower", None, "room", "apartment") or attr:
            if kind == "toilet" and attr:
                notes.append("'toilet' with an area asked -> interpreted as the toilet ROOM (WC), not the toilet bowl")
            kind = "room"
            filters.append({"field": "rtype", "op": "eq", "value": rq})
            notes.append(f"room type '{rq}' from the word '{rm.group(0)}'")

    # Unknown-words safety guard: >= 2 unknown words always defers; 1 unknown word defers if adjacent to the entity noun (modifier)
    unk = unknown_words(core)
    if len(unk) >= 2:
        return None, [f"words outside the rule vocabulary {unk[:6]} -> handled by the planner"]
    if len(unk) == 1:
        u = unk[0]
        target_tok = (rm.group(0) if rq and rm else (km.group(0) if km else "")).strip()
        if not target_tok or re.search(rf"\b{re.escape(u)}\s+{re.escape(target_tok)}|{re.escape(target_tok)}\s+{re.escape(u)}\b", core_norm):
            return None, [f"modifier '{u}' outside the rule vocabulary -> handled by the planner"]

    fx = [k for k, rx in V.KINDS if k in ("toilet", "washbasin", "bathtub", "shower", "door", "window") and re.search(rx, core)]
    excl_unq = QUOTE.sub(" ", excl_tl)
    excl_fx = [k for k, rx in V.KINDS if k in ("toilet", "washbasin", "bathtub", "shower", "door", "window") and re.search(rx, excl_unq)]
    if len(fx) > 1 and is_count and re.search(r"\band\b|\bund\b|\bsowie\b|\bet\b|\bou\b|\bja\b|\bsekä\b|\bning\b|\boch\b|\bog\b|\ben\b|\by\b|\be\b|\bed\b|\bi\b|\boraz\b|,|\+|in total|together|combined|insgesamt|yhteensä|kokku|totalt|totaal|łącznie", core):
        if excl_fx:
            fx = [k for k in fx if k not in excl_fx]
            notes.append(f"excluded fixture kinds {excl_fx} from combined count")
        kind = fx[0] if len(fx) == 1 else fx
        if isinstance(kind, list):
            notes.append(f"several object kinds counted together: {kind}")

    was_apartment_area = False
    if kind == "apartment" and attr in ("area", "perimeter"):
        kind = "room"
        was_apartment_area = True
        notes.append("area of an apartment = sum of the areas of its rooms")
    if kind in ("toilet", "washbasin", "bathtub", "shower") and attr == "area":
        notes.append(f"area of a {kind} fixture asked -> using the room that contains it")
    if kind is None and attr in ("area", "perimeter") and (any(f["field"] in ("label", "name") for f in filters) or NUM_RX.search(main_t)):
        kind = "room"
    if kind is None and filters and any(f["field"] == "label" for f in filters):
        kind = "room"
    if kind is None:
        why_not.append("no known object type (room, door, window, toilet, wall, ...) found in the question")

    # ---- apartment & room-number filters ------------------------------------------------------------------
    m_apt = APT_RX.search(main_t) or APT_RX.search(t)
    if m_apt and kind != "apartment":
        raw_a = (m_apt.group(1) or m_apt.group(2)).upper()
        if S["apartments"]:
            a = raw_a if raw_a in S["apartments"] else (m_apt.group(1) or m_apt.group(2))
            if a in S["apartments"]:
                filters.append({"field": "apartment", "op": "eq", "value": a})
            elif is_count:
                why_not.append(f"apartment '{raw_a}' does not exist on this floor plan")
            else:
                # Keeping the filter on non-existent apartment Z causes execute.py to deterministically refuse
                filters.append({"field": "apartment", "op": "eq", "value": raw_a})
        else:
            why_not.append(f"question asks about apartment '{raw_a}' but no apartments are defined in this drawing")

    numbers = {e.get("number") for e in store.of("room") if e.get("number")}
    for n in NUM_RX.findall(main_t):
        if n in numbers and kind == "room":
            filters = [f for f in filters if f["field"] != "label"]
            filters.append({"field": "number", "op": "eq", "value": n})

    # ---- exclusions & indoor filters ----------------------------------------------------------------------
    excl_rtypes = []
    if kind == "room" and (attr in ("area", "perimeter") or is_count) and not including:
        has_specific_room_filter = any(f["field"] in ("rtype", "label", "number", "name") for f in filters)
        if re.search(V.INDOOR_TOTAL, tl_unq) or (excl_unq and OUTDOOR_TARGET_RX.search(excl_unq)) or (was_apartment_area and not has_specific_room_filter):
            excl_rtypes.append("outdoor")
        if SHAFT_TARGET_RX.search(excl_unq) or (re.search(V.INDOOR_TOTAL, tl_unq) and SHAFT_TARGET_RX.search(tl_unq)):
            excl_rtypes.append("shaft")
        if re.search(r"elevator|lift|ascenseur|ascensore|aufzug|hissi", excl_unq):
            excl_rtypes.append("elevator")
        if excl_unq:
            for r_k, r_rx in V.RTYPE_IN_Q:
                if r_k != "outdoor" and re.search(r_rx, excl_unq):
                    pos_rt = next((f["value"] for f in filters if f["field"] == "rtype" and f["op"] == "eq"), None)
                    if r_k != pos_rt:
                        excl_rtypes.append(r_k)
        if excl_rtypes:
            dedup_ex = list(dict.fromkeys(excl_rtypes))
            pos_rt = next((f["value"] for f in filters if f["field"] == "rtype" and f["op"] == "eq"), None)
            if pos_rt is None:
                filters.append({"field": "rtype", "op": "nin", "value": dedup_ex})
                notes.append(f"excluding room types {dedup_ex} as the question asks")
        if excl_labels:
            filters.append({"field": "label", "op": "nin", "value": excl_labels})
            notes.append(f"excluding room labels {excl_labels} as the question asks")

    # Safety check: if an exclusion clause was present, make sure we actually handled it (or it's a known clarification)
    if excl_unq.strip():
        handled_excl = bool(excl_rtypes or excl_labels or excl_fx or DOOR_LEAF_CLARIF_RX.search(excl_unq))
        if not handled_excl:
            why_not.append("exclusion clause specifies a condition outside the rule layer -> handled by the planner")

    # ---- combined room types: "bedrooms and bathrooms" ----------------------------------------------------
    if kind == "room":
        rts = [k for k, rx in V.RTYPE_IN_Q if re.search(rx, core)]
        if len(rts) > 1 and re.search(r"\band\b|\bund\b|\bet\b|\bja\b|\boch\b|\bog\b|\ben\b|\by\b|\be\b|\bi\b|\+", core):
            filters = [f for f in filters if not (f["field"] == "rtype" and f["op"] == "eq")] + [{"field": "rtype", "op": "in", "value": rts}]
            notes.append(f"several room types asked together: {rts}")

    # ---- numeric thresholds: 'at least 15 m²', 'more than 20 m²', '10 m² or more', etc. -------------------
    thr = _extract_threshold(main_t.lower(), kind, attr) if kind else None
    if thr:
        fld, fop, val, matched_span = thr
        filters.append({"field": fld, "op": fop, "value": val})
        notes.append(f"threshold: {fld} {fop} {val:g}")
        rem_after_thr = main_t.lower().replace(matched_span, " ")
        if attr == fld and not is_count and not re.search(V.AREA if fld == "area" else V.LENGTH, rem_after_thr):
            attr = None
    else:
        # Safety: if an unparsed number + unit appears in core (not a room number or m2/m3 exponent), defer!
        core_no_rooms = NUM_RX.sub(" ", core_norm)
        core_no_rooms = re.sub(r"\b(?:m2|m3|ft2|cm2)\b", " ", core_no_rooms)
        core_no_rooms = re.sub(r"\b(?:level|lvl\.?|lv\.?|storey|floor)\s*\d+\b", " ", core_no_rooms)
        if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:m²|m³|sqm|cm|mm|ft|m\b)", core_no_rooms):
            why_not.append("numeric condition not parsed by rules -> handled by the planner")

    # ---- unit conversion ----------------------------------------------------------------------------------
    convert = None
    if re.search(r"square\s+f(?:ee|oo)t|sq\.?\s*ft\b|ft²|\bft2\b|\bsf\b|quadratfu(?:ß|ss)|pieds?\s+carrés?|piedi\s+quadrati|pies?\s+cuadrados?|stop\w*\s+kwadratow\w*|vierkante\s+voet|neliöjalk\w*|kvadratfot|ruutjalg\w*", tl_unq):
        convert = "ft2"
    elif re.search(r"square\s+centimet\w*|sq\.?\s*cm\b|cm²|\bcm2\b|quadratzentimet\w*|centimètres?\s+carrés?|centimetri\s+quadrati|centímetros?\s+cuadrados?|centymetr\w*\s+kwadratow\w*|vierkante\s+centimeter\w*", tl_unq):
        convert = "cm2"
    elif re.search(r"\bin\s+(?:feet|foot|ft|fuss|fuß|pieds?|piedi|pies)\b|\bfeet\b", tl_unq) and attr in ("length", "perimeter", "width"):
        convert = "ft"
    elif re.search(r"\b(?:millimet\w*|millimètr\w*|millimetr\w*|milímetr\w*|millimeet\w*|milimetr\w*|mm)\b", tl_unq) and attr in ("length", "perimeter", "width"):
        convert = "mm"
    elif re.search(r"\b(?:centimet\w*|zentimet\w*|centimètr\w*|centimetr\w*|centímetr\w*|senttimeet\w*|sentimeet\w*|centymetr\w*|cm)\b", tl_unq) and attr in ("length", "perimeter", "width"):
        convert = "cm"
    if convert and attr in ("area", "side_area") and convert not in ("ft2", "cm2"):
        convert = None
    if convert:
        notes.append(f"result converted to {convert}")

    # ---- op -----------------------------------------------------------------------------------------------
    if is_exists:
        op = "exists"
    elif is_count:
        op = "count"
    elif re.search(V.LARGEST, tl_unq):
        op = "max"
    elif re.search(V.SMALLEST, tl_unq):
        op = "min"
    elif re.search(V.AVERAGE, tl_unq):
        op = "avg"
    else:
        op = "sum"

    if unresolved and (kind in ("room",) and not any(f["field"] in ("label", "name", "number", "level") for f in filters) or len(unresolved) >= 1 and store.fmt == "ifc"):
        why_not.append(f"quoted text {unresolved} does not match any room label, room name or level in this drawing")
    sup = store.meta.get("supports")
    ks = kind if isinstance(kind, list) else [kind]
    if sup and kind and not all(k in sup or (k in ("shower", "bathtub") and "shower_or_bath" in sup) for k in ks):
        why_not.append(f"this drawing's extractor ({store.meta.get('extractor')}) does not produce '{kind}' entities -> handled by the planner")
    if why_not:
        return None, why_not
    out = {"op": op, "kind": kind, "attr": attr, "filters": filters, "group_by": None, "notes": notes}
    if convert:
        out["convert"] = convert
    return out, []
