"""A stand-in for Laya's Router, so tests need no model, GPU or network."""


class FakeRouter:
    """Answers every Laya question the same way: yes/no questions with `noul`, and choice
    questions with the last option, except that `person` is the probability of "person"."""

    def __init__(self, noul=0.0, person=0.0):
        self.noul = noul
        self.person = person
        self.requests = 0

    def predict_batch(self, requests, batch_size=None, sort_by_length=False):
        results = []
        self.requests += len(requests)
        for req in requests:
            answers = {}
            for qid, q in req["questions"].items():
                if q["type"] == "choice":
                    labels = list(q["criteria"])
                    probs = {k: float(k == labels[-1]) for k in labels}
                    if "person" in probs and self.person:
                        probs = {k: (self.person if k == "person" else 0.0) for k in labels}
                    answers[qid] = {"choice": max(probs, key=probs.get), "probabilities": probs}
                else:
                    answers[qid] = {"noul": self.noul}
            results.append({"answers": answers})
        return results
