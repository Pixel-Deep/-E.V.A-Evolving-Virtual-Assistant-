
"""Deterministic task routing for EVA."""

import re
from dataclasses import dataclass
from difflib import get_close_matches

from eva.tools.calculator import normalize_expression


@dataclass(frozen=True)
class RouteDecision:
    """Describe the selected route for a user request."""

    category: str
    handler: str
    reason: str
    implemented: bool


def _tokens(text: str) -> list[str]:
    """Extract lowercase words while ignoring punctuation."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _has_fuzzy_word(tokens: list[str], target: str) -> bool:
    """Recognize common spelling mistakes for selected words."""

    aliases = {
        "time": {"tym", "tiem", "tme"},
        "today": {"todays", "tday", "todai"},
        "minister": {"minster", "menister", "minester"},
        "president": {"presedent", "presdent"},
        "america": {"amrica", "amerıca"},
    }

    for token in tokens:
        if token == target or token in aliases.get(target, set()):
            return True

        if len(token) >= 4 and len(target) >= 4:
            if get_close_matches(token, [target], n=1, cutoff=0.78):
                return True

    return False


def _looks_like_coding_request(text: str) -> bool:
    """Recognize open-ended software creation requests before other routes."""
    low = text.lower()
    action = r"\b(write|create|generate|implement|build|design|develop|make|code|program|scaffold|refactor|fix|debug|script|scrape|automate)\b"
    artifact = r"\b(program|porgram|porgramin|code|script|function|class|website|web\s*site|webapp|web\s*app|portfolio|landing\s+page|login\s+(?:page|system|website)|application|software|project|app|site|game|bot|api|server|algorithm|data\s+structure|calculator|tool|extension|plugin|tracker|scraper|crawler|generator|clone|library|package|module|parser|compiler|interpreter|simulator|emulator|converter|downloader|editor|player|notifier|solver|chatbot|cipher|encryptor|decryptor|automation|utility|cli|gui|dashboard|password\s+manager|file\s+renamer|stock\s+price\s+tracker|to[- ]?do(?:\s+list)?)\b"
    language = r"\b(java|python|py|c\+\+|cpp|csharp|c#|\bc\b|javascript|typescript|\bts\b|html|css|rust|golang|go\b|php|kotlin|swift|node(?:\.js)?|react|vue|svelte|angular|django|flask|fastapi|express|bash|shell|\bsh\b|sql|\bsqlite\b|dart|flutter|perl|lua|matlab)\b"
    # Keep action and artifact distinct. Without this, "open VS Code" is
    # incorrectly classified as coding because the single word "Code" matches
    # both the action and artifact regexes. Direct app launches are actions, not
    # requests to generate a project.
    action_match = re.search(action, low)
    artifact_matches = list(re.finditer(artifact, low))
    language_matches = list(re.finditer(language, low))
    distinct_artifact = bool(
        action_match and any(match.span() != action_match.span() for match in artifact_matches)
    )
    if action_match and (distinct_artifact or language_matches):
        return True
    # Nouns-only requests still describe software when they name an artifact
    # and a language together: "a tic tac toe game in java", "scrape prices in python".
    if re.search(artifact, low) and re.search(language, low):
        return True
    # "i want a to-do list app", "give me a password generator" have no action verb.
    if re.search(r"\b(i want|i need|give me|make me|build me|write me|create me|would like|want a|need a)\b", low) and re.search(artifact, low):
        return True
    # Bare noun phrases ("a snake game", "password manager app") describe code
    # when they name a distinctive software artifact in an imperative-style
    # phrase without questions or opinions.
    software_names = r"\b(game|app|bot|website|webapp|extension|plugin|tracker|scraper|crawler|generator|clone|parser|compiler|simulator|emulator|converter|downloader|editor|notifier|solver|chatbot|encryptor|decryptor|cli|gui|dashboard|password manager|to[- ]do(?: list)?)\b"
    if re.match(r"^(?:a|an|some|any|one)\s+", low) and re.search(software_names, low) and not low.endswith(("?", "!")):
        if not re.search(r"\b(what|why|who|where|how|is|are|was|were|do|does|did|i (?:think|like|love|hate)|tell me|explain|game of)\b", low):
            return True
    if re.search(r"\b(build|design|create|make|develop)\b", low) and re.search(
        r"\b(portfolio|hotel menu|college result|student results|hospital staff|indian flag|dashboard|landing page)\b", low
    ):
        return True
    return False


def _is_supportive_conversation(text: str) -> bool:
    """Keep vulnerable/emotional conversation out of generic web search."""
    low = text.lower()
    return bool(re.search(
        r"\b(feeling low|feel low|feeling down|feel down|sad today|talk to me softly|"
        r"need someone to talk|i am upset|i'm upset|i feel terrible|help me emotionally|"
        r"my head is cracking|i feel lonely|i'm lonely|i am lonely)\b", low
    ))


def _looks_like_math(text: str) -> bool:
    """Conservatively identify mathematical intent before the LLM route."""
    low = text.lower()
    patterns = [
        r"\b(to the power|raised to|power of|squared|cubed|square root|cube root)\b",
        r"\b(calculate|compute|evaluate|simplify|expand|factor|factorise|factorize|differentiate|derivative|integrate|integral|limit of|solve for|solve equation|take the derivative|differentiate with respect to)\b",
        r"\b(vector|i\s*cap|j\s*cap|k\s*cap|dot product|cross product|magnitude of|angle between .*vectors?)\b",
        r"\b(arithmetic progression|geometric progression|\bap\b|\bgp\b|common difference|common ratio|nth term|\d+(?:st|nd|rd|th) term|sum of first \d+)\b",
        r"\b(sin|cos|tan|cot|sec|cosec|csc|log|ln|factorial|absolute value|matrix|determinant|inverse matrix|transpose|rank of|probability|permutations?|combinations?|choose|average|mean|median|mode|variance|standard deviation|stdev|statistics)\b",
        r"\b(slope|gradient|straight line|equation of a line|distance between points|midpoint|coordinate geometry|quadratic equation|area of|perimeter of|circumference|volume of|surface area)\b",
        r"\b(percent(?:age)? of|percentage increase|percentage decrease|percentage change|ratio|divided by|multiplied by|plus|minus|times|what is \d|how much is \d)\b",
        r"\b(gcd|hcf|lcm|greatest common|least common multiple|highest common factor|prime factors?|is \d+ (?:a )?prime|divisors of|factors of)\b",
        r"\b(binomial expansion|binomial theorem|series expansion|taylor|maclaurin|summation|sigma)\b",
        r"\b(convert|km to|miles to|celsius to|fahrenheit to|kg to|pounds to|meters to|feet to|inches to|cm to|liters to|gallons to)\b",
        r"\b(roots of|zeros of|degree of|remainder when|quotient when|polynomial division)\b",
        r"\b(real part|imaginary part|conjugate of|modulus of|argument of|complex)\b",
        r"\b(inequality|greater than|less than|solve .+[<>])\b",
        r"\b(quadratic formula|using quadratic formula)\b",
        r"\b(fibonacci|harmonic progression|hp\b)\b",
    ]
    if any(re.search(p, low) for p in patterns):
        return True
    if re.search(r"(?<!\w)\d+\s*!", low):
        return True
    # Unicode superscripts/subscripts and mathematical operators must be
    # detected before ordinary chat routing. In particular, 3⁻² has no ASCII
    # operator, so the old check incorrectly sent it to the language model.
    unicode_math = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉×÷−√π"
    if any(char in text for char in unicode_math):
        return bool(re.search(r"\d|[a-zA-Z]", text))
    # Plain numeric/symbolic expressions with operators, powers, or equations.
    if re.search(r"\d", low) and re.search(r"(?:[+\-*/×÷%^=()]|\*\*)", low):
        return True
    return False


def _looks_like_desktop_automation(text: str) -> bool:
    """Recognize direct computer-control requests, not requests to code automation."""
    low = text.lower().strip()
    if re.fullmatch(r"/?desktop[_ -]?automation[.!]?", low):
        return True
    # Explicit requests to write/build automation remain coding tasks.
    if re.search(r"\b(write|code|create|build|implement|debug|fix|generate)\b.{0,35}\b(script|program|automation|bot|app|code)\b", low):
        return False
    # Direct app-launch phrases are never coding requests. Keep this explicit
    # for VS Code because small local models and older route tables often match
    # the word "code" and send "open VS Code" into project generation.
    if re.fullmatch(
        r"(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+)?"
        r"(?:vs\s*code|vscode|visual\s+studio\s+code)"
        r"(?:\s+(?:app|application|editor))?[.!]?",
        low,
    ):
        return True
    # Use the same curated website alias list as the executor so any bundled
    # website (e.g. Canva, Notion, Trello) routes to automation instead of chat.
    if re.match(r"^(?:please\s+)?(?:open|launch|start|run)\s+", low):
        try:
            from eva.automation.desktop import _resolve_website_targets
            if _resolve_website_targets(re.sub(r"^(?:please\s+)?(?:open|launch|start)\s+", "", low)):
                return True
        except Exception:
            pass
    patterns = [
        r"\b(?:public ip|my public ip|external ip|find my ip)\b",
        r"\b(?:create|make|new)\s+(?:a\s+)?(?:python|java|text|html|css|javascript|js|json|xml|csv|markdown|yaml|yml|pdf|word|excel|powerpoint)\s+file\b",
        r"\b(?:open new tab|close (?:this )?tab|refresh (?:the )?(?:page|website|tab)|switch to next tab|switch to previous tab|open (?:browser )?history|open bookmarks|go back|go forward|developer tools|dev tools|full ?screen|scroll up|scroll down|scroll to top|scroll to bottom|seek forward|seek backward|increase playback speed|decrease playback speed|next video|previous video|zoom in|zoom out|open browser menu|open private window|pause video|play video|toggle video|fullscreen)\b",
        r"\b(?:play|find|search for)\s+.+?\s+(?:on|in)\s+(?:youtube|spotify)\b",
        r"\b(?:open|launch|start)\b.*\b(?:youtube|yotube|youtub|youtbe|spotify|github|google|gmail|chatgpt|reddit|netflix|instagram|insta|ig|facebook|fb|linkedin|whatsapp|whatsap|whatsupp|whats\s+app)\b",
        r"\b(?:play|find|search for)\s+.+?\s+(?:on|in)\s+(?:youtube|spotify)\b.*\b(?:and\s+)?(?:open|launch|start)\b",
        r"\b(open|launch|start)\s+(?:the\s+)?(?:app(?:lication)?\s+)?(?:youtube|google|google drive|drive|wikipedia|github|chatgpt|gmail|reddit|spotify|netflix|notepad|camera|calculator|settings|explorer|file explorer|browser|chrome|edge|firefox|vlc|vs\s*code)\b",
        r"\b(open|launch|start)\s+(?:the\s+)?(?:downloads?|documents?|desktop|pictures?|music|videos?)\s*(?:folder|directory)?\b",
        r"\b(open|launch|start)\s+(?:https?://|www\.)",
        r"\b(?:show|list|check|get|what is|tell me)\b.{0,25}\b(?:battery|battery level|volume level|running apps|open apps|clipboard|automation status)\b",
        r"\b(?:increase|raise|lower|decrease|turn up|turn down|mute|unmute)\b.{0,20}\b(?:volume|sound|audio)\b|\bvolume\b.{0,20}\b(?:up|down|mute|unmute|increase|decrease)\b|\bset volume\b",
        r"\b(?:take|capture)\b.{0,15}\bscreenshot\b|\bscreenshot\b",
        r"\b(?:read|get|show|copy|set)\b.{0,20}\bclipboard\b",
        r"\b(?:type|enter)\s+.{1,}",
        r"\b(?:press|send)\s+(?:the\s+)?keys?\b",
        r"\b(?:play|pause|skip|next|previous)\b.{0,20}\b(?:music|media|song|track|playback)\b",
        r"\b(?:say aloud|speak|read aloud|notify|show a message)\b",
        r"\b(?:quit|close)\s+(?:the\s+)?(?:app(?:lication)?\s+)?[a-z][\w .+-]*$",
        r"\b(?:doctor|automation help|automation status|check automation|what can you control|system info|system information|computer specs|pc specs|hardware info|network info|network status|show ip address|local ip|ip address|wifi status|wi-fi status|what wifi|connected wifi|audio devices|sound devices|check audio devices|test (?:the )?microphone|check (?:the )?microphone|mic test|test (?:the )?speakers?|check (?:the )?speakers?|speaker test|lock (?:the )?(?:computer|pc|screen|workstation))\b",
        r"\b(?:brightness|screen brightness)\b.{0,20}\b(?:up|down|increase|decrease|brighter|dimmer|higher|lower|status|level|current)\b|\b(?:increase|raise|decrease|lower|set|change)\b.{0,15}\bbrightness\b",
    ]
    return any(re.search(p, low) for p in patterns)


class TaskRouter:
    """Choose a handler using explicit routing rules."""

    def route(self, user_input: str) -> RouteDecision:
        text = user_input.strip()
        lower = text.lower()
        tokens = _tokens(text)

        if not text:
            return RouteDecision(
                "AMBIGUOUS", "clarify", "The request is empty.", True
            )

        # 1. Block consequential actions from automatic execution.
        if re.match(
            r"^(delete|remove|send|install|purchase|buy|"
            r"shutdown|shut down|restart|format|overwrite)\b",
            lower,
        ):
            return RouteDecision(
                "ACTION",
                "unavailable",
                "System actions require a policy and approval layer.",
                False,
            )

        # 2. Local date and time.
        asks_time = (
            _has_fuzzy_word(tokens, "time")
            and (
                any(word in tokens for word in ("what", "tell", "current", "now"))
                or len(tokens) <= 3
            )
        )

        asks_date = (
            _has_fuzzy_word(tokens, "date")
            and (
                any(word in tokens for word in ("today", "current", "what", "tell"))
                or len(tokens) <= 4
            )
        )

        if asks_time or asks_date:
            return RouteDecision(
                "SYSTEM_TIME",
                "system_time",
                "Use the computer's local clock.",
                True,
            )

        # User-facing support should not be mistaken for a time-sensitive web query.
        if _is_supportive_conversation(text):
            return RouteDecision(
                "CHAT", "ollama", "Respond supportively to the user's conversational intent.", True
            )

        # Hard guard for direct VS Code launch phrases. This is intentionally
        # evaluated before coding classification so "open VS Code" cannot ever
        # be interpreted as a request to generate code, even if aliases change.
        if re.fullmatch(
            r"(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+)?"
            r"(?:vs\s*code|vscode|visual\s+studio\s+code)"
            r"(?:\s+(?:app|application|editor))?[.!]?",
            lower,
        ):
            return RouteDecision(
                "DESKTOP_AUTOMATION", "desktop_automation",
                "Direct request to launch the Visual Studio Code application.", True
            )

        # Direct desktop-control requests go to the allow-listed automation bridge.
        # This must precede generic coding/web/chat routes so "open YouTube" is an action.
        if _looks_like_desktop_automation(text):
            return RouteDecision(
                "DESKTOP_AUTOMATION",
                "desktop_automation",
                "Use EVA's OS-aware, allow-listed desktop automation bridge.",
                True,
            )

        # Explicit software/project requests always enter the coding pipeline.
        # This precedes maths and web routing so code about numbers or websites
        # cannot be diverted into a calculator, search, or ordinary chat.
        if _looks_like_coding_request(text):
            return RouteDecision(
                "CODING", "coding_engine", "Use general-purpose project synthesis and validation.", True
            )

        # Mathematics always takes precedence over the language model and web search.
        # The deterministic engine either solves it or reports a clear limitation.
        if _looks_like_math(text):
            return RouteDecision(
                "MATHEMATICS",
                "math_engine",
                "Mathematical intent is handled by the deterministic symbolic math engine.",
                True,
            )

        # 3. AP/GP questions must be routed before ordinary arithmetic.
        is_progression = (
            "progression" in tokens
            or bool(re.search(r"\b(?:ap|gp)\b", lower))
        )

        progression_request = is_progression and (
            bool(
                re.search(
                    r"\b(sum|terms?|sequence|progression|"
                    r"common difference|common ratio)\b",
                    lower,
                )
            )
            or len(re.findall(r"-?\d+(?:\.\d+)?", text)) >= 3
        )

        if progression_request:
            return RouteDecision(
                "MATH_SOLVING",
                "math_solver",
                "Use the deterministic AP/GP progression solver.",
                True,
            )

        # 4. Current facts, office holders, and explicit web research.
        explicit_research = bool(
            re.search(
                r"\b(search|browse|google|look up|"
                r"find online|search the web|official website|"
                r"latest|most recent|up[- ]to[- ]date|"
                r"breaking news|current news|recent news|"
                r"current price|today's news|this week's news)\b",
                lower,
            )
        )

        office_pattern = (
            r"\b(?:"
            r"prime\s+(?:minister|minster|menister|minester|min[iy]ster)|"
            r"president|presedent|presdent|"
            r"chief\s+(?:minister|minster|menister)|"
            r"governor|ceo|leader|"
            r"prime\s+minister|"
            r"pm"
            r")\b"
        )

        mentions_office = bool(re.search(office_pattern, lower))

        asks_who_holds_office = mentions_office and (
            bool(
                re.search(
                    r"\b(who|current|currently|today|now|"
                    r"name|holder|holds|holding|incumbent)\b",
                    lower,
                )
            )
            or bool(re.search(r"\b(?:of|in)\b", lower))
        )

        # General requests for changing or time-sensitive information.
        asks_current_facts = bool(
            re.search(
                r"\b(current|currently|latest|recent|today|"
                r"right now|this year|as of now)\b",
                lower,
            )
        ) and not asks_time and not asks_date

        if explicit_research or asks_who_holds_office or asks_current_facts:
            return RouteDecision(
                "WEB_RESEARCH",
                "web_search",
                "Use web research for current or online information.",
                True,
            )

        # 5. Code execution/testing requires a restricted runner.
        if re.search(
            r"\b(run|execute|test|debug)\b.*"
            r"\b(code|program|script|function)\b"
            r"|\b(unit tests?|test cases?)\b",
            lower,
        ):
            return RouteDecision(
                "CODE_TEST",
                "unavailable",
                "A restricted code runner is not connected yet.",
                False,
            )

        # 6. File operations require approved file tools.
        if re.search(
            r"\b(read|inspect|search|open|edit|modify)\b.*"
            r"\b(file|folder|directory|project source)\b",
            lower,
        ):
            return RouteDecision(
                "FILE_OPERATION",
                "unavailable",
                "Approved file tools are not connected yet.",
                False,
            )

        # 8. Deterministic arithmetic.
        normalized = normalize_expression(text)

        expression_only = bool(
            re.fullmatch(r"[0-9\s()+*/%.\-]+", normalized)
            and re.search(r"\d", normalized)
            and re.search(r"[+\-*/%]", normalized)
        )

        calculation_request = bool(
            re.search(
                r"\b(calculate|compute|evaluate|percentage|"
                r"percent of|multiply|divide|subtract|"
                r"sum of|product of|how much is|what is)\b",
                lower,
            )
        )

        
        has_numbers = bool(re.search(r"\d", normalized))
        has_operator = bool(re.search(r"[+\-*/%×÷]", text))

        percentage_request = bool(
            re.search(
                r"\b\d+(?:\.\d+)?\s*percent\s+of\s+\d+(?:\.\d+)?\b",
                lower,
            )
            or re.search(
                r"\b\d+(?:\.\d+)?\s*%\s*(?:of\s*)?\d+(?:\.\d+)?\b",
                lower,
            )
        )

        if expression_only or (
            calculation_request
            and has_numbers
            and (has_operator or percentage_request)
        ):

            return RouteDecision(
                "CALCULATION",
                "calculator",
                "A deterministic calculation is required.",
                True,
            )

        # 10. Writing and drafting.
        if re.search(
            r"\b(write|draft|compose)\b.*"
            r"\b(email|letter|application|message|script)\b",
            lower,
        ):
            return RouteDecision(
                "WRITING",
                "ollama",
                "Writing and drafting use the local language model.",
                True,
            )

        # 11. Explanations and general knowledge.
        if re.search(
            r"\b(explain|teach me|what does|how does|"
            r"what is|what are|who are|why does|why do|how do)\b",
            lower,
        ):
            return RouteDecision(
                "EXPLANATION",
                "ollama",
                "General explanations use the local language model.",
                True,
            )

        # 12. Ordinary conversation and personal advice.
        return RouteDecision(
            "CHAT",
            "ollama",
            "Conversation and personal advice use the local language model.",
            True,
        )
