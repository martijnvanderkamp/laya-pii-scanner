"""Core of laya-pii-scanner: find personal data in Dutch and English text.

Code finds the fixed formats and checks them (e-mail, phone, IBAN mod-97, BSN 11-proef,
payment card Luhn, IP address, street address, dates) and applies context cues ("BSN",
"geboren", "mevr.", "B.V."). Laya judges what needs meaning: whether a capitalised word is a
person, whether a valid 9-digit number without a cue is a BSN, whether a date is a birth
date, and whether a sentence says something sensitive about a specific person (health,
belief, sexuality, politics, union, origin, crime). Keywords ("diabetes", "moskee") only
mark where.

    from laya_pii_scanner import PIIScanner, redact
    scanner = PIIScanner()
    chunks = scanner.scan(text)
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- categories and thresholds

# category -> (label, redaction token)
CATEGORIES = {
    "name": ("Name", "NAME"),
    "contact": ("Email or phone", "CONTACT"),
    "address": ("Address", "ADDRESS"),
    "id_number": ("National ID or passport", "ID"),
    "bank": ("Bank account", "IBAN"),
    "card": ("Payment card", "CARD"),
    "dob": ("Date of birth", "DOB"),
    "online_id": ("IP address", "IP"),
    "health": ("Health", "HEALTH"),
    "sexuality": ("Sexual orientation", "SEXUALITY"),
    "religion": ("Religion or belief", "RELIGION"),
    "politics": ("Political opinion", "POLITICS"),
    "union": ("Trade union membership", "UNION"),
    "ethnicity": ("Ethnic origin", "ETHNICITY"),
    "criminal": ("Criminal record", "CRIMINAL"),
}
SPECIAL = ("health", "sexuality", "religion", "politics", "union", "ethnicity", "criminal")

# One place for every threshold. Tuned with eval/evaluate.py --sweep (see "Evaluate and tune" in the README).
THRESHOLDS = {
    "name": 0.70,           # P(person) for a capitalised candidate
    "id_number": 0.50,      # P(personal ID) for a 9-digit number that passes the 11-proef
    "dob": 0.50,            # P(birth date) for a date with a year
    "special": 0.05,        # P(chunk says this about a specific person), when a keyword occurs
    "chunk_special": 0.80,  # the same question without a keyword; asked per sentence, so kept strict
    "review": 0.30,         # below its threshold but above this: shown for review
}

# ---------------------------------------------------------------- patterns

EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ROLE_MAILBOX = re.compile(
    r"^(?:info|contact|support|help|helpdesk|hr|admin|administratie|sales|verkoop|office|kantoor|"
    r"noreply|no-reply|service|klantenservice|facturen|factuur|finance|billing|team|hello|hallo|"
    r"receptie|secretariaat|privacy|post|mail|webmaster|jobs|vacatures|pers|press)@", re.I)
PHONE = re.compile(r"""(?<![\w+])(?:
      (?:\+|00)\d{1,3}[\s.-]?(?:\(0\)[\s.-]?)?\d{1,4}(?:[\s.-]?\d{2,4}){2,4}
    | 0\d{1,4}[\s.-]?\d{2,4}(?:[\s.-]?\d{2,4}){1,3}
    )(?!\w)""", re.X)
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
CARD = re.compile(r"(?<![\w-])[3-6]\d{3}(?:[ -]?\d){9,15}(?![\w-])")
NINE_DIGITS = re.compile(r"(?<![\w.,/-])(?:\d{9}|\d{4}\.\d{2}\.\d{3})(?![\w/-]|[.,]\d)")
IP = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.]|\.\d)")

_POSTCODE = r"[1-9]\d{3}[ ]?[A-Z]{2}"
_UK_POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]?[ ]?\d[A-Z]{2}"
_SUFFIX_NL = (r"(?:straat|laan|weg|plein|gracht|kade|singel|dijk|dreef|hof|park|markt|steeg|pad|baan|wal|"
              r"burgwal|erf|veld|plantsoen|boulevard|haven|ring|dam|berg|poort)")
_SUFFIX_EN = (r"(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Close|Way|Place|Square|Crescent|Court|"
              r"Gardens|Terrace|Grove|Row|Hill|Mews|Walk|View|Rise|Park|Green|Parade|Vale|Wharf|Yard)")
ADDRESS = re.compile(rf"""(?:
      \b[A-Z][\w'’-]*?{_SUFFIX_NL}[ ]\d+(?:[ ]?[a-zA-Z](?!\w)|-\d+)?(?:,?[ ]{_POSTCODE}(?:[ ][A-Z][\w'’-]+)?)?
    | \b\d+[a-zA-Z]?[ ](?:[A-Z][\w'’-]*[ ]){{1,3}}{_SUFFIX_EN}(?:,[ ][A-Z][\w'’-]+)?(?:,?[ ]{_UK_POSTCODE})?
    | \b{_POSTCODE},?[ ](?:huisnummer|nummer|nr\.?|no\.?)[ ]?\d+[a-zA-Z]?\b
    | \b{_POSTCODE}[ ]\d+[a-zA-Z]?\b
    )""", re.X)
# An address in a chunk with one of these words is probably a business address: shown for review.
BUSINESS_CUE = re.compile(
    r"(?<!\w)(?:b\.?v\.?|n\.?v\.?|v\.o\.f\.|hoofdkantoor|kantoor|vestiging|bedrijf\w*|winkel|magazijn|"
    r"distributiecentrum|fabriek|showroom|pand|headquarters|head office|offices?|store|shop|warehouse|"
    r"factory|company|ltd|inc|gmbh|plc|llc)(?!\w)", re.I)
# Cue patterns are matched against the text just before a candidate. Each starts at a word
# boundary and ends with \Z, so "Nair" never reads as the title "ir" and "$" never skips a newline.
VERSION_CUE = re.compile(r"(?<!\w)(?:versie|version|firmware|release|build|v)\.?[ \t]*\Z", re.I)
# A number or code right after one of these words is a personal ID, with or without a checksum.
ID_CUE = re.compile(
    r"(?<!\w)(?:bsn|burgerservicenummer|sofi-?nummer|citizen service number|passport|paspoort|rijbewijs|"
    r"driver'?s licen[cs]e|driving licen[cs]e|id-kaart|identiteitskaart|identity card|id card|"
    r"documentnummer|document number)\w*\W{0,6}(?:\w+\W+){0,2}\Z", re.I)
ID_CODE = re.compile(r"(?<![\w.-])(?=[A-Z0-9.]*\d)[A-Z0-9](?:[A-Z0-9]|\.(?=\w)){5,13}(?![\w-])")
# Words that make the capitalised word after them a person's name: titles, relations,
# greetings, and a sign-off on the line before.
PERSON_CUE = re.compile(
    r"(?<!\w)(?:dhr|mevr|mw|meneer|mevrouw|mr|mrs|ms|miss|dr|drs|prof|ir|collega|colleague|patiënte?|"
    r"cliënte?|zoon|dochter|broer|zus|moeder|vader|man|vrouw|son|daughter|brother|sister|mother|"
    r"father|wife|husband|hi|hoi|hallo|hello|hey|dear|beste|geachte)\.?[ \t]*\Z"
    r"|(?<!\w)(?:groet|groeten|regards|sincerely|cheers|vriendelijke groet|hartelijke groet),?[ \t]*\n\s*\Z",
    re.I)
INITIALS_NAME = re.compile(r"^(?:[A-Z]\.[ ]?)+(?:(?:van|de|der|den|te|ter|ten)[ ]+)*[A-Z][a-zà-öø-ÿ]")

_MONTHS = (r"januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|"
           r"january|february|march|may|june|july|august|october|"
           r"jan|feb|mrt|mar|apr|jun|jul|aug|sep|sept|okt|oct|nov|dec")
DATE = re.compile(rf"""\b(?:
      \d{{1,2}}[-/.]\d{{1,2}}[-/.](?:19|20)\d{{2}}
    | \d{{1,2}}(?:st|nd|rd|th)?[ ](?:{_MONTHS})\.?,?[ ](?:19|20)\d{{2}}
    | (?:{_MONTHS})\.?[ ]\d{{1,2}}(?:st|nd|rd|th)?,?[ ](?:19|20)\d{{2}}
    | (?:19|20)\d{{2}}-\d{{2}}-\d{{2}}
    )\b""", re.X | re.I)
BIRTH_CUE = re.compile(r"(?<!\w)(?:geboren|geb\.|geboortedatum|born|date of birth|d\.o\.b\.|dob|birthday|"
                       r"verjaardag)\W{0,4}(?:\w+\W+){0,2}\Z", re.I)

_CAP = r"[A-ZÀ-ÖØ-Þ][\w'’-]*[a-zà-öø-ÿ][\w'’-]*"
_INITIAL = r"[A-Z]\."
_TOKEN = rf"(?:{_CAP}|{_INITIAL})"
_PARTICLE = r"(?:van|de|der|den|het|te|ten|ter|in|op|'t|el|al|bin|ben|la|le|du|da|di|von|zu|dos|das|del)"
_SEP = rf"(?:[ \t]+(?:{_PARTICLE}[ \t]+)*|(?<=\.)|[ \t]*&[ \t]*)"
NAME_CANDIDATE = re.compile(rf"(?<![\w@.]){_TOKEN}(?:{_SEP}{_TOKEN})*")
STOP = set("""
de het een the a an i ik je jij u hij zij ze wij we jullie mijn onze ons our my your his her their
dit deze die dat this that these those er there hi hallo hoi hey dear beste geachte groeten regards
kind met vriendelijke bedankt thanks please graag als if when wanneer na voor in op bij van per via
volgens according gisteren vandaag morgen yesterday today tomorrow ook also maar but en and of or
want omdat because echter however let note ps re fw fwd subject onderwerp aan to from cc bcc datum date
maandag dinsdag woensdag donderdag vrijdag zaterdag zondag monday tuesday wednesday thursday friday
saturday sunday januari februari maart april mei juni juli augustus september oktober november
december january february march may june july august october
meneer mevrouw dhr mevr mr mrs ms miss dr prof ir drs ing sir madam patiënt patiënte patient
he she it they them him hers its you us who what which where why how wie wat welke waar hoe zijn haar hun
hierbij bijgaand bijgevoegd helaas alvast verder tevens daarnaast namens betreft inmiddels zoals hoewel
toen nadat sinds tijdens zodra dus daarom hier daar nu dan eerst daarna tenslotte kortom overigens
uiteraard natuurlijk excuses sorry hereby attached unfortunately furthermore regarding since during
after before while although so then now here first finally honestly basically actually just still yes
no ok okay kun kan kunt wil zou mag moet heeft hebben ben can could would should will might must have
has had does did are were
""".split())
PARTICLES = set("van de der den het te ten ter el al la le du da di von".split())

# Sensitive words per category. A trailing * matches any ending. Laya decides whether each hit
# is about a specific person ("zij heeft diabetes") or not ("diabetes komt veel voor").
LEXICON = {
    "health": """ziek* ziekte* ziekenhuis* huisarts* diagnos* diabetes kanker* depressi* burn-out burnout*
        overspannen* medicatie* medicijn* antidepressiva therapie* therapeut* psycholo* psychiat* zwanger*
        operatie* geopereerd hiv adhd autis* astma patiënt* behandel* insuline chemo* revalid*
        arbeidsongeschikt* beperking* gehandicapt* allergi* hartaanval* herseninfarct* dementie* verslav*
        klacht* pijn* illness* ill disease* cancer depress* antidepressant* therap* hospital* medication*
        medicine* pregnan* surger* treatment* disabilit* disabled chronic* migraine* anxiety panic* stroke
        dementia addict* rehab* sick* symptom* pain* hernia suiker* epilep* parkinson* alzheimer* reuma*
        artros* sikkel* sickle* anemi* bloedarmoede blind* doof deaf* rolstoel* wheelchair* bevall* baby
        expecting maternity herstel* recover* opname opgenomen admitted fysio* physio* bloedonderzoek*""",
    "sexuality": """homo* lesbi* biseksu* bisexu* transgender* transseksu* queer non-binair* nonbinary
        non-binary gay heterosexu* heteroseksu* geaardheid orientation coming-out uit_de_kast came_out
        coming_out""",
    "religion": """katholie* catholic* protestant* christen* christian* christelijk* moslim* muslim* islam*
        joods jood joden jewish jew hindoe* hindu* boeddhis* buddhis* atheïst* atheist* gelovig* geloof*
        religi* kerk* church* moskee* mosque* synago* ramadan kosher halal gebed* bidden pray* bijbel*
        bible koran* quran gereformeerd* hervormd* evangeli* orthodox* sikh* vasten fasting eid shabbat
        sabbat* hoofddoek* headscarf*""",
    "politics": """partij* party stemt stemde stemmen vote* voted voting vvd pvv d66 cda groenlinks pvda
        sp bbb nsc fvd ja21 christenunie sgp volt labour tory tories conservative* democrat* republican*
        socialis* communis* liberal* activist* demonstr* protest* groenen greens canvass* campagne*
        campaign* verkiezing* election* raadslid* councillor*""",
    "union": """vakbond* fnv cnv union* vakbondslid*""",
    "ethnicity": """afkomst* herkomst* etnisch* etnici* ethnic* race racial allochto* autochto*
        migratieachtergrond* migrant* vluchteling* refugee* asielzoeker* asylum marokkaan* turk* turks
        surinaam* antilliaan* indonesi* pools somali* eritre* syri* afghaan* nigeria* ghanees* chinees*
        chinese moroccan* turkish surinamese african* afrikaan* aziat* asian* latino* hispanic* zwart*
        black descent heritage afkomstig nationaliteit* nationality""",
    "criminal": """arrest* gearresteerd aangehouden opgepakt veroordeeld* veroordeling* strafblad* gevangen*
        gevangenis* cel celstraf taakstraf* werkstraf* verdacht* strafzaak* aangifte* politie* justitie*
        delict* misdrijf* fraude* diefstal* mishandel* inbraak* convicted conviction* criminal* prison*
        jail* suspect* charged custody police* fraud* theft* assault* burglar* sentenced probation parole
        offender* offence* offense* strafrecht* rechtbank* court trial detentie reclassering* enkelband*""",
}
# Asked once per chunk as "Does `text` say that one specific person ...?". This whole-chunk
# statement separated personal from general mentions far better than asking about each
# keyword (AUC 0.94 vs 0.62 on eval/data/tune.jsonl, see eval/experiment_special.py).
SPECIAL_STATEMENT = {
    "health": "is ill, has a medical condition, is pregnant or receives medical treatment",
    "sexuality": "has a particular sexual orientation or gender identity",
    "religion": "has a particular religion or belief",
    "politics": "supports or is a member of a political party or votes for one",
    "union": "is a member of a trade union",
    "ethnicity": "has a particular ethnic origin, nationality of origin or migration background",
    "criminal": "was arrested, suspected, convicted or imprisoned",
}


def _lexicon_regex(words: str) -> re.Pattern:
    parts = []
    for w in words.split():
        stem = re.escape(w.rstrip("*")).replace("_", r"\s+")
        parts.append(stem + r"\w*" if w.endswith("*") else stem)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.I)


LEXICON_RE = {cat: _lexicon_regex(words) for cat, words in LEXICON.items()}

# ---------------------------------------------------------------- Laya questions

WORD_Q = {"what": {
    "type": "choice",
    "instructions": "In `text`, what does `candidate` refer to?",
    "criteria": {"person": "a person's first name, surname or full name",
                 "organisation": "a company, brand, product, institution, party or team",
                 "place": "a street, city, region, country or building",
                 "other": "a common word, title, weekday, month or anything else"}}}
NUMBER_Q = {"what": {
    "type": "choice",
    "instructions": "In `text`, what is the number `candidate`?",
    "criteria": {"citizen_id": "a personal identification number such as a citizen service number (BSN) or passport number",
                 "bank_account": "a bank account number or payment card number",
                 "phone": "a telephone number",
                 "reference": "an order, invoice, ticket, article, product or other reference number",
                 "other": "an amount, quantity, year, date or anything else"}}}
DOB_Q = {"dob": {"type": "noul", "instructions": "In `text`, is `candidate` the date of birth of a person?"}}
CHUNK_Q = {cat: {"type": "noul", "instructions": f"Does `text` say that one specific person {verb}?"}
           for cat, verb in SPECIAL_STATEMENT.items()}

# ---------------------------------------------------------------- results


@dataclass
class Finding:
    category: str
    text: str
    start: Optional[int]      # absolute offsets in the scanned text; None for whole-chunk findings
    end: Optional[int]
    source: str               # "rule" (pattern, checksum or cue) or "laya"
    confidence: float
    threshold: float = 1.0

    @property
    def confirmed(self) -> bool:
        return self.confidence >= self.threshold

    @property
    def review(self) -> bool:
        return not self.confirmed and self.confidence >= _review_floor(self.threshold)


def _review_floor(threshold: float) -> float:
    return min(THRESHOLDS["review"], threshold / 2)


def _kept(confidence: float, threshold: float) -> bool:
    return confidence >= _review_floor(threshold)


@dataclass
class ChunkResult:
    start: int
    end: int
    text: str
    findings: List[Finding] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """"special" (sensitive data), "personal", "review" (only uncertain findings) or "clean"."""
        confirmed = [f for f in self.findings if f.confirmed]
        if any(f.category in SPECIAL for f in confirmed):
            return "special"
        if confirmed:
            return "personal"
        if any(f.review for f in self.findings):
            return "review"
        return "clean"

    def categories(self) -> set:
        return {f.category for f in self.findings if f.confirmed}


# ---------------------------------------------------------------- checks


def iban_valid(s: str) -> bool:
    s = s.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def luhn_valid(s: str) -> bool:
    d = [int(c) for c in re.sub(r"\D", "", s)]
    if not 13 <= len(d) <= 19:
        return False
    total = sum(d[-1::-2]) + sum(sum(divmod(2 * x, 10)) for x in d[-2::-2])
    return total % 10 == 0


def elfproef(s: str) -> bool:
    d = [int(c) for c in re.sub(r"\D", "", s)]
    if len(d) != 9 or d[0] == d[1] == d[2] == d[3] == d[4] == d[5] == d[6] == d[7] == d[8]:
        return False
    return (sum(w * x for w, x in zip(range(9, 1, -1), d[:8])) - d[8]) % 11 == 0


def phone_plausible(s: str) -> bool:
    n = len(re.sub(r"\D", "", s))
    return (9 <= n <= 15) if s.lstrip().startswith(("+", "00")) else (10 <= n <= 11)


# ---------------------------------------------------------------- chunking


def chunk_spans(text: str, max_chars: int = 500) -> List[Tuple[int, int]]:
    """Pack sentences and paragraphs into chunks of at most ~max_chars."""
    bounds = [0] + [m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n\s*\n", text)] + [len(text)]
    segments = [(a, b) for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]
    spans: List[Tuple[int, int]] = []
    for a, b in segments:
        while b - a > 2 * max_chars:  # one huge sentence: cut at a space
            cut = text.rfind(" ", a, a + max_chars)
            cut = cut if cut > a else a + max_chars
            spans.append((a, cut))
            a = cut
        if spans and b - spans[-1][0] <= max_chars and not re.search(r"\n\s*\n", text[spans[-1][1]:a]):
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))
    out = []
    for a, b in spans:
        chunk = text[a:b]
        a += len(chunk) - len(chunk.lstrip())
        b -= len(chunk) - len(chunk.rstrip())
        if b > a:
            out.append((a, b))
    return out


def sentence_spans(chunk: str) -> List[Tuple[int, int]]:
    """Sentences and lines within a chunk, whitespace trimmed."""
    bounds = [0] + [m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n+", chunk)] + [len(chunk)]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        piece = chunk[a:b]
        a2, b2 = a + len(piece) - len(piece.lstrip()), b - (len(piece) - len(piece.rstrip()))
        if b2 > a2:
            out.append((a2, b2))
    return out


# ---------------------------------------------------------------- scanner


class PIIScanner:
    """Scan text for personal data.

    device: "cuda" or "cpu"; defaults to CUDA when available.
    router: an existing `laya.Router` to reuse (or any object with a compatible `predict_batch`).
    chunk_check: also flag sensitive categories that no keyword pointed at (the safety net).

    Creating a scanner loads Laya's English and multilingual checkpoints once (downloading them
    from Hugging Face on first use); reuse the instance for every text.
    """

    def __init__(self, device: Optional[str] = None, router=None, chunk_check: bool = True):
        if router is None:
            import torch
            from laya import Router
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                router = Router(device=device or ("cuda" if torch.cuda.is_available() else "cpu"))
                router.preload(["english", "multilingual"])
        self.router = router
        self.chunk_check = chunk_check

    # -- step 1: patterns, and the candidates that need Laya ------------------------------
    def _patterns(self, chunk: str) -> Tuple[List[Finding], List[dict]]:
        found: List[Finding] = []
        todo: List[dict] = []
        taken: List[Tuple[int, int]] = []

        def free(a, b):
            return all(b <= x or a >= y for x, y in taken)

        def add(cat, a, b, conf=1.0, src="rule", thr=1.0):
            found.append(Finding(cat, chunk[a:b], a, b, src, conf, thr))
            taken.append((a, b))

        def cue(rx, a, width=40):
            return rx.search(chunk[max(0, a - width):a])

        for m in IBAN.finditer(chunk):
            s = m.group()
            for cut in range(len(s), 14, -1):  # the pattern may run into a following word
                if iban_valid(s[:cut]):
                    add("bank", m.start(), m.start() + len(s[:cut].rstrip()))
                    break
        for m in EMAIL.finditer(chunk):
            if free(*m.span()):
                taken.append(m.span())
                if not ROLE_MAILBOX.match(m.group()):
                    add("contact", *m.span())
        for m in CARD.finditer(chunk):
            if free(*m.span()) and luhn_valid(m.group()):
                add("card", *m.span())
        for m in IP.finditer(chunk):
            if free(*m.span()) and not cue(VERSION_CUE, m.start(), 20):
                add("online_id", *m.span())
        for m in DATE.finditer(chunk):
            if free(*m.span()):
                taken.append(m.span())
                if cue(BIRTH_CUE, m.start()):
                    add("dob", *m.span())
                else:
                    todo.append({"kind": "dob", "span": m.span(), "questions": DOB_Q})
        for m in ID_CODE.finditer(chunk):  # "BSN 3185.74.627", "paspoortnummer NR4KD7P62"
            if free(*m.span()) and cue(ID_CUE, m.start()):
                add("id_number", *m.span())
        for m in NINE_DIGITS.finditer(chunk):  # a valid 11-proef number without a cue: ask Laya
            if free(*m.span()) and elfproef(m.group()):
                taken.append(m.span())
                todo.append({"kind": "id_number", "span": m.span(), "questions": NUMBER_Q})
        for m in PHONE.finditer(chunk):
            if free(*m.span()) and phone_plausible(m.group()):
                add("contact", *m.span())
        sentences = sentence_spans(chunk)
        for m in ADDRESS.finditer(chunk):
            if free(*m.span()):
                # a business word in the same sentence ("hoofdkantoor", "B.V.") makes it a review item
                sa, sb = next(((a, b) for a, b in sentences if a <= m.start() < b), (0, len(chunk)))
                business = BUSINESS_CUE.search(chunk[sa:sb])
                add("address", *m.span(), conf=0.4 if business else 1.0, thr=0.5)

        seen = set()
        for m in NAME_CANDIDATE.finditer(chunk):
            a, b = m.span()
            words = m.group().split()
            # "Beste Jan" -> "Jan", but "De Vries" stays whole
            while words and words[0].strip(".,").lower() in STOP and not (
                    len(words) > 1 and words[0].lower() in PARTICLES):
                a += chunk[a:b].index(words[0]) + len(words[0])
                a += len(chunk[a:b]) - len(chunk[a:b].lstrip())
                words = words[1:]
            if chunk[a:b].endswith(("'s", "’s")):  # "Tom's" -> "Tom"
                b -= 2
            if not words or not free(a, b):
                continue
            cand = chunk[a:b]
            if cand.lower() in STOP or cand in seen:
                continue
            seen.add(cand)
            if INITIALS_NAME.match(cand) or cue(PERSON_CUE, a, 20):  # "T.J. Hoekstra", "mevr. Bakker"
                add("name", a, b, conf=0.95, thr=0.5)
            else:
                todo.append({"kind": "name", "span": (a, b), "questions": WORD_Q})

        keywords = {cat: [m.span() for m in rx.finditer(chunk) if free(*m.span())]
                    for cat, rx in LEXICON_RE.items()}
        return found, todo, keywords

    # -- step 2: Laya ---------------------------------------------------------------------
    @staticmethod
    def _score(kind: str, answers: dict) -> Tuple[float, float]:
        """(confidence that this candidate is PII of `kind`, threshold for `kind`)."""
        if kind == "name":
            return answers["what"]["probabilities"]["person"], THRESHOLDS["name"]
        if kind == "id_number":
            return answers["what"]["probabilities"]["citizen_id"], THRESHOLDS["id_number"]
        return answers["dob"]["noul"], THRESHOLDS["dob"]

    def scan(self, text: str) -> List[ChunkResult]:
        """Split `text` into passages of up to ~500 characters and return one result per passage."""
        chunks =[ChunkResult(a, b, text[a:b]) for a, b in chunk_spans(text)]
        requests, meta, keywords = [], [], []
        for ci, ch in enumerate(chunks):
            found, todo, kw = self._patterns(ch.text)
            ch.findings.extend(found)
            keywords.append(kw)
            for t in todo:
                a, b = t["span"]
                requests.append({"state": {"text": ch.text, "candidate": ch.text[a:b]}, "questions": t["questions"]})
                meta.append((ci, t))
            # Sensitive categories are asked per sentence, with the previous sentence as context so
            # "Hij" still points at someone: on whole 500-character chunks the signal washed out.
            sents = sentence_spans(ch.text)
            for si, (a, b) in enumerate(sents):
                ctx = sents[si - 1][0] if si else a
                requests.append({"state": {"text": ch.text[ctx:b]}, "questions": CHUNK_Q})
                meta.append((ci, {"kind": "sentence", "span": (a, b)}))
        results = self.router.predict_batch(requests, batch_size=32, sort_by_length=True) if requests else []
        safety: Dict[Tuple[int, str], float] = {}
        for (ci, t), res in zip(meta, results):
            ch = chunks[ci]
            if t["kind"] == "sentence":
                sa, sb = t["span"]
                for cat in SPECIAL:
                    p = res["answers"][cat]["noul"]
                    spans = [(a, b) for a, b in keywords[ci][cat] if sa <= a < sb]
                    if spans:  # a keyword marks where; Laya decides whether it is about someone
                        for a, b in spans:
                            if _kept(p, THRESHOLDS["special"]):
                                ch.findings.append(Finding(cat, ch.text[a:b], a, b, "laya", p, THRESHOLDS["special"]))
                    elif self.chunk_check:
                        safety[(ci, cat)] = max(p, safety.get((ci, cat), 0.0))
                continue
            conf, thr = self._score(t["kind"], res["answers"])
            if _kept(conf, thr):
                a, b = t["span"]
                ch.findings.append(Finding(t["kind"], ch.text[a:b], a, b, "laya", conf, thr))
        for (ci, cat), p in safety.items():  # no keyword anywhere in that sentence: the safety net
            ch = chunks[ci]
            if _kept(p, THRESHOLDS["chunk_special"]) and not any(f.category == cat for f in ch.findings):
                ch.findings.append(Finding(cat, "(whole passage)", None, None, "laya", p, THRESHOLDS["chunk_special"]))
        for ch in chunks:  # chunk-relative -> absolute offsets
            for f in ch.findings:
                if f.start is not None:
                    f.start += ch.start
                    f.end += ch.start
            ch.findings.sort(key=lambda f: (f.start is None, f.start or 0))
        return chunks


def redact(text: str, chunks: Iterable[ChunkResult], include_review: bool = False) -> str:
    """Replace every confirmed finding (and, optionally, every review finding) with a token."""
    spans =sorted({(f.start, f.end, f.category) for ch in chunks for f in ch.findings
                    if f.start is not None and (f.confirmed or (include_review and f.review))})
    out, pos = [], 0
    for a, b, cat in spans:
        if a < pos:
            continue
        out.append(text[pos:a])
        out.append(f"[{CATEGORIES[cat][1]}]")
        pos = b
    out.append(text[pos:])
    return "".join(out)
