# ruff: noqa: E501, RUF001
"""Build the CHALLENGE set for question detection (M-7): tests/eval/detection_challenge.jsonl.

    cd services/api && python scripts/build_detection_challenge.py

Written AFTER the detector was frozen, and never used to tune it. It is deliberately harder than the
main set (scripts/build_detection_set.py), because that set was written by the same author, in the same
style and with the same design in mind as the detector, so a perfect score on it proves little. These
examples add what real speech-to-text output looks like and what the main set avoids: fillers and repeated
words, a preamble before the question, tag questions, questions with no question word ("you have used
redis right"), imperatives such as "name three differences", exclamations that begin with a question word,
reported or rhetorical questions with no explicit cue, and a candidate's own account of a question.

Everything is invented. Written by the assistant (an AI); no native-speaker review; no real speech.
Labels follow the same policy as the main set (docs/QUESTION_DETECTION_EVALUATION.md section 2).
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "eval" / "detection_challenge.jsonl"

CHALLENGE: dict[tuple[str, bool], list[tuple[str, str]]] = {
    ("en", True): [
        ("declarative_mark", "you have worked with kafka before?"),
        ("declarative_mark", "so you are comfortable with on call rotations?"),
        ("noise", "um so what uh what is your experience with with docker"),
        ("noise", "so basically what is the the difference between a list and a tuple"),
        (
            "preamble",
            "thanks for that moving on to the next part i would like to ask what is your approach to testing",
        ),
        ("preamble", "that was a good answer so how would you scale it to ten times the traffic"),
        ("tag", "you have used redis right"),
        ("short_followup", "and what about caching"),
        ("imperative", "name three differences between sql and nosql databases"),
        ("imperative", "compare quicksort and mergesort in terms of memory use"),
        ("imperative", "give me a quick overview of your architecture"),
        ("noise", "what is the uh the time complexity of a binary search"),
    ],
    ("en", False): [
        ("exclamation", "what a wonderful opportunity this is"),
        (
            "mid_wh_statement",
            "we discussed how the architecture evolved over the last three years and what we learned",
        ),
        ("account", "i asked myself how the system would scale and decided to shard the database"),
        ("announcement", "now i will ask you a few behavioral questions"),
        (
            "reported",
            "the question was what is the difference between a thread and a process and i answered it",
        ),
        ("reported", "i was thinking about why we picked that framework in the first place"),
        ("filler", "so yeah that is basically what i did on that project"),
        ("filler", "um so uh yeah i think that is about it for me"),
        ("closing", "great thank you i will let the team know about the next steps"),
        ("smalltalk", "how nice to meet you i have heard a lot about your work"),
        ("closing", "we will contact you within one week about the outcome"),
        ("rhetorical", "who would have thought that a small change could cause that outage"),
    ],
    ("hi", True): [
        ("declarative_mark", "आपको python आती है?"),
        ("declarative_mark", "आप रिमोट काम करने के लिए तैयार हैं?"),
        ("noise", "उम तो आप आप अपना अनुभव बताइए"),
        ("noise", "तो बेसिकली docker और kubernetes में क्या फर्क है"),
        ("preamble", "आगे बढ़ते हुए मैं पूछना चाहता हूँ कि आप टेस्टिंग कैसे करते हैं"),
        ("preamble", "यह अच्छा जवाब था तो इसे दस गुना ट्रैफ़िक के लिए कैसे स्केल करेंगे"),
        ("tag", "आपने redis इस्तेमाल किया है ना"),
        ("short_followup", "और caching का क्या"),
        ("imperative", "sql और nosql के बीच तीन अंतर बताइए"),
        ("imperative", "quicksort और mergesort की memory use के हिसाब से तुलना कीजिए"),
        ("imperative", "अपने architecture का एक छोटा सा overview दीजिए"),
        ("noise", "binary search की time complexity क्या होती है"),
    ],
    ("hi", False): [
        ("exclamation", "कितना शानदार मौका है यह"),
        ("mid_wh_statement", "हमने चर्चा की कि क्या करना चाहिए और कैसे करना चाहिए"),
        ("account", "मैंने खुद से सोचा कि यह सिस्टम कैसे स्केल होगा और database को shard करने का फैसला किया"),
        ("announcement", "अब मैं आपसे कुछ behavioral सवाल पूछूँगा"),
        ("reported", "सवाल यह था कि thread और process में क्या अंतर है और मैंने जवाब दे दिया"),
        ("reported", "मैं सोच रहा था कि हमने शुरू में वह framework क्यों चुना था"),
        ("filler", "तो हाँ मैंने उस प्रोजेक्ट में मूल रूप से यही किया"),
        ("filler", "उम तो हाँ मुझे लगता है मेरी तरफ से बस इतना ही"),
        ("closing", "बहुत बढ़िया धन्यवाद मैं टीम को अगले चरणों के बारे में बता दूँगा"),
        ("smalltalk", "आपसे मिलकर बहुत अच्छा लगा मैंने आपके काम के बारे में बहुत सुना है"),
        ("closing", "हम एक हफ्ते के भीतर आपसे नतीजे के बारे में संपर्क करेंगे"),
        ("rhetorical", "कौन सोच सकता था कि इतना छोटा बदलाव इतना बड़ा आउटेज पैदा करेगा"),
    ],
    ("te", True): [
        ("declarative_mark", "మీకు python అనుభవం ఉంది?"),
        ("declarative_mark", "మీరు రిమోట్‌గా పని చేయడానికి సిద్ధం?"),
        ("noise", "ఉమ్ మరి మీరు మీరు మీ అనుభవం గురించి చెప్పండి"),
        ("noise", "అంటే docker మరియు kubernetes మధ్య ఏమిటి తేడా"),
        ("preamble", "ముందుకు వెళ్తూ నేను అడగాలనుకుంటున్నాను మీరు టెస్టింగ్ ఎలా చేస్తారు"),
        ("preamble", "ఇది మంచి సమాధానం అయితే దీన్ని పది రెట్లు ట్రాఫిక్‌కి ఎలా స్కేల్ చేస్తారు"),
        ("tag", "మీరు redis వాడారు కదా"),
        ("short_followup", "మరి caching విషయంలో ఏమిటి"),
        ("imperative", "sql మరియు nosql మధ్య మూడు తేడాలు చెప్పండి"),
        ("imperative", "quicksort మరియు mergesort ని memory use పరంగా పోల్చండి"),
        ("imperative", "మీ architecture గురించి ఒక చిన్న overview ఇవ్వండి"),
        ("noise", "binary search యొక్క time complexity ఎంత"),
    ],
    ("te", False): [
        ("exclamation", "ఎంత అద్భుతమైన అవకాశం ఇది"),
        ("mid_wh_statement", "ఏమి చేయాలి ఎలా చేయాలి అని మేము చర్చించాము"),
        ("account", "ఈ సిస్టమ్ ఎలా స్కేల్ అవుతుందో అని నాకే అనిపించింది అందుకే database ని shard చేయాలని నిర్ణయించాను"),
        ("announcement", "ఇప్పుడు నేను మీకు కొన్ని behavioral ప్రశ్నలు అడుగుతాను"),
        ("reported", "ప్రశ్న ఏమిటంటే thread మరియు process మధ్య తేడా ఏమిటి అని నేను సమాధానం చెప్పాను"),
        ("reported", "మేము మొదట ఆ framework ని ఎందుకు ఎంచుకున్నామో నేను ఆలోచిస్తున్నాను"),
        ("filler", "అవును నేను ఆ ప్రాజెక్ట్‌లో ప్రాథమికంగా అదే చేశాను"),
        ("filler", "ఉమ్ అవును నా వైపు నుంచి అంతే అనుకుంటున్నాను"),
        ("closing", "చాలా బాగుంది ధన్యవాదాలు తదుపరి దశల గురించి టీమ్‌కి తెలియజేస్తాను"),
        ("smalltalk", "మిమ్మల్ని కలవడం చాలా సంతోషం మీ పని గురించి చాలా విన్నాను"),
        ("closing", "ఫలితం గురించి మేము ఒక వారంలో మిమ్మల్ని సంప్రదిస్తాము"),
        ("rhetorical", "ఇంత చిన్న మార్పు ఇంత పెద్ద అంతరాయం కలిగిస్తుందని ఎవరు ఊహించారు"),
    ],
}


def main() -> None:
    rows = []
    for (lang, label), items in CHALLENGE.items():
        for index, (category, text) in enumerate(items):
            rows.append(
                {
                    "id": f"{lang}-ch-{category}-{'p' if label else 'n'}{index + 1:02d}",
                    "lang": lang,
                    "category": category,
                    "label": label,
                    "split": "challenge",
                    "text": text,
                }
            )
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate id")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} challenge examples to {OUT}")


if __name__ == "__main__":
    main()
