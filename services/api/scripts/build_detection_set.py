# ruff: noqa: E501, RUF001
"""Build the question-detection evaluation set (M-7): tests/eval/detection_set.jsonl.

    cd services/api && python scripts/build_detection_set.py

Every example is INVENTED for this evaluation: no real interview, transcript, person, employer or
recording is used. They were written by the assistant (an AI) in the style a speech-to-text provider
returns text: mostly lower case for English, the question mark usually missing, no commas. Hindi and
Telugu are in their own scripts, with English technical terms left in Latin letters (the code-switching
interview speakers actually produce), plus a small romanised (Hinglish / Tenglish) group per language.
NO NATIVE-SPEAKER REVIEW has been done: that is a documented limitation.

Labels are explicit and follow the policy in docs/QUESTION_DETECTION_EVALUATION.md section 2:
  positive  = a question, or a request or task, that the candidate is expected to answer or do
  negative  = anything else, INCLUDING an utterance cut off before it says what is being asked
              (answering a fragment would be wrong), procedural requests ("turn on your camera"),
              small talk, statements, and sentences that merely contain a question word.

Splits: within every (language, category) the examples alternate dev, heldout, dev, ... The detector
may be tuned against `dev` only; `heldout` is reported but never used to tune.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "eval" / "detection_set.jsonl"

POS = "positive"
NEG = "negative"

# (language, category, label) -> utterances
DATA: dict[tuple[str, str, str], list[str]] = {
    # ------------------------------------------------------------------ English
    ("en", "direct", POS): [
        "what is your expected salary range",
        "when can you start if we make you an offer",
        "why should we hire you for this position",
        "how many years of experience do you have with java",
        "what is your notice period",
        "are you comfortable relocating to another city",
        "do you have any questions for me",
        "how did you hear about this role",
    ],
    ("en", "technical", POS): [
        "what is the difference between a mutex and a semaphore",
        "how does a hash map handle collisions",
        "explain how tcp differs from udp",
        "what happens when you type a url into a browser",
        "can you explain how database indexing works",
        "which sorting algorithm would you use for nearly sorted data and why",
        "how would you detect a deadlock in a running service",
        "what is the time complexity of inserting into a binary search tree",
    ],
    ("en", "behavioral", POS): [
        "tell me about a time you had a conflict with a coworker",
        "describe a situation where you missed a deadline",
        "give me an example of when you showed leadership",
        "how do you handle criticism from your manager",
        "tell me about the most challenging project you have worked on",
        "walk me through a time you had to learn something quickly",
        "what did you do when a teammate was not pulling their weight",
        "have you ever disagreed with a technical decision and what did you do",
    ],
    ("en", "followup", POS): [
        "and what was the result of that",
        "why did you choose that approach over the alternatives",
        "how did the team react to that decision",
        "what would you do differently next time",
        "can you give me a more specific example",
        "so how long did that migration take",
    ],
    ("en", "indirect", POS): [
        "i was wondering how you would approach scaling this service",
        "i'd be curious to hear why you moved from backend to data engineering",
        "i'd like to understand how you prioritize your work",
        "let's talk about your experience with distributed systems",
        "i am interested in knowing how you mentor junior engineers",
        "maybe you could tell me a bit about your last project",
    ],
    ("en", "multiple", POS): [
        "where did you study and what was your major there",
        "what is your biggest strength and what is your biggest weakness",
        "how do you test your code and how do you handle flaky tests",
        "why python and how does it compare with go for this kind of work",
    ],
    ("en", "task", POS): [
        "write a function that reverses a linked list",
        "design a url shortener like bit ly",
        "implement an lru cache with constant time operations",
        "given an array of integers find two numbers that add up to a target",
    ],
    ("en", "statement", NEG): [
        "my name is priya and i manage the data platform team",
        "we are a series b startup with about three hundred employees",
        "i have been working as a backend engineer for six years",
        "the team uses python and postgres for most services",
        "i led the migration of our billing system to kubernetes",
        "our interview process has three rounds including this one",
        "i really enjoyed working on the search ranking project",
        "the office is located in the central business district",
    ],
    ("en", "keyword", NEG): [
        "i know what you mean about the deadline",
        "that is why i decided to move into management",
        "what i did was rewrite the entire caching layer",
        "how i handled it was by talking to the team first",
        "who i am today is a result of those years",
        "when i joined the company we had no tests at all",
        "which is exactly what we needed at the time",
        "she asked me why i left and i said it was the commute",
    ],
    ("en", "instruction", NEG): [
        "please share your screen so we can see the code",
        "go ahead and open the shared document",
        "can you turn on your camera please",
        "could you mute yourself while i set this up",
        "let me send you the link in the chat",
        "hold on one second while i pull up the problem",
    ],
    ("en", "incomplete", NEG): [
        "so what is the difference between",
        "tell me about a time when you",
        "how would you go about",
        "can you walk me through the",
        "what do you think is the most important thing when",
        "why did you decide to leave your",
    ],
    ("en", "chatter", NEG): [
        "thank you so much for joining us today",
        "it was great talking to you",
        "okay that makes sense",
        "yeah i can hear you fine",
        "sorry i did not catch that",
        "good morning how are you doing today",
        "let me know when you are ready to start",
        "that was a really good answer",
    ],
    # ------------------------------------------------------------------ Hindi (Devanagari, code-switched)
    ("hi", "direct", POS): [
        "आपका वर्तमान वेतन कितना है",
        "आप हमारी कंपनी में क्यों काम करना चाहते हैं",
        "आपके पास कितने साल का अनुभव है",
        "आप कब से नौकरी बदलने के बारे में सोच रहे हैं",
        "आप कहाँ से पढ़ाई कर चुके हैं",
        "क्या आप दूसरे शहर में जाने के लिए तैयार हैं",
        "आपकी नोटिस पीरियड कितनी है",
        "क्या आपके पास हमसे कोई सवाल है",
    ],
    ("hi", "technical", POS): [
        "mutex और semaphore में क्या अंतर है",
        "hash map में collision को कैसे संभाला जाता है",
        "आप database indexing कैसे काम करता है यह समझा सकते हैं",
        "browser में url डालने पर क्या होता है",
        "एक binary search tree में insert करने की time complexity क्या है",
        "आप deadlock को कैसे detect करेंगे",
        "tcp और udp में क्या फर्क है",
        "python में decorators कैसे काम करते हैं",
    ],
    ("hi", "behavioral", POS): [
        "अपने किसी सहकर्मी के साथ हुए विवाद के बारे में बताइए",
        "ऐसी स्थिति का वर्णन कीजिए जब आप समय सीमा पूरी नहीं कर पाए",
        "नेतृत्व का कोई उदाहरण दीजिए",
        "आप अपने मैनेजर की आलोचना को कैसे संभालते हैं",
        "आपका अब तक का सबसे चुनौतीपूर्ण प्रोजेक्ट कौन सा रहा",
        "कोई ऐसा समय बताइए जब आपको कुछ जल्दी सीखना पड़ा",
        "जब आपकी टीम का कोई सदस्य काम नहीं कर रहा था तब आपने क्या किया",
        "क्या आपने कभी किसी तकनीकी फैसले से असहमति जताई और तब आपने क्या किया",
    ],
    ("hi", "followup", POS): [
        "और उसका नतीजा क्या रहा",
        "आपने दूसरे तरीकों की जगह यही तरीका क्यों चुना",
        "टीम ने इस फैसले पर कैसे प्रतिक्रिया दी",
        "अगली बार आप क्या अलग करेंगे",
        "क्या आप कोई और ठोस उदाहरण दे सकते हैं",
        "तो वह migration कितने समय में पूरा हुआ",
    ],
    ("hi", "indirect", POS): [
        "मैं जानना चाहता हूँ कि आप इस सर्विस को स्केल कैसे करेंगे",
        "मुझे यह सुनने में दिलचस्पी है कि आपने backend से data engineering में बदलाव क्यों किया",
        "आप अपने बारे में कुछ बताइए",
        "ज़रा अपने पिछले प्रोजेक्ट के बारे में बताएँ",
        "चलिए distributed systems के आपके अनुभव पर बात करते हैं",
        "मैं समझना चाहती हूँ कि आप अपना काम कैसे प्राथमिकता देते हैं",
    ],
    ("hi", "multiple", POS): [
        "आपने कहाँ पढ़ाई की और आपका मुख्य विषय क्या था",
        "आपकी सबसे बड़ी ताकत क्या है और सबसे बड़ी कमज़ोरी क्या है",
        "आप अपना कोड कैसे टेस्ट करते हैं और flaky tests को कैसे संभालते हैं",
        "python क्यों और go के मुकाबले यह कैसा है",
    ],
    ("hi", "task", POS): [
        "एक function लिखिए जो linked list को उलट दे",
        "bit ly जैसा url shortener डिज़ाइन कीजिए",
        "constant time operations वाला lru cache implement कीजिए",
        "integers के array में दो ऐसी संख्याएँ खोजिए जिनका योग target के बराबर हो",
    ],
    ("hi", "statement", NEG): [
        "मेरा नाम प्रिया है और मैं डेटा प्लेटफ़ॉर्म टीम संभालती हूँ",
        "हमारी कंपनी एक स्टार्टअप है जिसमें लगभग तीन सौ लोग काम करते हैं",
        "मैं पिछले छह साल से बैकएंड इंजीनियर के रूप में काम कर रहा हूँ",
        "हमारी टीम ज़्यादातर सर्विसेज़ के लिए python और postgres इस्तेमाल करती है",
        "मैंने अपने billing system को kubernetes पर migrate करने का नेतृत्व किया",
        "हमारी इंटरव्यू प्रक्रिया में यह मिलाकर तीन राउंड हैं",
        "मुझे search ranking प्रोजेक्ट पर काम करना बहुत अच्छा लगा",
        "हमारा दफ़्तर शहर के बीच वाले इलाके में है",
    ],
    ("hi", "keyword", NEG): [
        "मुझे पता है कि आप डेडलाइन के बारे में क्या कह रहे हैं",
        "कैसे भी करके मैंने वह काम समय पर पूरा किया",
        "मैंने जो किया वह पूरी caching layer को दोबारा लिखना था",
        "जब मैं कंपनी में आया तब कोई टेस्ट नहीं थे",
        "मैं आपको बाद में बताऊँगा कि यह कैसे काम करता है",
        "क्या बात है आपने तो कमाल कर दिया",
        "कौन क्या कर रहा था यह मुझे उस समय नहीं पता था",
        "उसने पूछा कि मैंने नौकरी क्यों छोड़ी और मैंने कहा कि सफ़र बहुत लंबा था",
    ],
    ("hi", "instruction", NEG): [
        "कृपया अपनी स्क्रीन शेयर कीजिए ताकि हम कोड देख सकें",
        "आगे बढ़िए और शेयर किया हुआ डॉक्यूमेंट खोलिए",
        "क्या आप अपना कैमरा चालू कर सकते हैं",
        "क्या आप मेरी बात होने तक अपना माइक म्यूट कर सकते हैं",
        "मैं चैट में आपको लिंक भेज देता हूँ",
        "एक सेकंड रुकिए मैं समस्या खोलता हूँ",
    ],
    ("hi", "incomplete", NEG): [
        "तो इन दोनों के बीच का",
        "मुझे ऐसे समय के बारे में बताइए जब आप",
        "आप इसे कैसे",
        "क्या आप मुझे समझा सकते हैं कि",
        "आपके हिसाब से सबसे ज़रूरी बात क्या होती है जब",
        "आपने अपनी पिछली नौकरी क्यों छोड़ी क्योंकि",
    ],
    ("hi", "chatter", NEG): [
        "आज हमारे साथ जुड़ने के लिए बहुत धन्यवाद",
        "आपसे बात करके अच्छा लगा",
        "ठीक है यह समझ में आ गया",
        "हाँ मुझे आपकी आवाज़ साफ़ सुनाई दे रही है",
        "माफ़ कीजिए मैंने ठीक से सुना नहीं",
        "सुप्रभात आज आप कैसे हैं",
        "जब आप तैयार हों तो मुझे बता दीजिए",
        "यह बहुत अच्छा जवाब था",
    ],
    ("hi", "roman", POS): [
        "aap apne baare mein bataiye",
        "kya aapko python mein kaam karne ka anubhav hai",
        "aap is project mein sabse badi challenge kya thi",
        "microservices aur monolith mein kya difference hai",
        "aap deadline ko kaise manage karte hain",
    ],
    ("hi", "roman", NEG): [
        "mera naam rahul hai aur main backend team mein hoon",
        "dhanyavaad aaj aane ke liye",
        "screen share kijiye please",
        "main aapko baad mein bataunga ki yeh kaise kaam karta hai",
        "theek hai samajh gaya",
    ],
    # ------------------------------------------------------------------ Telugu (Telugu script, code-switched)
    ("te", "direct", POS): [
        "మీ ప్రస్తుత జీతం ఎంత",
        "మీరు మా కంపెనీలో ఎందుకు పని చేయాలనుకుంటున్నారు",
        "మీకు ఎన్ని సంవత్సరాల అనుభవం ఉంది",
        "మీరు ఉద్యోగం మారాలని ఎప్పటి నుంచి ఆలోచిస్తున్నారు",
        "మీరు ఎక్కడ చదువుకున్నారు",
        "మీరు వేరే నగరానికి వెళ్లడానికి సిద్ధంగా ఉన్నారా",
        "మీ నోటీసు పీరియడ్ ఎంత",
        "మీకు మమ్మల్ని అడగడానికి ఏమైనా ప్రశ్నలు ఉన్నాయా",
    ],
    ("te", "technical", POS): [
        "mutex మరియు semaphore మధ్య తేడా ఏమిటి",
        "hash map లో collisions ని ఎలా హ్యాండిల్ చేస్తారు",
        "database indexing ఎలా పని చేస్తుందో వివరించగలరా",
        "browser లో url టైప్ చేస్తే ఏమి జరుగుతుంది",
        "binary search tree లో insert చేసే time complexity ఎంత",
        "deadlock ని మీరు ఎలా గుర్తిస్తారు",
        "tcp మరియు udp మధ్య తేడా ఏమిటి",
        "python లో decorators ఎలా పని చేస్తాయి",
    ],
    ("te", "behavioral", POS): [
        "మీ సహోద్యోగితో వచ్చిన విభేదం గురించి చెప్పండి",
        "మీరు గడువును అందుకోలేకపోయిన సందర్భాన్ని వివరించండి",
        "మీరు నాయకత్వం చూపిన ఒక ఉదాహరణ చెప్పండి",
        "మీ మేనేజర్ చేసిన విమర్శను మీరు ఎలా తీసుకుంటారు",
        "మీరు ఇప్పటివరకు చేసిన అత్యంత కష్టమైన ప్రాజెక్ట్ ఏది",
        "మీరు ఏదైనా త్వరగా నేర్చుకోవాల్సి వచ్చిన సందర్భం గురించి చెప్పండి",
        "మీ టీమ్ సభ్యుడు పని చేయనప్పుడు మీరు ఏమి చేశారు",
        "మీరు ఎప్పుడైనా సాంకేతిక నిర్ణయంతో విభేదించారా అప్పుడు ఏమి చేశారు",
    ],
    ("te", "followup", POS): [
        "మరి దాని ఫలితం ఏమిటి",
        "మీరు ఇతర మార్గాలకంటే ఇదే పద్ధతిని ఎందుకు ఎంచుకున్నారు",
        "ఈ నిర్ణయంపై టీమ్ ఎలా స్పందించింది",
        "వచ్చేసారి మీరు ఏమి వేరేగా చేస్తారు",
        "మీరు ఇంకొక స్పష్టమైన ఉదాహరణ ఇవ్వగలరా",
        "అయితే ఆ migration పూర్తవడానికి ఎంత సమయం పట్టింది",
    ],
    ("te", "indirect", POS): [
        "ఈ సర్వీస్‌ను మీరు ఎలా స్కేల్ చేస్తారో తెలుసుకోవాలని ఉంది",
        "మీరు backend నుంచి data engineering కి ఎందుకు మారారో వినాలని ఉంది",
        "మీ గురించి కొంచెం చెప్పండి",
        "మీ గత ప్రాజెక్ట్ గురించి కాస్త చెప్పగలరా",
        "ఇక distributed systems లో మీ అనుభవం గురించి మాట్లాడుకుందాం",
        "మీరు మీ పనులకు ఎలా ప్రాధాన్యత ఇస్తారో నాకు అర్థం చేసుకోవాలని ఉంది",
    ],
    ("te", "multiple", POS): [
        "మీరు ఎక్కడ చదువుకున్నారు మీ మెయిన్ సబ్జెక్ట్ ఏమిటి",
        "మీ బలం ఏమిటి మీ బలహీనత ఏమిటి",
        "మీరు మీ కోడ్‌ను ఎలా టెస్ట్ చేస్తారు flaky tests ని ఎలా హ్యాండిల్ చేస్తారు",
        "python ఎందుకు దీన్ని go తో పోలిస్తే ఎలా ఉంటుంది",
    ],
    ("te", "task", POS): [
        "linked list ని రివర్స్ చేసే ఒక function రాయండి",
        "bit ly లాంటి url shortener ని డిజైన్ చేయండి",
        "constant time operations ఉన్న lru cache ని implement చేయండి",
        "integers array లో కలిపితే target వచ్చే రెండు సంఖ్యలను కనుగొనండి",
    ],
    ("te", "statement", NEG): [
        "నా పేరు ప్రియ నేను డేటా ప్లాట్‌ఫామ్ టీమ్‌ను చూసుకుంటాను",
        "మాది సుమారు మూడు వందల మంది ఉన్న స్టార్టప్ కంపెనీ",
        "నేను గత ఆరు సంవత్సరాలుగా బ్యాకెండ్ ఇంజనీర్‌గా పని చేస్తున్నాను",
        "మా టీమ్ ఎక్కువ సర్వీసులకు python మరియు postgres వాడుతుంది",
        "నేను మా billing system ని kubernetes కి మార్చే పనికి నాయకత్వం వహించాను",
        "మా ఇంటర్వ్యూ ప్రక్రియలో ఇది కలిపి మూడు రౌండ్లు ఉన్నాయి",
        "search ranking ప్రాజెక్ట్‌లో పని చేయడం నాకు చాలా నచ్చింది",
        "మా ఆఫీస్ నగరం మధ్యలో ఉంది",
    ],
    ("te", "keyword", NEG): [
        "మీరు డెడ్‌లైన్ గురించి ఏమి చెబుతున్నారో నాకు తెలుసు",
        "ఎలాగైనా నేను ఆ పనిని సమయానికి పూర్తి చేశాను",
        "నేను చేసింది మొత్తం caching layer ని మళ్లీ రాయడమే",
        "నేను కంపెనీలో చేరినప్పుడు అసలు టెస్టులే లేవు",
        "ఇది ఎలా పని చేస్తుందో నేను మీకు తర్వాత చెబుతాను",
        "ఎవరు ఏమి చేస్తున్నారో నాకు అప్పట్లో తెలియదు",
        "ఎందుకు వదిలేశావని అతను అడిగాడు నేను ప్రయాణం ఎక్కువని చెప్పాను",
        "ఏమిటో ఈ రోజు చాలా బిజీగా ఉంది",
    ],
    ("te", "instruction", NEG): [
        "దయచేసి మీ స్క్రీన్ షేర్ చేయండి మేము కోడ్ చూస్తాం",
        "ముందుకు వెళ్లి షేర్ చేసిన డాక్యుమెంట్ ఓపెన్ చేయండి",
        "మీరు మీ కెమెరా ఆన్ చేయగలరా",
        "నేను ఇది సెట్ చేసే వరకు మీరు మైక్ మ్యూట్ చేయగలరా",
        "నేను చాట్‌లో మీకు లింక్ పంపిస్తాను",
        "ఒక్క నిమిషం ఆగండి నేను సమస్యను తెరుస్తాను",
    ],
    ("te", "incomplete", NEG): [
        "అయితే ఈ రెండింటి మధ్య",
        "మీరు ఎదుర్కొన్న ఒక సందర్భం గురించి చెప్పండి అప్పుడు మీరు",
        "మీరు దీన్ని ఎలా",
        "మీరు నాకు వివరించగలరా ఏంటంటే",
        "మీ దృష్టిలో అత్యంత ముఖ్యమైన విషయం ఏమిటంటే ఎప్పుడు",
        "మీరు మీ గత ఉద్యోగాన్ని ఎందుకు వదిలారు ఎందుకంటే",
    ],
    ("te", "chatter", NEG): [
        "ఈ రోజు మాతో చేరినందుకు చాలా ధన్యవాదాలు",
        "మీతో మాట్లాడటం సంతోషంగా ఉంది",
        "సరే అది అర్థమైంది",
        "అవును మీ గొంతు నాకు స్పష్టంగా వినిపిస్తోంది",
        "క్షమించండి నాకు సరిగ్గా వినిపించలేదు",
        "శుభోదయం ఈ రోజు మీరు ఎలా ఉన్నారు",
        "మీరు సిద్ధంగా ఉన్నప్పుడు నాకు చెప్పండి",
        "అది చాలా మంచి సమాధానం",
    ],
    ("te", "roman", POS): [
        "mee gurinchi cheppandi",
        "meeru python lo pani chesara",
        "ee project lo mee biggest challenge enti",
        "microservices ki monolith ki difference enti",
        "meeru deadlines ela manage chestaru",
    ],
    ("te", "roman", NEG): [
        "naa peru rahul nenu backend team lo unnanu",
        "ee roju vachinanduku dhanyavadalu",
        "dayachesi screen share cheyandi",
        "idi ela pani chestundo nenu tarvata chebutanu",
        "sare ardhamaindi",
    ],
}


def main() -> None:
    rows = []
    for (lang, category, label), texts in DATA.items():
        for index, text in enumerate(texts):
            rows.append(
                {
                    "id": f"{lang}-{category}-{'p' if label == POS else 'n'}{index + 1:02d}",
                    "lang": lang,
                    "category": category,
                    "label": label == POS,
                    "split": "dev" if index % 2 == 0 else "heldout",
                    "text": text,
                }
            )
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate id")
    texts_seen = [r["text"] for r in rows]
    if len(texts_seen) != len(set(texts_seen)):
        raise SystemExit("duplicate text")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    by_lang: dict[str, int] = {}
    for row in rows:
        by_lang[row["lang"]] = by_lang.get(row["lang"], 0) + 1
    print(f"wrote {len(rows)} examples to {OUT}: {by_lang}")


if __name__ == "__main__":
    main()
