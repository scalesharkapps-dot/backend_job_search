"""Skill taxonomy, normalisation and extraction (shared by CV parsing and matching).

Design rules:
* Every skill has one canonical name plus aliases (case-insensitive).
* *Ambiguous* aliases (e.g. ``Go``, ``R``, ``C``, ``py``, ``ML``, ``AI``) are only
  accepted from explicit skill lists (a CV "Skills" section, a job's structured
  skills, user input) — never from free text, where they are ordinary words.
* ``implies`` records strict "knowing X means knowing Y" relations
  (FastAPI → Python). Implied skills are reported as *inferred*, never as stated.
* No fuzzy/similar-word matching: unrelated skills are never equivalent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class SkillDef:
    name: str
    category: str
    aliases: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()
    implies: tuple[str, ...] = ()
    # Common English words: matched in free text only with this exact casing.
    case_sensitive: tuple[str, ...] = ()


def _s(
    name: str, category: str, aliases: str = "", ambiguous: str = "", implies: str = "", cs: str = ""
) -> SkillDef:
    split = lambda v: tuple(a.strip() for a in v.split("|") if a.strip())  # noqa: E731
    return SkillDef(name, category, split(aliases), split(ambiguous), split(implies), split(cs))


LANG, FW, DB, CLOUD, DEVOPS, DATA, TOOL, MOBILE, BIZ, TEST, SEC = (
    "programming_language", "framework", "database", "cloud", "devops", "data_ai",
    "tool", "mobile", "business", "testing", "security",
)

SKILLS: tuple[SkillDef, ...] = (
    # Languages
    _s("Python", LANG, "python3|python 3", "py"),
    _s("JavaScript", LANG, "javascript|ecmascript|es6", "js"),
    _s("TypeScript", LANG, "typescript", "ts"),
    _s("Java", LANG, "java|java se|java ee"),
    _s("C++", LANG, "c++|cpp"),
    _s("C#", LANG, "c#|csharp|c sharp"),
    _s("C", LANG, "", "c"),
    _s("Go", LANG, "golang", "go"),
    _s("Rust", LANG, "rust lang", "rust", cs="Rust"),
    _s("Ruby", LANG, "ruby"),
    _s("PHP", LANG, "php"),
    _s("Kotlin", LANG, "kotlin"),
    _s("Swift", LANG, "swiftui|swift ui", "swift", cs="Swift"),
    _s("Dart", LANG, "dart"),
    _s("Scala", LANG, "scala"),
    _s("R", LANG, "r programming|rstudio", "r"),
    _s("MATLAB", LANG, "matlab"),
    _s("SQL", LANG, "sql|t-sql|pl/sql|plsql"),
    _s("Bash", LANG, "bash|shell scripting|shell script"),
    _s("PowerShell", LANG, "powershell"),
    _s("HTML", LANG, "html|html5"),
    _s("CSS", LANG, "css|css3"),
    _s("Solidity", LANG, "solidity"),
    # Frameworks / libraries
    _s("FastAPI", FW, "fastapi|fast api", implies="Python"),
    _s("Django", FW, "django|django rest framework|drf", implies="Python"),
    _s("Flask", FW, "flask", implies="Python"),
    _s("React", FW, "reactjs|react.js", "react", implies="JavaScript", cs="React"),
    _s("Next.js", FW, "next.js|nextjs", implies="React"),
    _s("Angular", FW, "angular|angularjs"),
    _s("Vue.js", FW, "vue|vue.js|vuejs"),
    _s("Node.js", FW, "node.js|nodejs|node js", implies="JavaScript"),
    _s("Express.js", FW, "express.js|expressjs", implies="Node.js"),
    _s("NestJS", FW, "nestjs|nest.js", implies="Node.js"),
    _s("Spring Boot", FW, "spring boot|springboot|spring framework", implies="Java"),
    _s(".NET", FW, ".net|dotnet|asp.net|.net core", implies="C#"),
    _s("Laravel", FW, "laravel", implies="PHP"),
    _s("Ruby on Rails", FW, "ruby on rails", "rails", implies="Ruby", cs="Rails"),
    _s("Flutter", MOBILE, "flutter", implies="Dart"),
    _s("React Native", MOBILE, "react native", implies="React"),
    _s("Android", MOBILE, "android|android sdk"),
    _s("iOS", MOBILE, "ios development|ios sdk", "ios"),
    _s("jQuery", FW, "jquery"),
    _s("Tailwind CSS", FW, "tailwind|tailwindcss", implies="CSS"),
    _s("Bootstrap", FW, "bootstrap"),
    _s("GraphQL", FW, "graphql"),
    _s("REST APIs", FW, "rest api|rest apis|restful|restful api|restful apis", "rest", cs="REST"),
    _s("gRPC", FW, "grpc"),
    _s("SQLAlchemy", FW, "sqlalchemy", implies="Python"),
    _s("Pydantic", FW, "pydantic", implies="Python"),
    _s("Celery", FW, "celery", implies="Python"),
    # Databases
    _s("PostgreSQL", DB, "postgresql|postgres|psql", implies="SQL"),
    _s("MySQL", DB, "mysql", implies="SQL"),
    _s("SQLite", DB, "sqlite", implies="SQL"),
    _s("Microsoft SQL Server", DB, "sql server|mssql|ms sql", implies="SQL"),
    _s("Oracle Database", DB, "oracle database|oracle db", implies="SQL"),
    _s("MongoDB", DB, "mongodb|mongo db", "mongo"),
    _s("Redis", DB, "redis"),
    _s("Elasticsearch", DB, "elasticsearch|elastic search"),
    _s("Cassandra", DB, "cassandra"),
    _s("DynamoDB", DB, "dynamodb"),
    _s("Firebase", CLOUD, "firebase|firestore"),
    _s("Supabase", CLOUD, "supabase"),
    # Cloud / DevOps
    _s("AWS", CLOUD, "aws|amazon web services|ec2|s3|aws lambda"),
    _s("Azure", CLOUD, "azure|microsoft azure"),
    _s("Google Cloud", CLOUD, "google cloud|gcp|google cloud platform"),
    _s("Docker", DEVOPS, "docker|dockerfile|containerization"),
    _s("Kubernetes", DEVOPS, "kubernetes|k8s"),
    _s("Terraform", DEVOPS, "terraform"),
    _s("Ansible", DEVOPS, "ansible"),
    _s("Jenkins", DEVOPS, "jenkins"),
    _s("GitHub Actions", DEVOPS, "github actions"),
    _s("GitLab CI", DEVOPS, "gitlab ci|gitlab ci/cd"),
    _s("CI/CD", DEVOPS, "ci/cd|continuous integration|continuous deployment|ci cd"),
    _s("Linux", DEVOPS, "linux|ubuntu|unix"),
    _s("Nginx", DEVOPS, "nginx"),
    _s("Git", TOOL, "github|gitlab|bitbucket", "git", cs="Git|GIT"),
    _s("Microservices", DEVOPS, "microservices|microservice architecture"),
    _s("Kafka", DEVOPS, "kafka|apache kafka"),
    _s("RabbitMQ", DEVOPS, "rabbitmq"),
    # Data / AI
    _s("Machine Learning", DATA, "machine learning", "ml"),
    _s("Deep Learning", DATA, "deep learning", "dl", implies="Machine Learning"),
    _s("Artificial Intelligence", DATA, "artificial intelligence", "ai"),
    _s("Natural Language Processing", DATA, "natural language processing", "nlp", implies="Machine Learning"),
    _s("Computer Vision", DATA, "computer vision|opencv", "cv", implies="Machine Learning"),
    _s("Large Language Models", DATA, "large language models|large language model|llms|llm", implies="Artificial Intelligence"),
    _s("Generative AI", DATA, "generative ai|genai|gen ai", implies="Artificial Intelligence"),
    _s("LangChain", DATA, "langchain", implies="Large Language Models"),
    _s("RAG", DATA, "retrieval augmented generation|retrieval-augmented generation", "rag", implies="Large Language Models"),
    _s("TensorFlow", DATA, "tensorflow", implies="Deep Learning"),
    _s("PyTorch", DATA, "pytorch", implies="Deep Learning"),
    _s("Keras", DATA, "keras", implies="Deep Learning"),
    _s("scikit-learn", DATA, "scikit-learn|sklearn|scikit learn", implies="Machine Learning"),
    _s("Hugging Face", DATA, "hugging face|huggingface|transformers library", implies="Machine Learning"),
    _s("Pandas", DATA, "pandas", implies="Python"),
    _s("NumPy", DATA, "numpy", implies="Python"),
    _s("Data Analysis", DATA, "data analysis|data analytics|analytics"),
    _s("Data Visualization", DATA, "data visualization|data visualisation"),
    _s("Statistics", DATA, "statistics|statistical analysis"),
    _s("Power BI", DATA, "power bi|powerbi"),
    _s("Tableau", DATA, "tableau"),
    _s("Excel", TOOL, "ms excel|microsoft excel|advanced excel", "excel", cs="Excel|MS Excel"),
    _s("Apache Spark", DATA, "apache spark|pyspark", "spark", cs="Spark"),
    _s("Hadoop", DATA, "hadoop"),
    _s("Airflow", DATA, "airflow|apache airflow"),
    _s("ETL", DATA, "etl|data pipelines|data pipeline"),
    _s("Data Engineering", DATA, "data engineering"),
    _s("Snowflake", DATA, "snowflake"),
    _s("BigQuery", DATA, "bigquery"),
    # Testing / security
    _s("Pytest", TEST, "pytest", implies="Python"),
    _s("Unit Testing", TEST, "unit testing|unit tests"),
    _s("Selenium", TEST, "selenium"),
    _s("Jest", TEST, "", "jest", cs="Jest"),
    _s("Cypress", TEST, "cypress"),
    _s("Cybersecurity", SEC, "cybersecurity|cyber security|information security"),
    _s("Penetration Testing", SEC, "penetration testing|pentesting"),
    _s("OAuth", SEC, "oauth|oauth2|oauth 2.0"),
    _s("JWT", SEC, "jwt|json web token|json web tokens"),
    # Tools / design
    _s("Jira", TOOL, "jira"),
    _s("Figma", TOOL, "figma"),
    _s("Postman", TOOL, "postman"),
    _s("Photoshop", TOOL, "photoshop|adobe photoshop"),
    _s("Illustrator", TOOL, "adobe illustrator", "illustrator"),
    _s("UI/UX Design", TOOL, "ui/ux|ui ux|ux design|ui design|user experience"),
    _s("WordPress", TOOL, "wordpress"),
    _s("Shopify", TOOL, "shopify"),
    _s("SAP", TOOL, "sap erp|sap", ),
    _s("Salesforce", TOOL, "salesforce"),
    _s("QuickBooks", TOOL, "quickbooks"),
    # Methodology / business
    _s("Agile", BIZ, "agile methodology|agile development", "agile", cs="Agile"),
    _s("Scrum", BIZ, "scrum", implies="Agile"),
    _s("Project Management", BIZ, "project management"),
    _s("Digital Marketing", BIZ, "digital marketing"),
    _s("SEO", BIZ, "seo|search engine optimization"),
    _s("Social Media Marketing", BIZ, "social media marketing|smm"),
    _s("Content Writing", BIZ, "content writing|copywriting"),
    _s("Accounting", BIZ, "accounting|bookkeeping"),
    _s("Financial Analysis", BIZ, "financial analysis|financial modeling|financial modelling"),
    _s("Sales", BIZ, "business development|sales executive|b2b sales", "sales", cs="Sales"),
    _s("Customer Service", BIZ, "customer service|customer support"),
    _s("Communication", BIZ, "communication skills|verbal communication|written communication"),
    _s("Leadership", BIZ, "leadership|team leadership"),
    _s("Problem Solving", BIZ, "problem solving|problem-solving"),
    _s("Teamwork", BIZ, "teamwork|team player"),
    _s("Human Resources", BIZ, "human resources|recruitment|talent acquisition"),
)


@dataclass
class _Index:
    by_alias: dict[str, str] = field(default_factory=dict)  # unambiguous + ambiguous (explicit lists)
    free_text_aliases: dict[str, str] = field(default_factory=dict)  # unambiguous only
    defs: dict[str, SkillDef] = field(default_factory=dict)
    pattern: re.Pattern[str] | None = None
    cs_pattern: re.Pattern[str] | None = None
    cs_aliases: dict[str, str] = field(default_factory=dict)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@lru_cache(maxsize=1)
def _index() -> _Index:
    idx = _Index()
    for skill in SKILLS:
        idx.defs[skill.name] = skill
        names = {skill.name.lower(), *[a.lower() for a in skill.aliases]}
        for alias in names:
            if alias not in {a.lower() for a in skill.ambiguous}:
                idx.free_text_aliases.setdefault(_norm(alias), skill.name)
            idx.by_alias.setdefault(_norm(alias), skill.name)
        for alias in skill.ambiguous:
            idx.by_alias.setdefault(_norm(alias), skill.name)
    # Short canonical names that are ordinary words must not match free text.
    for skill in SKILLS:
        if skill.name.lower() in {a.lower() for a in skill.ambiguous}:
            idx.free_text_aliases.pop(skill.name.lower(), None)
    for skill in SKILLS:
        for alias in skill.case_sensitive:
            idx.cs_aliases[alias] = skill.name
    cs = "|".join(re.escape(a) for a in sorted(idx.cs_aliases, key=len, reverse=True))
    idx.cs_pattern = re.compile(rf"(?<![\w+#./-])(?:{cs})(?![\w+#]|\.\w)")
    aliases = sorted(idx.free_text_aliases, key=len, reverse=True)
    # Boundaries allow symbols such as C++, C#, .NET, Node.js, CI/CD.
    alternation = "|".join(re.escape(a) for a in aliases)
    idx.pattern = re.compile(rf"(?<![\w+#./-])(?:{alternation})(?![\w+#]|\.\w)", re.IGNORECASE)
    return idx


def normalize_skill(raw: str) -> str | None:
    """Map an explicitly listed skill (any alias, any case) to its canonical name."""
    if not raw:
        return None
    cleaned = _norm(raw).strip(" .,;:-•*·()[]")
    return _index().by_alias.get(cleaned)


def skill_category(name: str) -> str | None:
    skill = _index().defs.get(name)
    return skill.category if skill else None


def extract_skills_from_text(text: str) -> list[str]:
    """Find canonical skills mentioned in free text (ambiguous aliases excluded)."""
    if not text:
        return []
    idx = _index()
    assert idx.pattern is not None
    found: dict[str, None] = {}
    for match in idx.pattern.finditer(text):
        canonical = idx.free_text_aliases.get(_norm(match.group(0)))
        if canonical:
            found.setdefault(canonical, None)
    assert idx.cs_pattern is not None
    for match in idx.cs_pattern.finditer(text):
        found.setdefault(idx.cs_aliases[match.group(0)], None)
    return list(found)


def normalize_skill_list(items: list[str] | None) -> tuple[list[str], list[str]]:
    """Normalise an explicit skill list.

    Returns ``(canonical_skills, unrecognised_raw_items)``; unknown skills are kept
    separately (title-cased) instead of being discarded or force-matched.
    """
    known: dict[str, None] = {}
    unknown: dict[str, None] = {}
    for item in items or []:
        if not isinstance(item, str) or not item.strip():
            continue
        canonical = normalize_skill(item)
        if canonical:
            known.setdefault(canonical, None)
            continue
        # A list entry may itself be a phrase ("Python and Django").
        inner = extract_skills_from_text(item)
        if inner:
            for skill in inner:
                known.setdefault(skill, None)
        else:
            cleaned = re.sub(r"\s+", " ", item.strip(" .,;:-•*·"))[:60]
            if cleaned and _norm(cleaned) not in {_norm(k) for k in unknown}:
                unknown.setdefault(cleaned, None)
    return list(known), list(unknown)


def expand_implied(skills: list[str]) -> dict[str, str]:
    """Return ``{skill: source}`` where source is ``stated`` or ``implied``."""
    idx = _index()
    result: dict[str, str] = {s: "stated" for s in skills}
    stack = list(skills)
    while stack:
        current = stack.pop()
        skill = idx.defs.get(current)
        if not skill:
            continue
        for implied in skill.implies:
            if implied not in result:
                result[implied] = "implied"
                stack.append(implied)
    return result


def skill_key(name: str) -> str:
    """Comparable key for skills (canonical when known, else lowercase)."""
    return (normalize_skill(name) or name).strip().lower()
