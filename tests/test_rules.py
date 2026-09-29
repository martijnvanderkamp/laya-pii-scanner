"""Rule-level tests. They use a stand-in for Laya, so they need no model, GPU or network."""
import pytest

from laya_pii_scanner import PIIScanner, redact
from laya_pii_scanner.scanner import chunk_spans, elfproef, iban_valid, luhn_valid, sentence_spans


class FakeRouter:
    """Answers every Laya question with the same low (or chosen) probability."""

    def __init__(self, noul=0.0):
        self.noul = noul

    def predict_batch(self, requests, batch_size=None, sort_by_length=False):
        results = []
        for req in requests:
            answers = {}
            for qid, q in req["questions"].items():
                if q["type"] == "choice":
                    labels = list(q["criteria"])
                    answers[qid] = {"choice": labels[-1],
                                    "probabilities": {k: float(k == labels[-1]) for k in labels}}
                else:
                    answers[qid] = {"noul": self.noul}
            results.append({"answers": answers})
        return results


def categories(text, noul=0.0):
    chunks = PIIScanner(router=FakeRouter(noul)).scan(text)
    return {f.category for ch in chunks for f in ch.findings if f.confirmed}


# ---------------------------------------------------------------- checksums

@pytest.mark.parametrize("value, ok", [
    ("NL91ABNA0417164300", True),
    ("NL91 ABNA 0417 1643 00", True),
    ("GB33BUKB20201555555555", True),
    ("NL91ABNA0417164301", False),
])
def test_iban(value, ok):
    assert iban_valid(value) is ok


@pytest.mark.parametrize("value, ok", [
    ("4111 1111 1111 1111", True),
    ("5500 0000 0000 0004", True),
    ("4111 1111 1111 1112", False),
])
def test_luhn(value, ok):
    assert luhn_valid(value) is ok


@pytest.mark.parametrize("value, ok", [
    ("123456782", True),
    ("111222333", True),
    ("482913756", False),
    ("111111111", False),
])
def test_elfproef(value, ok):
    assert elfproef(value) is ok


# ---------------------------------------------------------------- rules

def test_bsn_with_cue_and_iban():
    assert {"id_number", "bank"} <= categories("Mijn BSN is 123456782 en mijn IBAN is NL91ABNA0417164300.")


def test_passport_code_with_cue():
    assert "id_number" in categories("Paspoortnummer NR4KD7P62, afgegeven in 2022.")


def test_personal_contact_but_not_role_mailbox():
    assert "contact" in categories("Bel me op 06-12345678 of mail jan.jansen@example.com.")
    assert "contact" not in categories("Vragen? Mail naar info@example.com.")


def test_ip_but_not_version_number():
    assert "online_id" in categories("Inlogpoging vanaf 192.0.2.10 geblokkeerd.")
    assert "online_id" not in categories("INFO firmware 4.12.0.7 uitgerold.")


def test_home_address_confirmed_business_address_for_review():
    assert "address" in categories("Hij woont op de Kerkstraat 12, 3511 AB Utrecht.")
    chunks = PIIScanner(router=FakeRouter()).scan("Het hoofdkantoor zit op Industrieweg 40 in Tilburg.")
    found = [f for ch in chunks for f in ch.findings if f.category == "address"]
    assert found and all(f.review and not f.confirmed for f in found)


def test_birth_date_with_cue():
    assert "dob" in categories("Jan (geboren 3 maart 1985) komt morgen langs.")
    assert "dob" not in categories("De vergadering is op 3 maart 2026.")


def test_names_with_title_initials_greeting_or_sign_off():
    assert "name" in categories("Nieuwe relatie: mevr. J. de Boer.")
    assert "name" in categories("Met vriendelijke groet,\nSanne Kuipers")
    assert "name" in categories("Hi Rachel, thanks for the update.")


def test_cues_match_whole_words_only():
    # "Nair" ends in "ir" (the Dutch engineer title) and "Bosman" in "man": neither is a cue
    chunks = PIIScanner(router=FakeRouter()).scan("Kind regards,\nPriya Nair\nPeople Team")
    names = {f.text for ch in chunks for f in ch.findings if f.category == "name" and f.confirmed}
    assert names == {"Priya Nair"}
    assert "name" not in categories("Contactpersoon: Kees Bosman\nAfdeling Inkoop")


def test_business_cue_only_in_the_same_sentence():
    text = "She works in our Leeds office. She lives at 41 Harehills Grove, Leeds LS8 4DX."
    assert "address" in categories(text)


def test_clean_text_stays_clean():
    chunks = PIIScanner(router=FakeRouter()).scan("Philips heeft vandaag nieuwe kwartaalcijfers gepubliceerd.")
    assert [ch.verdict for ch in chunks] == ["clean"]


def test_sensitive_keyword_needs_laya_to_agree():
    assert "health" not in categories("Zij heeft diabetes.", noul=0.0)
    assert "health" in categories("Zij heeft diabetes.", noul=0.9)


def test_redact():
    text = "Bel Jan op 06-12345678, IBAN NL91ABNA0417164300."
    chunks = PIIScanner(router=FakeRouter()).scan(text)
    out = redact(text, chunks)
    assert "06-12345678" not in out and "NL91ABNA0417164300" not in out
    assert "[CONTACT]" in out and "[IBAN]" in out


# ---------------------------------------------------------------- chunking

def test_chunks_respect_max_chars():
    text = " ".join(f"Dit is zin nummer {i}." for i in range(100))
    spans = chunk_spans(text, max_chars=200)
    assert len(spans) > 1
    assert all(b - a <= 200 for a, b in spans)


def test_sentence_spans():
    chunk = "Eerste zin. Tweede zin!\nDerde regel"
    assert [chunk[a:b] for a, b in sentence_spans(chunk)] == ["Eerste zin.", "Tweede zin!", "Derde regel"]
