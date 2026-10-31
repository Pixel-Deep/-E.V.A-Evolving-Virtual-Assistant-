
"""Deterministic task routing for EVA."""

import re
from dataclasses import dataclass
from difflib import get_close_matches

from eva.tools.calculator import normalize_expression
from eva.core.intent_understanding import analyze_intent, is_compound_web_task
from eva.automation.orchestration.analyzer import analyze_request


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
    artifact = r"\b(program|porgram|porgramin|code|script|function|class|website|web\s*site|webapp|web\s*app|portfolio|landing\s+page|login\s+(?:page|system|website)|(?:software|desktop|mobile|web)\s+application|software|project|app|site|game|bot|api|server|algorithm|data\s+structure|calculator|tool|extension|plugin|tracker|scraper|crawler|generator|clone|library|package|module|parser|compiler|interpreter|simulator|emulator|converter|downloader|editor|player|notifier|solver|chatbot|cipher|encryptor|decryptor|automation|utility|cli|gui|dashboard|password\s+manager|file\s+renamer|stock\s+price\s+tracker|to[- ]?do(?:\s+list)?)\b"
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
    # Prose about programming languages (comparisons, guides, essays, tutorials, notes)
    # is a writing or document automation task, not software project generation.
    is_prose_document = bool(re.search(
        r"\b(?:comparison|compare|versus|\bvs\b|essay|article|guide|tutorial|summary|summari[sz]e|"
        r"notes?|overview|introduction|intro|pros\s+and\s+cons|advantages|disadvantages|"
        r"cheat\s*sheet|differences?|plan|schedule|timetable|curriculum|roadmap)\b",
        low,
    )) and not bool(re.search(
        r"\b(?:write|code|create|build|generate|implement)\s+(?:a\s+)?(?:script|program|app|code|software|bot|tool|extension|package|library|module|crawler|scraper)\b",
        low,
    ))
    is_text_file_save = bool(re.search(
        r"\b(?:save|export)\b.{0,80}\b(?:text\s+file|\.txt|\.md|notes?|desktop|documents)\b", low
    )) and not bool(re.search(
        r"\b(?:python|java|c\+\+|rust|go)\s+(?:script|program|code|project|app)\b", low
    ))
    is_desktop_workspace = bool(re.search(
        r"\b(?:create|make|set\s*up|prepare|build)\b.{0,80}\b(?:workspace|folders?|directories|directory)\b.{0,80}\b(?:desktop|documents|downloads)\b",
        low,
    )) or bool(re.search(
        r"\b(?:folders? for|folders? named|subfolders?)\b.{0,80}\b(?:desktop|documents|downloads)\b",
        low,
    )) or bool(re.search(
        r"\b(?:roadmap|schedule|study)\b.{0,80}\b(?:in each folder|separate file|text file)\b",
        low,
    ))
    if is_prose_document or is_text_file_save or is_desktop_workspace:
        return False
    if re.search(r"^(?:what\s+(?:is|are|does|was|were)|define|explain|tell\s+me\s+about|how\s+does)\b", low):
        return False
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
    # Coordinate geometry often appears as a natural-language request whose
    # points are written directly as tuples, e.g. "Find the distance between
    # (-2, 3) and (4, -5)". Do not require the literal phrase "between points":
    # the deterministic math engine already understands tuple coordinates.
    coordinate_pairs = re.findall(
        r"\(\s*[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\s*\)",
        low,
    )
    if len(coordinate_pairs) >= 2 and re.search(r"\b(distance|midpoint|slope|gradient)\b", low):
        return True
    named_coordinate_points = re.findall(
        r"\b[a-z]\s*\(\s*[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\s*\)",
        low,
    )
    if len(named_coordinate_points) >= 2 and re.search(r"\b(distance|midpoint|slope|gradient)\b", low):
        return True
    patterns = [
        r"\b(to the power|raised to|power of|squared|cubed|square root|cube root)\b",
        r"\b(calculate|compute|evaluate|simplify|expand|factor|factorise|factorize|differentiate|derivative|integrate|integral|limit of|solve for|solve equation|take the derivative|differentiate with respect to)\b",
        r"\bsolve\b.{0,60}=",
        r"\b(vector|i\s*cap|j\s*cap|k\s*cap|dot product|cross product|magnitude of|angle between .*vectors?)\b",
        r"\b(arithmetic progression|geometric progression|\bap\b|\bgp\b|common difference|common ratio|nth term|\d+(?:st|nd|rd|th) term|sum of first \d+)\b",
        r"\b(sin|cos|tan|cot|sec|cosec|csc|log|ln|factorial|absolute value|matrix|determinant|inverse matrix|transpose|rank of|probability|permutations?|combinations?|choose|average|mean|median|mode|variance|standard deviation|stdev|statistics)\b",
        r"\b(slope|gradient|straight line|equation of a line|distance between points|midpoint|coordinate geometry|quadratic equation|area of|perimeter of|circumference|volume of|surface area)\b",
        r"\b(percent(?:age)? of|percentage increase|percentage decrease|percentage change|ratio|divided by|multiplied by|what is \d|how much is \d)\b",
        r"(?<!\w)(?:plus|minus|times)(?!\s+(?:a|an|the|my|some|separate|extra|additional)\b)",
        r"\b(gcd|hcf|lcm|greatest common|least common multiple|highest common factor|prime factors?|is \d+ (?:a )?prime|divisors of|factors of)\b",
        r"\b(binomial expansion|binomial theorem|series expansion|taylor|maclaurin|summation|sigma)\b",
        r"\b(?:sum\s+of\s+)?(?:the\s+)?(?:integers|whole\s+numbers|numbers)\s+from\s+-?\d+\s+(?:through|to|up\s+to)\s+-?\d+\b",
        r"\b(convert|km to|miles to|celsius to|fahrenheit to|kg to|pounds to|meters to|feet to|inches to|cm to|liters to|gallons to)\b",
        r"\b(roots of|zeros of|degree of|remainder when|quotient when|polynomial division)\b",
        r"\b(real part|imaginary part|conjugate of|modulus of|argument of|complex)\b",
        r"\b(inequality|greater than|less than|solve .+[<>])\b",
        r"\b(quadratic formula|using quadratic formula)\b",
        r"\b(fibonacci|harmonic progression|hp\b)\b",
        # Recognize quantitative word problems before the plain arithmetic
        # fallback. These need symbolic/contextual parsing, not the calculator.
        r"\b(?:tank|container|reservoir)\b.{0,160}\b(?:full|filled|capacity|litres?|liters?|adding)\b",
        r"\b\d+(?:\.\d+)?\s*%\s*(?:of|pass|passed|fail|failed|discount|increase|decrease|both|students?)\b",
        r"\b(?:at least one subject|pass both|passing both|students passing|average speed for the whole journey)\b",
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
    # Plain numeric/symbolic expressions only. Never classify a natural-language
    # sentence as maths merely because it contains a negative test input, a hyphen,
    # a filename, or a parenthesized aside (e.g. "test Java with input -3").
    expression_text = re.sub(r"^(?:solve|evaluate|simplify|expand|factor)\s+", "", low)
    expression_only = re.fullmatch(
        r"\s*[+-]?(?:\d+(?:\.\d*)?[a-z]?|\.\d+|[a-z]|π)"
        r"(?:\s*(?:\*\*|[+\-*/×÷%^=])\s*[+-]?(?:\d+(?:\.\d*)?[a-z]?|\.\d+|[a-z]|π|\([^()]*\)))*\s*",
        expression_text,
    )
    if expression_only and re.search(r"\d", expression_text) and re.search(r"[+\-*/×÷%^=]", expression_text):
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
    nav_match = re.match(r"^(?:please\s+)?(?:open|launch|start|run|go to|visit|navigate to|browse to)\s+", low)
    if nav_match:
        try:
            from eva.automation.desktop import _resolve_website_targets, _resolve_mixed_open_targets
            frag = low[nav_match.end():].strip()
            if _resolve_website_targets(frag) or _resolve_mixed_open_targets(frag):
                return True
        except Exception:
            pass
    # Registry-backed workflows are execution requests, not open-ended chat or code generation.
    # Match these before the coding/web fallbacks; service.handle_text either invokes a
    # real registered tool or returns an explicit missing-input/capability message.
    automation_intents = [
        r"^(?:/gaps|/capability-gaps|show capability gaps|list capability gaps|show missing automation tools)[.!?]?$",
        r"\b(?:show|list|display|describe|explain)\b.{0,100}\b(?:registered|available|automation)\b.{0,100}\b(?:tools|capabilities|permissions|dependencies|parameters|instructions)\b",
        r"\b(?:usage instructions|parameters|limitations|example)\b.{0,100}\b(?:every|each|available|registered)\b.{0,60}\btools?\b",
        r"\b(?:running applications|running apps|open applications|running processes|processes)\b.{0,100}\b(?:resource usage|memory usage|cpu usage|names|details)\b",
        r"\b(?:find|search|list|show)\b.{0,100}\bpdf files?\b.{0,100}\b(?:downloads|documents|folder|names|sizes|locations)\b",
        r"\b(?:list|show|find|search|read|summari[sz]e)\b.{0,120}\b(?:readme(?:\.md)?|[\w.-]+\.py|python files?)\b.{0,100}\b(?:eva project|project|first section|summari[sz]e|folder)?\b",
        r"\b(?:show|list|find|search)\b.{0,100}\b(?:folders? in downloads|png files? in downloads|downloads folders?)\b",
        r"\b(?:find|scan|detect|identify|report|delete|remove|clean(?:up)?)\b.{0,100}\bduplicate files?\b",
        r"\b(?:organize|organise|sort|categorize|categorise|group)\b.{0,100}\b(?:downloads|files|folder|directory)\b",
        r"\b(?:create|make|build|generate)\b.{0,100}\b(?:zip archive|zip file|archive)\b.{0,100}\b(?:folder|project|cache|temporary files?|verify)\b",
        r"\b(?:open|navigate|go to|visit|browse to)\b.{0,100}https?://\S+",
        r"^https?://\S+$",
        r"\b(?:download|save)\b.{0,100}https?://\S+",
        r"\b(?:extract|collect|save)\b.{0,100}\b(?:headings|facts|source urls|source urls|summary report)\b",
        r"\b(?:git status|git diff|show git|current branch|diff summary)\b",
        r"\b(?:analyze|analyse|inspect|check|report on)\b.{0,100}\bproject\b.{0,120}\b(?:errors|health|tests?|lint|build|changed files)\b",
        r"\bcreate\b.{0,60}\bproject health report\b",
        r"\b(?:open|launch|start)\b.{0,70}\b(?:browser|notepad|chrome|edge)\b.{0,100}\b(?:arrange|multitasking|side by side|split screen)\b",
        r"\b(?:write|type|draft|compose|create)\b.{0,120}\b(?:not\s*epad|note\s*pad)\b",
        r"\b(?:save|create|write)\b.{0,100}\bpacking checklist\b.{0,100}\b(?:file|text file|save)\b",
        r"\bopen\b.{0,60}\b(?:my )?browser\b.{0,100}\b(?:website i specify|site i specify|website you specify|url i provide)\b",
        r"\b(?:run|execute)\b.{0,100}\b(?:registered )?(?:build|lint|test) steps\b",
        r"\b(?:create|make|build|design)\b.{0,100}\b(?:reusable )?workflow\b",
        r"\b(?:break|map)\b.{0,80}\b(?:task|request)\b.{0,80}\b(?:deterministic steps|registered tools|validated workflow)\b",
        r"\bclipboard\b.{0,100}\b(?:save|copy)\b.{0,60}\bfile\b|\b(?:read|copy)\b.{0,60}\bclipboard\b.{0,60}\bfile\b",
        r"\b(?:add|put)\b.{0,80}\b(?:in|into|to)\s+(?:my\s+|the\s+)?(?:[a-z0-9_-]+\s+)*(?:cart|basket)\b|\b(?:open|visit|go to|search)\b.{0,60}\b(?:amazon|flipkart|ebay|walmart|target|bestbuy|store)\b.{0,80}\b(?:add|put)\b.{0,40}\b(?:cart|basket)\b",
        r"\b(?:create|make|open)\b.{0,50}\bnew text document\b",
        r"\b(?:check|test|verify)\b.{0,60}\burl\b.{0,60}\b(?:reachability|reachable|status|http)\b",
        # Compound natural-language tasks must reach the deterministic automation
        # bridge so it can try direct handlers and then invoke the local planner.
        r"\b(?:open|launch|start)\b.{0,80}\bnotepad\b.{0,100}\b(?:write|draft|compose|type|letter|application)\b",
        r"\b(?:write|draft|compose|prepare)\b.{0,100}\b(?:letter|application|email|e-mail|message)\b.{0,100}\b(?:notepad|principal|school|@|gmail|email|e-mail)\b",
        r"\b(?:find|locate|search for)\b.{0,100}\b(?:file|document|\.docs)\b.{0,100}\b(?:read|open|show|display|print|terminal|console)\b",
        r"\b(?:read|summari[sz]e)\b.{0,100}\b(?:notes?\.txt|\.txt)\b",
        r"\b(?:write|draft|compose|create)\b.{0,100}\b(?:birthday message|message)\b.{0,100}\b(?:save|text file)\b",
        r"\b(?:search(?:\s+for)?)\b.{0,100}\b(?:copy\s+and\s+paste|copy/paste|keyboard shortcuts)\b.{0,100}\b(?:save|text file)\b",
        r"\bcalculate\b.{0,120}\busing\s+(?:the\s+)?calculator\b",
        r"\b(?:search|find)\b.{0,100}\b(?:screenshots?|screen capture)\b.{0,120}\b(?:save|text file)\b",
        r"\b(?:open|visit|go to)\b.{0,80}\bgmail\b.{0,50}\b(?:spam|junk|inbox|sent|drafts?)\b",
        r"\b(?:create|make|build|generate)\b.{0,80}\b(?:zip archive|zip file|project health report)\b",
        r"\b(?:research|find\s+(?:information|info|resources?|details?))\b.{0,140}\b(?:save|file|desktop|document|text\s+file|notepad)\b",
        r"\b(?:write|draft|compose|prepare|generate|create|make)\b.{0,100}\b(?:introduction|intro|guide|tutorial|overview|comparison|summary|essay|article|research|notes?)\b.{0,100}\b(?:save|file|desktop|document|text\s+file|notepad)\b",
        r"\b(?:save|export)\s+(?:the\s+result|the\s+output|it|this|that)?\s*(?:as|to|into|on|in)\s+[\"'`]?[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9]+)?(?:\s+(?:on|in|to)\s+(?:my\s+)?(?:desktop|documents|downloads))?\b",
        r"\b(?:save|export)\s+(?:the\s+result|the\s+output|it|this|that)?\s*(?:on|to|in)\s+(?:my\s+)?(?:desktop|documents|downloads)\b",
        r"\b(?:search|find|look\s+for)\b.{0,80}\b(?:file|document)\b.{0,80}\b(?:whose|with|that\s+has|containing|that\s+contains)\s+(?:the\s+)?(?:name|filename)\b",
        r"\b(?:search\s+the\s+web|search\s+google|search\s+online|search\s+for)\b.{0,100}\b(?:and\s+)?open\s+(?:it\s+)?(?:in\s+)?(?:my\s+)?(?:browser|chrome|edge|firefox)\b",
        r"\b(?:create|make|prepare|set\s*up)\b.{0,80}\b(?:workspace|folders?)\b.{0,80}\b(?:desktop|documents|downloads)\b",
        r"\b(?:folders?\s+(?:for|named)|subfolders?)\b.{0,80}\b(?:python|java|web|desktop|documents)\b",
        r"\b(?:run|execute)\b.{0,60}\b(?:diagnostic|timeout|cancellation)\b",
        r"\b(?:run|execute)\b.{0,60}\b(?:compound\s+|two-step\s+)?workflow\b",
        r"\b(?:write|save|export)\b.{0,80}\bto\s+[A-Za-z0-9_.-]+\.(?:txt|csv|json|md|docx?|pdf)\b",
    ]
    if any(re.search(pattern, low, re.I) for pattern in automation_intents):
        return True

    patterns = [
        r"^(?:/tools|list automation tools|show automation tools|what tools do you have)[.!?]?$",
        r"^(?:approve move|approve file move|approve last action|approve the last action|approve action|approve pending action|approve organization|approve organization plan|approve organize(?: .+)?|approve organise(?: .+)?|approve plan|approve pending plan|confirm plan|approve task plan)[.!]?$",
        r"\b(?:inspect|read)\s+(?:the\s+)?(?:webpage|web page|website)\s+https?://\S+",
        r"\b(?:list|show)\s+(?:the\s+)?files(?:\s+in\s+.+)?$",
        r"\b(?:find|search\s+for)\s+files?\s+(?:named|matching|like)\s+.+",
        r"\b(?:search|find)\s+files\s+for\s+.+\s+in\s+.+",
        r"\bconvert\s+(?:csv\s+file|json\s+file)\s+.+\s+to\s+(?:json|csv)\s+file\s+.+",
        r"\bcreate\s+(?:a\s+)?zip\s+archive\s+from\s+folder\s+.+\s+to\s+.+",
        r"\bextract\s+zip\s+file\s+.+\s+to\s+.+",
        r"\b(?:create|make)\s+(?:a\s+)?folder\s+(?:named\s+)?\S.*$",
        r"\b(?:copy|move)\s+file\s+.+\s+to\s+.+",
        r"\b(?:copy|move)\b.{0,80}\b(?:files?|pdfs?|documents?|folders?|images?|screenshots?)\b",
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
        r"^(?:please\s+)?(?:type|enter)\s+.{1,}",
        r"\b(?:press|send)\s+(?:the\s+)?keys?\b",
        r"\b(?:play|pause|skip|next|previous)\b.{0,20}\b(?:music|media|song|track|playback)\b",
        r"\b(?:say aloud|speak|read aloud|notify|show a message)\b",
        r"\b(?:quit|close)\s+(?:the\s+)?(?:app(?:lication)?\s+)?[a-z][\w .+-]*$",
        r"\b(?:doctor|automation help|automation status|check automation|what can you control|system info|system information|computer specs|pc specs|hardware info|network info|network status|show ip address|local ip|ip address|wifi status|wi-fi status|what wifi|connected wifi|audio devices|sound devices|check audio devices|test (?:the )?microphone|check (?:the )?microphone|mic test|test (?:the )?speakers?|check (?:the )?speakers?|speaker test|lock (?:the |my )?(?:computer|pc|screen|workstation))\b",
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

        # 1. Block consequential requests unless they are a generic keyboard
        # sequence, which is handled by the automation bridge's one-time approval.
        # Distinguish harmless text/data operations (formatting paragraphs,
        # removing spreadsheet duplicates) from destructive system/disk/file actions.
        polite_prefix = r"(?:(?:please|can you|could you|would you)\s+)*"
        is_consequential = False
        if re.match(polite_prefix + r"(?:shutdown|shut down|restart|reboot)\b", lower):
            is_consequential = True
        elif (re.match(polite_prefix + r"(?:purchase|buy)\b", lower) and not re.search(r"\b(?:add|put)\b.{0,80}\b(?:in|into|to)\s+(?:my\s+|the\s+)?(?:shopping\s+)?(?:cart|basket)\b", lower)) or re.search(r"\b(?:pay\s+for|purchase|buy|checkout)\b.{0,60}\b(?:saved\s+card|credit\s+card|card|bank|account|money)\b|\b(?:book|reserve)\s+(?:a\s+)?(?:flight|hotel|ticket|cab|ride)\b", lower):
            is_consequential = True
        elif re.match(polite_prefix + r"install\b", lower):
            is_consequential = True
        elif re.match(polite_prefix + r"format\b", lower):
            is_text_or_data = bool(re.search(
                r"\bformat\b.{0,60}\b(?:paragraph|bullet|bullet\s*points?|text|code|markdown|table|list|document|string|date|number|currency|cells?|rows?|columns?|json|xml|html|data|content|notes?|essay|message)\b",
                lower,
            ) or re.search(r"\bformat\s+(?:this|the|a|an)\b.{0,40}\binto\b", lower))
            if not is_text_or_data:
                is_consequential = True
        is_windows_temp_deletion = ("windows/temp" in lower.replace("\\", "/") or "c:/windows" in lower.replace("\\", "/")) and any(w in lower for w in ("delete", "cleanup", "remove", "wipe", "purge"))
        if is_windows_temp_deletion or re.search(r"\b(?:execute\s+(?:a\s+)?(?:command\s+to|cleanup\s+by)\s+)?(?:delete|remove|erase|wipe|purge)\b.{0,60}\b(?:system|temporary|temp|all\s+files|disk|drive)\b", lower):
            is_consequential = True
            if is_windows_temp_deletion or re.search(r"\b(?:without|no)\s+(?:asking\s+(?:for\s+)?)?(?:confirmation|confirm|approval|prompting)\b", lower):
                return RouteDecision(
                    "ACTION",
                    "unavailable",
                    "APPROVAL_REQUIRED: Destructive actions and file deletions cannot be executed without explicit user confirmation. EVA security policy strictly prohibits unconfirmed system deletion.",
                    False,
                )
        elif re.match(polite_prefix + r"(?:delete|remove)\b", lower):
            is_duplicate_cleanup = bool(re.search(r"\bduplicate\s+files?\b", lower))
            is_data_cleaning = bool(re.search(
                r"\b(?:delete|remove)\s+(?:all\s+)?(?:duplicate\s+rows?|duplicate\s+lines?|duplicate\s+entries|empty\s+lines?|blank\s+lines?|blank\s+rows?|empty\s+rows?|whitespace|spaces?|unwanted|trailing|nulls?|n/a|na|comments?|items?|elements?|rows?|columns?)\b",
                lower,
            ) or re.search(r"\b(?:from|in)\s+(?:the\s+)?(?:spreadsheet|table|list|text|document|csv|tsv|dataset|dataframe|sheet|workbook|string|paragraph|notes?|array)\b", lower))
            if is_duplicate_cleanup:
                # Duplicate cleanup requests are directed to the safe
                # desktop automation handler, which inspects, previews, and enforces approval
                # rather than falling through to an unrelated conversational answer.
                pass
            elif not is_data_cleaning:
                is_consequential = True
        elif re.match(polite_prefix + r"overwrite\b", lower):
            if re.search(r"\boverwrite\s+(?:the\s+)?(?:disk|drive|partition|volume|hard\s*drive|operating\s*system|system32)\b", lower):
                is_consequential = True
        elif re.match(polite_prefix + r"send\b", lower):
            is_key_sequence = bool(re.match(polite_prefix + r"send\s+(?:the\s+)?keys?\b", lower))
            # "send me ..." is an information request, not a consequential action.
            is_info_request = bool(re.search(r"\bsend\s+me\b", lower))
            # "send ... to notepad/clipboard/file" directs content to a local tool.
            is_local_target = bool(re.search(
                r"\bsend\b.{0,60}\bto\s+(?:notepad|clipboard|file|desktop|documents|downloads)\b", lower
            ))
            if not is_key_sequence and not is_info_request and not is_local_target:
                is_consequential = True

        if is_consequential:
            return RouteDecision(
                "ACTION",
                "unavailable",
                "This consequential action is not available for automatic execution. Use an explicitly approved supported operation where one exists.",
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

        # A date/time mention inside a file, report, document, or compound task
        # is an argument to that task, not the whole request.
        embedded_date_time_task = bool(re.search(
            r"\b(?:create|make|write|save|include|containing|with|folder|file|report|document|letter|application|spreadsheet|workbook)\b",
            lower
        ))
        has_remote_location = bool(re.search(
            r"\b(?:in|at|for)\s+([a-zA-Z\s]{2,40})\b", lower
        ) and not re.search(r"\b(?:in|at|for)\s+(?:my\s+)?(?:computer|system|pc|laptop|clock|area|city|timezone|local|here)\b", lower))
        if (asks_time or asks_date) and not embedded_date_time_task and not has_remote_location:
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

        # A question about EVA's supported abilities is answered from the live
        # automation registry, not a language model's possibly invented feature list.
        if re.search(
            r"\bwhat\s+(?:types?|kinds?)\s+of\s+automation\s+can\s+(?:you|eva)\s+(?:do|perform|handle|support)\b|"
            r"\bwhat\s+can\s+(?:you|eva)\s+automate\b|"
            r"\bwhat\s+tasks\s+can\s+(?:you|eva)\s+(?:automate|handle|perform)\b", lower
        ):
            return RouteDecision("AUTOMATION_CAPABILITIES", "desktop_automation",
                                 "Answer using the capabilities registered in this EVA installation.", True)

        # Parse once before broad desktop/coding/math/web keyword routes.
        # Definition questions containing "file search" or "latest" are not actions.
        intent = analyze_intent(text)
        if intent.intent == "explanation":
            return RouteDecision("EXPLANATION", "ollama", "The user asks about a term or concept, not for an action.", True)

        # High-confidence compound requests owned by the orchestration subsystem
        # share one planning, execution, and verification contract. This check is
        # deliberately before keyword routes so a paraphrase cannot be split into
        # a generic web search or an unrelated file handler.
        analysis = analyze_request(text)
        if analysis is not None:
            return RouteDecision(
                "DESKTOP_AUTOMATION",
                "desktop_automation",
                f"Recognized a {analysis.kind.value} workflow whose outcomes must be planned and verified as one task.",
                True,
            )

        # Compound web actions must be routed before the broad WEB_RESEARCH rule,
        # even when intent analysis returns an unclassified handler. The action order
        # is the user's objective: search X on YouTube then play result two is not a
        # single search request just because the engine name appears after the topic.
        if is_compound_web_task(text):
            return RouteDecision(
                "DESKTOP_AUTOMATION", "desktop_automation",
                "A sequential web task must be planned and executed as one workflow; search results alone do not satisfy it.", True,
            )

        # Explicit online research must beat broad desktop heuristics. The mention
        # of "Python", "latest", or a filename in an online research question
        # must not convert that request into local file or app automation.
        if intent.handler == "desktop_automation" and intent.intent == "web_browsing":
            return RouteDecision("DESKTOP_AUTOMATION", "desktop_automation",
                                 "Use the registered API-free public HTML search and controlled browser tools to read actual pages.", True)
        if intent.handler == "web_search" and intent.intent == "web_research":
            # A compound research request is not completed by returning search
            # snippets. Route it through the orchestration bridge so it can search,
            # open a source, extract real page text/headings, and save a report.
            compound_connector = bool(re.search(
                r"\b(?:and\s+then|then|after\s+that|afterwards|and\s+also|and)\b|[,;]", lower
            ))
            has_action_after_search = bool(re.search(
                r"\b(?:open|follow|select|read|extract|headings?|summari[sz]e|explain|report|save|write|create|compare|download|click|visit|navigate|verify|check|copy|paste)\b",
                lower,
            ))
            has_automation_target = bool(re.search(
                r"\b(?:clipboard|notepad|file|desktop|documents|downloads|folder|\.txt|\.docx?|\.pdf|\.md)\b",
                lower,
            ))
            compound_research = is_compound_web_task(text) or bool(
                (compound_connector and intent.action_count >= 2 and has_action_after_search)
                or (intent.action_count >= 2 and has_automation_target)
            )
            if compound_research:
                return RouteDecision(
                    "DESKTOP_AUTOMATION", "desktop_automation",
                    "Compound web research requires search, page reading, extraction and any requested file output to be orchestrated as one verified task.", True,
                )
            return RouteDecision("WEB_RESEARCH", "web_search", "The user explicitly requested a single web search.", True)

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

        # Resolve ambiguous natural-language intent before broad keyword rules.
        # In particular, a local file task containing "latest" is not web research,
        # "Python files" is not code generation, and an explicit Calculator request
        # is a desktop action rather than a math-engine request.
        if intent.handler == "desktop_automation" and intent.intent in {
            "file_inspection", "file_action", "file_organization", "local_document_task", "calculator_ui",
            "desktop_action", "compound_automation", "multi_app_open", "git_status", "file_creation", "web_browsing",
            "office_artifact_creation", "data_conversion", "archive_operation",
            "project_inspection", "browser_diagnostics", "browser_control", "system_status", "shopping_workflow",
        }:
            reasons = {
                "file_inspection": "Recognized a local file/project inspection request before coding or web keyword rules.",
                "file_action": "Recognized a local file manipulation request (copy/move/delete/organize) to be executed or planned safely.",
                "file_organization": "Recognized a local file organization request.",
                "local_document_task": "Recognized a user-referenced local document rather than a general web query.",
                "calculator_ui": "The user explicitly requested the Calculator application.",
                "desktop_action": "Recognized a direct desktop target from the user's requested action.",
                "compound_automation": "Recognized a compound request over desktop/file automation objects.",
                "multi_app_open": "Recognized an ordered list of application launches and will preserve the requested order.",
                "git_status": "Recognized read-only Git working-tree inspection rather than a chat or math request.",
                "file_creation": "Recognized explicit local file/folder creation and its destination.",
                "office_artifact_creation": "Recognized a requested office-format deliverable rather than a software project.",
                "data_conversion": "Recognized a local file-format transformation, not a mathematical unit conversion.",
                "archive_operation": "Recognized a local archive operation that must use a real archive tool.",
                "project_inspection": "Recognized read-only project inspection rather than code generation or web search.",
                "browser_diagnostics": "Recognized diagnostics for the active browser page rather than a topical web search.",
                "browser_control": "Recognized interaction with a page element rather than a web-search request.",
                "system_status": "Recognized a local system/application status request rather than an online query.",
                "shopping_workflow": "Recognized a website shopping workflow; cart actions must be grounded in page state and purchases remain unapproved.",
            }
            return RouteDecision("DESKTOP_AUTOMATION", "desktop_automation", reasons[intent.intent], True)

        # Generic planning-required intents must not fall through to chat merely
        # because a new task shape has not yet earned a dedicated named category.
        # The planner still validates against registered tools and policy before
        # anything executes; this is a routing decision, not an execution grant.
        if intent.handler == "desktop_automation" and intent.requires_planning:
            return RouteDecision(
                "DESKTOP_AUTOMATION", "desktop_automation",
                "The request contains an execution workflow that should be decomposed and validated against registered tools.", True,
            )

        # Undetermined multi-verb requests that reference automation objects
        # (files, folders, desktop apps, browser, etc.) belong to the planner,
        # not the chat/coding/math fallbacks. The planner validates every step
        # against the live registry and policy before any execution occurs.
        if intent.intent == "undetermined" and getattr(intent, "action_count", 0) >= 2:
            _automation_object_re = re.compile(
                r"\b(?:file|folder|directory|desktop|notepad|browser|url|screenshot|"
                r"clipboard|downloads?|documents?|website|app(?:lication)?|tab|window|"
                r"chrome|edge|firefox|excel|word|pdf|zip)\b"
            )
            if _automation_object_re.search(lower):
                return RouteDecision(
                    "DESKTOP_AUTOMATION", "desktop_automation",
                    "Undetermined multi-action request referencing automation objects routed to planner for validated decomposition.",
                    True,
                )

        # Static validation of an existing named source file belongs to the
        # registered automation tool chain, not ProjectAgent's open-ended project
        # generator. Keep create/build requests in the coding pipeline unless the
        # request clearly validates an already-existing named file.
        static_source_validation = bool(
            re.search(r"\b(?:validate|check|verify|lint|compile)\b.{0,100}\b(?:syntax|source|code|script|program|\w+\.(?:py|pyi|java|js|mjs|cjs|c|h|cpp|cc|cxx|hpp|css|html|htm|json|xml|svg|ts|tsx|rs|go))\b", lower)
            or re.search(r"\b(?:syntax|static)\s+(?:check|validation)\b.{0,100}\b\w+\.(?:py|pyi|java|js|mjs|cjs|c|h|cpp|cc|cxx|hpp|css|html|htm|json|xml|svg|ts|tsx|rs|go)\b", lower)
        )
        creating_code_artifact = bool(re.search(
            r"\b(?:create|write|generate|build|implement|scaffold|develop)\b.{0,100}\b(?:script|program|source code|code file|\w+\.(?:py|pyi|java|js|ts|cpp|c|go|rs))\b",
            lower,
        ))
        if static_source_validation and not creating_code_artifact:
            return RouteDecision(
                "DESKTOP_AUTOMATION", "desktop_automation",
                "Validate the named existing source file through the registered bounded static validator; do not generate a replacement project or execute the source.", True,
            )

        # Running/testing an existing source file is not code generation and must
        # not be diverted to maths just because test inputs include numbers/signs.
        # This environment has no unrestricted code runner, so report that boundary
        # honestly rather than pretending to generate or execute code.
        existing_code_execution = bool(re.search(
            r"\b(?:run|execute|test|debug)\b.{0,100}\b(?:my |this |the )?(?:python|java|javascript|typescript|c\+\+|c#|csharp|rust|go|php|kotlin|swift|c program|source|script|program|code|project|workspace|repo|repository|\w+\.(?:py|java|js|ts|cpp|c|cs|go|rs))\b|"
            r"\b(?:run|execute)\b.{0,60}\b(?:all\s+)?tests?\b|"
            r"\b(?:test|tests?\s+for)\s+(?:my |this |the )?(?:project|repo|repository|codebase|app|workspace)\b",
            lower,
        )) and not bool(re.search(
            r"\b(?:write|create|generate|build|implement|develop|scaffold)\b.{0,100}\b(?:script|program|code|function|class|app|website|tests?)\b",
            lower,
        )) and not bool(re.search(r"\bdiagnostic\b", lower))
        if existing_code_execution:
            return RouteDecision(
                "CODE_TEST", "unavailable",
                "The request is to execute or test existing code; a restricted local code runner is not connected, so EVA must not misroute it as code generation or maths.",
                False,
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
                r"\b(search|browse|google|look up|research|investigate|explore|"
                r"find online|search the web|official website|official page|official documentation|find the official|"
                r"latest|most recent|up[- ]to[- ]date|weather forecast|"
                r"breaking news|current news|recent news|"
                r"current price|today's news|this week's news)\b",
                lower,
            )
        )
        explicit_weather = bool(re.search(r"\bweather\b", lower) and
                                re.search(r"\b(?:tomorrow|forecast|temperature|rain)\b", lower))

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

        # Specific rewriting, drafting, and schedule requests belong to writing, even if
        # they include words such as "professionally" that resemble web queries.
        if re.search(r"\brewrite\b", lower) and re.search(r"\bprofessionally\b", lower):
            return RouteDecision("WRITING", "ollama", "Rewrite the supplied text in a professional tone.", True)
        if re.search(r"\b(?:study )?(?:timetable|plan|schedule)\b", lower) and re.search(r"\b(?:create|make|write|prepare)\b", lower):
            return RouteDecision("WRITING", "ollama", "Create the requested study timetable.", True)
        if re.search(r"\b(?:write|draft|compose|prepare)\b", lower) and re.search(r"\b(?:email|e-mail|letter|application|essay|speech|story|poem)\b", lower):
            return RouteDecision("WRITING", "ollama", "Draft the requested written content.", True)

        # General requests for changing or time-sensitive information.
        asks_current_facts = bool(
            re.search(
                r"\b(current|currently|latest|recent|today|"
                r"right now|this year|as of now)\b",
                lower,
            )
        ) and not asks_time and not asks_date

        asks_for_learning_sites = bool(
            re.search(r"\b(?:find|list|recommend|suggest)\b", lower)
            and re.search(r"\bfree websites?\b", lower)
            and re.search(r"\b(?:learn|learning|practice|typing)\b", lower)
        )

        if explicit_research or explicit_weather or asks_who_holds_office or asks_current_facts or asks_for_learning_sites:
            return RouteDecision(
                "WEB_RESEARCH",
                "web_search",
                "Use web research for current or online information.",
                True,
            )

        # 5. Code execution/testing requires a restricted runner.
        is_explanation_query = bool(re.search(
            r"^(?:what\s+(?:is|are|does|happens)|define|explain|teach\s+me|why|how\s+(?:do|can|would|does)|is\s+it\s+(?:safe|possible)|can\s+(?:i|we|two|multiple|several|processes|threads|programs|anyone))\b",
            lower
        ))
        if not is_explanation_query and (
            re.search(
                r"\b(run|execute|test|debug)\b.*"
                r"\b(code|program|script|function|project|tests?|unit tests?|test cases?|suite)\b",
                lower,
            )
            or re.search(
                r"\b(?:run|execute)\s+(?:the\s+)?(?:unit\s+tests?|test\s+cases?|project\s+tests?)\b",
                lower,
            )
        ):
            return RouteDecision(
                "CODE_TEST",
                "unavailable",
                "A restricted code runner is not connected yet.",
                False,
            )

        # 6. File operations use EVA's registered desktop automation tools.
        if not is_explanation_query and re.search(
            r"\b(read|inspect|search|open|edit|modify)\b.*"
            r"\b(file|folder|directory|project source)\b",
            lower,
        ):
            return RouteDecision(
                "DESKTOP_AUTOMATION",
                "desktop_automation",
                "Use EVA's registered filesystem and desktop tools.",
                True,
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
