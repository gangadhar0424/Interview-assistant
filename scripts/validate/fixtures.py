# ruff: noqa: E501
"""Synthetic fixtures for provider validation (M-3). Everything here is invented: a fictional
candidate, fictional employers and made-up project names. No real person, resume or interview data.

    python scripts/validate/fixtures.py <out_dir>

Writes resume.docx, jd.docx and truth.json (retrieval questions with marker phrases, plus the
spoken-question reference texts used for speech-to-text accuracy).
"""

import json
import os
import sys

RESUME = [
    ("Alex Sample", "Senior Backend Engineer. Fictional profile used only for software testing."),
    (
        "Summary",
        "Backend engineer with eight years of experience building payment and logistics systems in Python and Go. Led the Zorbex Ledger rewrite and mentored five engineers.",
    ),
    (
        "Experience",
        "Zorbex Systems, Staff Engineer, 2021 to 2025. Designed the Quillon event pipeline that processes forty thousand events per second with Kafka and PostgreSQL. Reduced settlement delays by sixty percent by introducing idempotent retries.",
    ),
    (
        "Experience",
        "Brindle Freight, Backend Engineer, 2017 to 2021. Built the Marlow route optimizer in Go, cutting fuel estimation time from nine seconds to under one second using a cached geohash index.",
    ),
    (
        "Projects",
        "Tessera Cache: an open source read-through cache library with a pluggable eviction policy. Wrote the adaptive TinyLFU admission filter and benchmarked it against LRU.",
    ),
    (
        "Education",
        "Bachelor of Technology in Computer Science, Northfield Institute of Technology, graduated 2016 with distinction in distributed systems.",
    ),
    (
        "Skills",
        "Python, Go, PostgreSQL, Kafka, Redis, Kubernetes, Terraform, observability with OpenTelemetry, incident response, and technical interviewing.",
    ),
    (
        "Achievements",
        "Won the Zorbex Engineering Excellence Award in 2023 for the zero-downtime ledger migration. Spoke at the fictional PayConf 2024 about idempotency keys.",
    ),
]

JD = [
    ("Role", "Staff Backend Engineer at Halcyon Payments, a fictional payments company."),
    (
        "Responsibilities",
        "Own the reliability of the Halcyon settlement engine. Design event-driven services, improve observability, mentor engineers and lead incident reviews.",
    ),
    (
        "Requirements",
        "Seven or more years of backend experience, strong PostgreSQL and Kafka skills, experience with idempotent payment flows, and a track record of reducing production incidents.",
    ),
    (
        "Interview process",
        "Recruiter call, system design round on a ledger service, coding round in Python or Go, and a final behavioral round with the engineering director.",
    ),
]

# question -> a marker phrase that must appear in a retrieved passage for retrieval to count as correct.
RETRIEVAL = [
    ("Tell me about the Quillon event pipeline.", ["Quillon"], "resume"),
    ("How did you reduce settlement delays?", ["idempotent retries"], "resume"),
    ("What did you build at Brindle Freight?", ["Marlow route optimizer"], "resume"),
    ("How fast was the route optimizer after your changes?", ["under one second"], "resume"),
    ("Do you have open source experience?", ["Tessera Cache"], "resume"),
    ("What eviction filter did you write?", ["TinyLFU"], "resume"),
    ("Where did you study?", ["Northfield Institute"], "resume"),
    ("Which technologies do you know?", ["Kubernetes", "Terraform"], "resume"),
    ("Did you win any awards?", ["Engineering Excellence Award"], "resume"),
    ("What did you speak about at a conference?", ["PayConf"], "resume"),
    ("How many engineers have you mentored?", ["mentored five engineers"], "resume"),
    ("What was your migration achievement?", ["zero-downtime ledger migration"], "resume"),
    ("What are the responsibilities of the role?", ["settlement engine"], "document"),
    ("What does the role require?", ["idempotent payment flows"], "document"),
    ("What is the interview process?", ["system design round"], "document"),
    ("Who is the final interviewer?", ["engineering director"], "document"),
]

# Spoken questions for STT accuracy: reference text only; the WAVs are generated from these.
SPOKEN = [
    "Can you explain the difference between a process and a thread?",
    "Tell me about a time you resolved a conflict on your team.",
    "How would you design a rate limiter for a public API?",
    "What is the time complexity of binary search?",
    "Why do you want to work at Halcyon Payments?",
    "Describe how idempotency keys prevent duplicate charges.",
    "What happens when you type a web address into a browser?",
    "How do you handle a production incident at night?",
    "Explain the CAP theorem in simple words.",
    "What are your greatest strengths as a backend engineer?",
]

# Questions used for LLM timing: some grounded in the resume, some general.
ASK = [
    "Tell me about yourself.",
    "What did you build at Zorbex Systems?",
    "How did you reduce settlement delays?",
    "Explain the difference between a process and a thread.",
    "How would you design a rate limiter?",
    "Tell me about a time you led an incident review.",
    "What is your experience with Kafka?",
    "Why do you want to join Halcyon Payments?",
    "What is idempotency and why does it matter in payments?",
    "Describe your open source work.",
]


def make_docx(path: str, rows: list[tuple[str, str]]) -> None:
    import docx

    document = docx.Document()
    for index, (heading, body) in enumerate(rows):
        document.add_heading(heading, level=0 if index == 0 else 1)
        document.add_paragraph(body)
    document.save(path)


def main() -> None:
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    make_docx(os.path.join(out, "resume.docx"), RESUME)
    make_docx(os.path.join(out, "jd.docx"), JD)
    questions = [
        {
            "id": f"s{index + 1:02d}",
            "group": "resume" if source == "resume" else "document",
            "expect": source,
            "question": question,
            "contains": markers,
        }
        for index, (question, markers, source) in enumerate(RETRIEVAL)
    ]
    truth = {"questions": questions, "spoken": SPOKEN, "ask": ASK}
    with open(os.path.join(out, "truth.json"), "w", encoding="utf-8") as handle:
        json.dump(truth, handle, indent=2)
    print(f"wrote {len(questions)} retrieval questions, {len(SPOKEN)} spoken, {len(ASK)} ask")


if __name__ == "__main__":
    main()
