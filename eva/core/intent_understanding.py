"""Small deterministic intent analysis shared by routing and automation planning.

This module does not execute actions and does not replace EVA's specialist engines.
Its job is to recognize when words like "latest", "Python", or "Calculator" are
part of a local task rather than signals for a web, coding, or math route.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentAnalysis:
    intent: str
    handler: str
    normalized: str
    signals: tuple[str, ...] = ()
    action_count: int = 1
    requires_planning: bool = False


_SPACE = re.compile(r"\s+")
_LOCAL_VERBS = re.compile(
    r"\b(?:find|locate|look for|search for|list|show|read|inspect|open|display|"
    r"summari[sz]e|extract|scan|check|rename|copy|move|save|create|write|"
    r"edit|correct|rewrite|update|organize|organise|sort|verify|compare|research)\b", re.I
)
_ACTION_VERBS = re.compile(
    r"\b(?:open|launch|start|run|find|locate|search|list|show|read|inspect|"
    r"summari[sz]e|extract|check|verify|create|make|write|draft|compose|type|"
    r"paste|save|download|copy|move|rename|edit|correct|rewrite|update|"
    r"calculate|compute|browse|visit|navigate|play|capture|take|research|compare|order|organize|organise|sort)\b", re.I
)
_FILE_OBJECT = re.compile(
    r"\b(?:my|the|this|that|latest|recent|oldest|older|newer|duplicate|all|any|selected)?\s*(?:assignment|report|notes?|document|"
    r"readme(?:\.md)?|python files?|source files?|project files?|file|files|folder|"
    r"directory|spreadsheet|workbook|pdfs?|images?|screenshots?|downloads|documents|desktop)\b"
    r"|\b[\w.-]+\.(?:py|md|txt|pdf|docx?|xlsx?|csv|json|ya?ml|log|ini|toml)\b",
    re.I,
)
_FILE_INSPECT_VERB = re.compile(
    r"\b(?:find|locate|look for|search(?:\s+for)?|list|show|read|inspect|display|"
    r"summari[sz]e|extract|scan|check|open|explain|understand|describe)\b", re.I
)
_FILE_ACTION_VERB = re.compile(
    r"\b(?:copy|move|rename|delete|remove|archive|backup|organize|organise|sort|clean|cleanup)\b", re.I
)
_CODE_CHANGE = re.compile(
    r"\b(?:fix|debug|refactor|implement|rewrite|modify|edit|patch|change|repair)\b"
    r".{0,100}\b(?:code|bug|source|script|program|function|class|module|project|logic|syntax|error)\b"
    r"|\b(?:fix|debug|refactor|implement|patch|repair)\b.{0,100}\b(?:\.py|\.java|\.js|\.ts|\.c|\.cpp|\.cs)\b",
    re.I,
)
_CODE_CREATE = re.compile(
    r"\b(?:write|create|generate|implement|build|develop|scaffold|code|program)\b"
    r".{0,100}\b(?:code|script|program|function|class|website|web app|(?:software|desktop|mobile|web) application|"
    r"project|game|bot|api|server|algorithm|data structure|automation script|cli|gui)\b"
    r"|\b(?:write|create|generate|build|develop)\b.{0,80}\b(?:in|using|with)\s+"
    r"(?:python|java|javascript|typescript|c\+\+|c#|rust|go|php|kotlin|swift)\b",
    re.I,
)
_EXPLICIT_WEB = re.compile(
    r"\b(?:search the web|search online|research online|research on the web|search the internet|look online|look up|browse the web|web search|"
    r"find online|official website|official page|official documentation|find the official|"
    r"open search results|search youtube|search google|google\s+(?:how|what|why|where|who|when)\b|\bsearch\s+(?:google|online)\b)\b", re.I
)
_COMPOUND_CONNECTOR = re.compile(r"\b(?:and then|then|after that|afterwards|before|and|also)\b", re.I)
_EXPLICIT_BROWSE = re.compile(
    r"\b(?:browse\s+(?:the\s+)?(?:web|internet)|research\s+(?:the\s+)?(?:web|internet)|"
    r"research\s+online|visit\s+(?:this\s+)?(?:website|webpage|page|url|link)|"
    r"read\s+(?:this\s+)?(?:website|webpage|page|url|link)|"
    r"summari[sz]e\s+(?:this\s+)?(?:website|webpage|page|url|link)|"
    r"compare\s+.+?\s+(?:online|on\s+the\s+web)|"
    r"find\s+the\s+official\s+.+?\s+page)\b", re.I
)


def is_compound_web_task(text: str) -> bool:
    """Identify an explicit web-search request followed by another operation.

    Routing and the automation service both use this predicate. Keeping the
    rule here prevents one layer from treating a compound request as a search
    while another layer expects the task planner to orchestrate it.
    """
    low = normalize_command(text).casefold()
    search = bool(re.search(
        r"\b(?:search|look\s+up|find)\b.{0,100}\b(?:google|youtube|wikipedia|web|internet|online|results?|videos?|articles?|information)\b|"
        r"\bsearch\s+(?:about|for)\s+.{1,160}", low
    ))
    connector = bool(re.search(r"\b(?:and\s+then|then|after\s+that|afterwards|and\s+also|and)\b|[,;]", low))
    follow_up = bool(re.search(
        r"\b(?:open|click|follow|visit|navigate|read|inspect|summari[sz]e|extract|save|write|create|download|compare|verify|check|play|select|copy|paste)\b",
        low
    ))
    # Require a sequential clue specifically tying an action after the search;
    # this avoids treating terms such as "open-source search tools" as a workflow.
    sequential_follow_up = bool(re.search(
        r"(?:\b(?:and\s+then|then|after\s+that|afterwards|and\s+also|and)\b|[,;])\s*"
        r"(?:please\s+)?(?:open|click|follow|visit|navigate|read|inspect|summari[sz]e|extract|save|write|create|download|compare|verify|check|play|select|copy|paste)\b",
        low
    ))
    return search and connector and follow_up and sequential_follow_up


def normalize_command(text: str) -> str:
    """Normalize whitespace and common quote wrappers without altering meaning."""
    value = (text or "").replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return _SPACE.sub(" ", value).strip()


def analyze_intent(text: str) -> IntentAnalysis:
    """Classify high-level intent with precedence rules designed to avoid route collisions."""
    normalized = normalize_command(text)
    low = normalized.casefold().strip().rstrip(".!? ")
    if not low:
        return IntentAnalysis("empty", "clarify", low, ("empty_request",), 0, False)

    action_count = len(list(_ACTION_VERBS.finditer(low)))
    has_connector = bool(_COMPOUND_CONNECTOR.search(low))
    signals: list[str] = []

    # Explanatory/how-to questions are not commands. In particular, "Explain this task
    # in simple words: organize my Downloads" must not invoke the organizer; explanation
    # intent wins unless the user is explicitly asking to inspect an actual local file.
    explicit_file_explanation = bool(re.search(
        r"\b(?:explain|describe|understand|tell me what)\b.{0,100}\b(?:this|that|the|my)\s+(?:python|javascript|java|typescript|source|code)?\s*file\b|"
        r"\b(?:explain|describe|understand)\b.{0,80}\b(?:\.py|\.js|\.ts|\.java)\b", low
    ))
    # Definitional, meta, and explanation queries (e.g., "what does latest mean...", "define ...")
    # are always explanations, even if they mention words like "latest", "time", or "file".
    is_definitional_query = bool(re.match(
        r"^(?:what does\b.{1,100}\bmean\b|define\s+|explain the (?:term|meaning|concept)\b|explain why\b|what is the meaning of\b|explain what\b.{0,60}\bmeans?\b)",
        low
    ))

    # Explicit file/folder content inspection queries (e.g. "what is in my downloads", "what is inside folder")
    # must route to file inspection, not conversational explanation.
    is_file_lookup = bool(re.search(
        r"\bwhat\s+(?:is|are)\s+(?:in|inside|under)\s+(?:my\s+|the\s+)?(?:folder|directory|downloads|desktop|documents|repo|repository|project|path)\b|"
        r"\bwhat\s+(?:files?|documents?|folders?|contents?)\s+(?:is|are)\s+(?:in|inside|under|there)\b|"
        r"\bwhat\s+is\s+(?:the\s+)?(?:contents?|size|text|data|extension)\s+of\s+(?:the\s+|this\s+|my\s+)?(?:file|document|folder|directory|archive|[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]{1,8})\b|"
        r"\bwhat\s+(?:does|is)\s+(?:the\s+|this\s+|my\s+)?(?:file|script|code|document)\s+(?:contain|have)\b",
        low
    ))

    # Math questions must not be captured as broad conceptual explanations.
    math_indicators = (
        r"\b(?:calculate|compute|solve|maths?|mathematics|midpoint|slope|gradient|hypotenuse|"
        r"derivative|integral|determinant|eigenvalues?|gcd|hcf|lcm|factorial|square root|cube root|"
        r"distance between|equation of|standard deviation|variance|median|"
        r"(?:mean\s+of|calculate\s+(?:the\s+)?mean|find\s+(?:the\s+)?mean|arithmetic\s+mean)|"
        r"(?:mode\s+of|calculate\s+(?:the\s+)?mode|find\s+(?:the\s+)?mode|statistical\s+mode))\b|"
        r"\b(?:sum|product|quotient|square|cube|difference)\s+of\s+[-+]?\d+|\bvalue\s+of\s+[-+]?\d+|"
        r"\bwhat\s+is\s+[-+]?\d+\s*(?:%|percent(?:age)?)\s+of\s+[-+]?\d+|"
        r"\b\d+\s*(?:%|percent(?:age)?)\s+of\s+[-+]?\d+|"
        r"\bwhat\s+(?:is|was)\s+[-+]?\d+(?:\.\d+)?\s*(?:[\+\-\*\/x×÷\^]|plus|minus|times|multiplied\s+by|divided\s+by)\s*[-+]?\d+|"
        r"\bwhat\s+is\s+(?:the\s+)?(?:square\s+root|cube\s+root|derivative|integral|value|sum|product|quotient|factorial)\s+of\b|"
        r"\bwhat\s+is\s+[-+]?\d+(?:\.\d+)?\b(?!\s+(?:am|pm|o'?clock|version|edition|st|nd|rd|th\b))|"
        r"\b[-+]?\d+(?:\.\d+)?\s*[\+\-\*\/x×÷\^]\s*[-+]?\d+(?:\.\d+)?\b|"
        r"\b[-+]?\d+\s+(?:plus|minus|times|multiplied\s+by|divided\s+by)\s+[-+]?\d+\b|"
        r"\(\s*[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d+(?:\.\d+)?\s*\)"
    )
    is_math_query = not is_definitional_query and bool(re.search(math_indicators, low))

    # Current facts, time-sensitive queries, and live web research questions are not static explanations.
    current_facts_indicators = (
        r"\b(?:current|currently|latest|recent|most recent|right now|this year|as of now|today's|tonight's|yesterday's)\b|"
        r"\b(?:weather|temperature|forecast|stock price|share price|market cap|exchange rate|bitcoin price|crypto price)\b|"
        r"\b(?:population|capital|president|prime minister|ceo|governor|leader|chancellor|monarch)\s+of\b|"
        r"\bcurrent\s+(?:version|release|president|prime minister|ceo|leader|status|weather|price|news|score)\b|"
        r"\b(?:who is|who holds|what is)\s+(?:the\s+)?current\b|"
        r"\bwhat\s+is\s+(?:the\s+)?(?:latest|current|newest|recent)\b|"
        r"\bwhat\s+is\s+(?:the\s+)?(?:weather|temperature|stock\s+price|price\s+of|population\s+of|capital\s+of)\b"
    )
    is_current_info_query = not is_definitional_query and not is_file_lookup and not is_math_query and bool(re.search(current_facts_indicators, low)) and not bool(
        re.search(r"^(?:what is|what's|tell me)\s+(?:the\s+)?(?:current\s+)?(?:date|time|local time)\b", low)
    )
    is_status_or_calc = (
        bool(re.search(r"\b(?:battery|ip\s+address|public\s+ip|local\s+ip|wifi|running\s+(?:apps|processes))\b", low))
        or is_math_query
        or is_current_info_query
        or is_file_lookup
    )

    if is_file_lookup and not is_definitional_query:
        return IntentAnalysis("file_inspection", "desktop_automation", low, ("local_file_lookup",), max(1, action_count), False)

    explanation_pattern = is_definitional_query or (not is_status_or_calc and (
        re.match(r"^(?:(?:can|could)\s+you\s+(?:please\s+)?)?(?:explain|teach me|tell me about|walk me through|describe)\b", low)
        or re.match(r"^(?:what does .{1,100} mean|define\s+|explain the (?:term|meaning|concept)\b|explain why\b|why (?:does|do|should|would|is|are|can|can't|cannot)\b)", low)
        or re.match(r"^(?:what is the meaning of|what is the difference between|difference between)\b", low)
        or re.match(r"^(?:how do i|how can i|how to|what is the (?:command|syntax|way|method) to|how would i)\b", low)
        or re.match(r"^what\s+(?:is|are|was|were)\s+(?:a|an|the|this|that)?\s*(?!date|time|current\s+time|today|\d)[a-z]", low)
        or re.match(r"^(?:is it|is there)\s+(?:possible|safe|allowed|recommended|ok|okay|legal|normal|common|advisable)\s+(?:to|for)\b", low)
        or re.match(r"^(?:can|could)\s+(?:two|multiple|several|different|other|processes|threads|programs|applications|users|anyone)\b", low)
        or re.match(r"^(?:can|could)\s+(?:i|we|one|a\s+user|a\s+developer|a\s+program)\b", low)
        or re.match(r"^what happens\s+(?:if|when|after|upon)\b", low)
        or (re.match(r"^(?:please\s+)?explain\s+(?:this|the|that)\s+task\b", low) and not explicit_file_explanation)
    ))
    if explanation_pattern and not explicit_file_explanation:
        return IntentAnalysis("explanation", "conversation", low, ("definition_or_meta_question",), action_count, False)

    # "Explain what this Python file does" is read-only file analysis, not
    # open-ended explanation/chat. This must not trigger for "explain what Python is".
    explain_local_file = bool(re.search(
        r"\b(?:explain|describe|understand|tell me what)\b.{0,100}\b(?:this|that|the|my)\s+(?:python|javascript|java|typescript|source|code)?\s*file\b|"
        r"\b(?:explain|describe|understand)\b.{0,80}\b(?:\.py|\.js|\.ts|\.java)\b",
        low
    ))

    # Requests to inspect local material win over incidental language such as
    # "latest", "Python", or "project". A genuine code modification remains coding.
    file_context = bool(_FILE_OBJECT.search(low)) or explain_local_file
    inspect_verb = bool(_FILE_INSPECT_VERB.search(low)) or explain_local_file
    code_change = bool(_CODE_CHANGE.search(low))
    code_create = bool(_CODE_CREATE.search(low))
    explicitly_online = bool(_EXPLICIT_WEB.search(low))
    explicitly_browse = bool(_EXPLICIT_BROWSE.search(low)) or bool(re.search(
        r"\b(?:find|look for|open|read|summari[sz]e|explain|research)\b.{0,120}\bofficial.{0,30}\b(?:docs?|documentation|developer docs)\b|"
        r"\bofficial.{0,30}\b(?:docs?|documentation)\b.{0,100}\b(?:install|installation|setup|getting started|first section|examples?)\b",
        low,
    )) or bool(
        re.search(r"https?://[^\s<>\"']+", normalized, re.I)
        and re.search(r"\b(?:read|inspect|extract|summari[sz]e|research|browse|visit|compare)\b", low)
    ) or bool(re.search(
        r"https?://[^\s<>\"']+.{0,180}\b(?:find|locate|search for|read|inspect|extract|headings?|summari[sz]e|click)\b|"
        r"\bopen\s+(?:wikipedia|amazon|youtube|reddit|github|stackoverflow|stack\s*overflow|microsoft|google|mozilla|python(?:\.org)?)\b.{0,100}\b(?:find|read|inspect|extract|headings?|summari[sz]e|click|add to cart)\b",
        low
    ))
    explicit_code_file = bool(re.search(
        r"\b(?:write|create|generate|build|develop|implement)\b.{0,100}\b"
        r"(?:python|java|javascript|typescript|c\+\+|c#|rust|go|php|kotlin|swift)\b.{0,70}\b"
        r"(?:file|script|program|module|(?:software|desktop|mobile|web) application|app)\b", low
    ))
    explicitly_not_code = bool(re.search(r"\b(?:do not|don't|dont|without)\s+(?:generate|create|write|edit|modify)\b", low))

    # Ordered app lists are a desktop workflow, not a Calculator-only request just
    # because Calculator appears among several targets. Parse/launch each target in
    # the requested order and retain per-target outcomes.
    multi_app_open = bool(re.search(
        r"\b(?:open|launch|start)\b.{0,80}\b(?:apps?|applications?)\b.{0,120}\b(?:in this order|in order)\b",
        low
    ))
    if multi_app_open:
        return IntentAnalysis("multi_app_open", "desktop_automation", low,
                              ("ordered_application_launches",), max(2, action_count), True)

    git_inspection = bool(re.search(
        r"\b(?:git status|git diff|current branch|working tree|changed files in git|git changes)\b", low
    )) or bool(re.search(r"\bcheck\b.{0,60}\bgit\b.{0,30}\bstatus\b", low))
    if git_inspection:
        return IntentAnalysis("git_status", "desktop_automation", low,
                              ("read_only_git_inspection",), max(1, action_count), False)

    # Prefer the requested deliverable/action over generic artifact nouns. An
    # Excel expense tracker is a workbook, not a software project; converting a
    # CSV to JSON is a file transformation, not a math problem because it says
    # "convert"; and inspecting a web control is not a web-search query.
    office_output = bool(re.search(
        r"\b(?:create|make|build|generate|prepare|write)\b.{0,100}\b(?:excel|spreadsheet|workbook|word document|powerpoint|presentation|pdf report)\b|"
        r"\b(?:create|make|prepare|write|generate)\b.{0,100}\b(?:report|memo|checklist)\b.{0,100}\b(?:titled|with headings?|save|on my desktop|on desktop|conclusion|introduction)\b|"
        r"\b(?:create|make|generate|prepare)\b.{0,100}\b(?:structured )?word document\b|"
        r"\b(?:expense|budget|invoice|attendance|grade|inventory|tracker)\b.{0,70}\b(?:spreadsheet|workbook|excel|xlsx)\b",
        low
    ))
    software_artifact_context = bool(re.search(
        r"\b(?:script|program|source code|code generator|web app|website|(?:software|desktop|mobile|web) application|software project)\b", low
    )) and bool(re.search(r"\b(?:python|java|javascript|typescript|c\+\+|c#|rust|golang)\b", low))
    # A combined spreadsheet/document + source-code request needs the automation
    # coordinator so it can explicitly report the missing cross-engine capability.
    # Do not let the code half steal the route and silently generate only a project.
    mixed_office_and_code = bool(
        office_output
        and re.search(r"\b(?:script|program|source code|code module|function)\b|"
                      r"\b(?:python|java|javascript|typescript|c\+\+|c#|rust|golang)\b.{0,60}\b(?:script|program|file|module|function|code)\b", low)
        and re.search(r"\b(?:and|also|plus|along with|as well as)\b", low)
    )
    if mixed_office_and_code:
        return IntentAnalysis("office_artifact_creation", "desktop_automation", low,
                              ("mixed_office_and_code_workflow",), max(2, action_count), True)
    if office_output and not code_change and not software_artifact_context:
        return IntentAnalysis("office_artifact_creation", "desktop_automation", low,
                              ("explicit_office_deliverable",), max(1, action_count), True)

    data_conversion = bool(re.search(
        r"\b(?:convert|transform|export|serialize|serialise|clean|filter|deduplicate|dedup)\b.{0,120}\b(?:csv|json|yaml|yml|xml|tsv|xlsx|excel|spreadsheet|data file|file format|table|dataset|dataframe)\b|"
        r"\b(?:remove|delete)\s+(?:all\s+)?(?:duplicate|duplicates|duplicate\s+rows?|empty\s+lines?|blank\s+lines?|nulls?)\b.{0,120}\b(?:from|in)\s+(?:the\s+)?(?:spreadsheet|excel|csv|tsv|table|dataset|dataframe|sheet|workbook|data file|(?:text\s+)?files?|documents?)\b|"
        r"\b(?:remove|delete)\s+(?:all\s+)?(?:duplicate\s+rows?|duplicate\s+entries|duplicate\s+records)\b|"
        r"\b(?:csv|json|yaml|yml|xml|tsv)\b.{0,100}\b(?:to|into|as)\s+(?:csv|json|yaml|yml|xml|tsv|xlsx|excel|spreadsheet)\b",
        low
    ))
    if data_conversion:
        return IntentAnalysis("data_conversion", "desktop_automation", low,
                              ("file_format_transformation",), max(1, action_count), True)

    # Archive classification must be tied to an archive object. The generic verb
    # "extract" is common in web research (extract headings/facts/key points) and
    # must never turn a research workflow into ZIP extraction.
    archive_operation = bool(re.search(
        r"\b(?:zip|archive|compress|unzip)\b.{0,100}\b(?:folder|directory|project|archive|zip|files?|contents?)\b|"
        r"\b(?:extract|unzip)\s+(?:the\s+)?(?:contents?\s+of\s+)?(?:zip(?:ped)?\s+(?:file|archive)|archive)\b|"
        r"\b(?:create|make|build|compress|zip)\b.{0,100}\b(?:zip\s+archive|zip\s+file|archive)\b",
        low
    ))
    if archive_operation:
        return IntentAnalysis("archive_operation", "desktop_automation", low,
                              ("local_archive_operation",), max(1, action_count), True)

    project_inspection = bool(re.search(
        r"\b(?:inspect|analy[sz]e|explain|show|list|describe|review|audit|trace|look through|tell me)\b.{0,100}\b(?:project|repository|repo|folder structure|entry point|directory structure|routing|orchestration|codebase)\b|"
        r"\b(?:folder structure|directory structure|main entry point)\b",
        low
    )) and not code_change and not explicitly_online
    if project_inspection:
        return IntentAnalysis("project_inspection", "desktop_automation", low,
                              ("read_only_project_inspection",), max(1, action_count), True)

    shopping_workflow = bool(re.search(
        r"\b(?:open|visit|go to|navigate to|search|find|buy)\b.{0,80}\b(?:amazon|flipkart|ebay|walmart|target|bestbuy|best\s*buy|aliexpress|costco|etsy|shopify|shopping website|online store|ecommerce|store)\b.{0,140}\b(?:add|put|cart|basket)\b|"
        r"\b(?:add|put|buy)\b.{0,80}\b(?:to|in|into)\s+(?:my\s+|the\s+)?(?:[a-z0-9_-]+\s+)*(?:cart|basket|bag)\b|"
        r"\b(?:add|put)\s+.+?\s+(?:in|into|to)\s+(?:my\s+|the\s+)?(?:[a-z0-9_-]+\s+)*(?:cart|basket|bag)\b|"
        r"\b(?:cart|basket)\b.{0,60}\b(?:add|put)\b",
        low
    ))
    if shopping_workflow:
        return IntentAnalysis("shopping_workflow", "desktop_automation", low,
                              ("website_shopping_action",), max(1, action_count), True)

    browser_diagnostics = bool(re.search(
        r"\b(?:current page|this page|browser|webpage|web page)\b.{0,100}\b(?:console|browser errors?|failed requests?|network errors?|http errors?|diagnostics?)\b|"
        r"\b(?:console errors?|failed network requests?|network requests?)\b.{0,100}\b(?:page|browser|webpage|current)\b",
        low
    ))
    if browser_diagnostics:
        return IntentAnalysis("browser_diagnostics", "desktop_automation", low,
                              ("current_browser_diagnostics",), max(1, action_count), True)

    system_diagnostics = bool(re.search(
        r"\b(?:diagnostic\s+check|system\s+diagnostics?)\b|"
        r"\b(?:timeout|cancel|cancellation)\b.{0,60}\b(?:diagnostic|process|task)\b",
        low
    ))
    if system_diagnostics:
        return IntentAnalysis("system_status", "desktop_automation", low,
                              ("diagnostic_check",), max(1, action_count), True)

    browser_control = bool(re.search(
        r"\b(?:button|link|form|dropdown|checkbox|control|element|tab)\b.{0,100}\b(?:click|press|inspect|find|select|fill|check|read|label|named|called)\b|"
        r"\b(?:inspect|click|press|select|fill|check)\b.{0,100}\b(?:button|link|form|dropdown|checkbox|control|element|tab)\b",
        low
    ))
    if browser_control:
        return IntentAnalysis("browser_control", "desktop_automation", low,
                              ("page_element_interaction",), max(1, action_count), True)

    file_organization = bool(re.search(
        r"\b(?:organize|organise|sort|group|categorize|categorise|tidy|clean up)\b.{0,100}\b(?:my )?(?:downloads|files|folders|documents|desktop|directory|by file type|by extension|by date|into folders)\b|"
        r"\b(?:downloads|files)\b.{0,80}\b(?:by file type|by extension|into folders|by date)\b",
        low,
    ))
    if file_organization and not (code_create or code_change or explicit_code_file) and not explicitly_online:
        return IntentAnalysis("file_organization", "desktop_automation", low,
                              ("local_file_organization",), max(1, action_count), True)

    system_status = bool(re.search(
        r"\b(?:show|list|check|tell me|get|find out|see whether|identify)\b.{0,100}\b(?:available applications|running applications|current system status|system status|system information|pc status|computer status|memory usage|ram usage|cpu usage|disk usage|resource usage|memory-consuming apps|top memory processes|high cpu processes|running processes|system resources)\b|"
        r"\b(?:available applications|running applications|current system status|system status|pc status|computer status|memory usage|ram usage|cpu usage|disk usage|resource usage|memory-consuming apps|top memory processes|high cpu processes|running processes|system resources)\b",
        low
    ))
    if system_status:
        return IntentAnalysis("system_status", "desktop_automation", low,
                              ("local_system_information",), max(1, action_count), False)

    explicit_code_request = (code_change or code_create or explicit_code_file) and not explicitly_not_code
    if explicit_code_request:
        return IntentAnalysis("code_generation", "coding_engine", low,
                              ("explicit_code_change" if code_change else "explicit_code_creation",), action_count, True)

    if explicitly_browse:
        return IntentAnalysis("web_browsing", "desktop_automation", low,
                              ("explicit_public_page_browsing",), max(1, action_count), True)

    if explicitly_online:
        return IntentAnalysis("web_research", "web_search", low, ("explicit_online_intent",), action_count, False)

    local_computer_search = bool(re.search(
        r"\b(?:search|find|look for|locate|scan)\b.{0,100}\b(?:my computer|this pc|my pc|my laptop|my drive|local drive|hard drive|file system|computer)\b.{0,100}\b(?:file|files|document|documents|resume|cv|report|photo|image|pdf|folder|filename|name)\b|"
        r"\b(?:search|find|look for|locate|scan)\b.{0,60}\b(?:on|in)\s+(?:my )?(?:computer|pc|laptop|drive)\b",
        low,
    ))
    if local_computer_search:
        return IntentAnalysis("file_inspection", "desktop_automation", low,
                              ("explicit_local_computer_search",), max(1, action_count), True)

    file_action = bool(_FILE_ACTION_VERB.search(low))
    if file_context and (inspect_verb or file_action) and not (code_change and re.search(r"\b(?:fix|debug|refactor|patch|repair|implement)\b", low)):
        signals.extend(("local_file_or_project_object", "file_action_or_inspection_verb"))
        if re.search(r"\b(?:latest|recent|newest|oldest|older|newer)\b", low):
            signals.append("recency_modifies_local_object")
        compound = action_count >= 2 or has_connector or bool(re.search(
            r"\b(?:correct|rewrite|summari[sz]e|extract|save|copy|move|open)\b", low
        ) and re.search(r"\b(?:find|locate|search|read|inspect|list|show)\b", low))
        kind = "file_inspection" if (inspect_verb and not file_action) else "file_action"
        return IntentAnalysis(kind, "desktop_automation", low, tuple(signals), action_count, compound or file_action)

    # Lock screen/workstation requests
    if re.search(r"\b(?:lock\s+(?:the\s+|my\s+)?(?:screen|computer|pc|workstation)|lock_screen)\b", low):
        return IntentAnalysis("desktop_action", "desktop_automation", low,
                              ("explicit_lock_screen_operation",), max(1, action_count), False)

    # Clipboard operations are direct desktop actions even when they contain only
    # one verb. Without this rule, "Copy this text to the clipboard: ..." falls
    # through as undetermined and relies on a legacy parser's wording quirks.
    clipboard_action = bool(re.search(
        r"\b(?:copy|put|set|write)\b.{0,100}\bclipboard\b|"
        r"\b(?:read|get|show|clear)\b.{0,60}\bclipboard\b|"
        r"\bclipboard\b.{0,60}\b(?:read|get|show|clear|contents?)\b",
        low,
    ))
    if clipboard_action:
        return IntentAnalysis("desktop_action", "desktop_automation", low,
                              ("explicit_clipboard_operation",), max(1, action_count),
                              action_count > 1 or has_connector)

    # If the user explicitly names the Calculator app as the instrument, send the
    # request through desktop automation; plain arithmetic remains deterministic math.
    if re.search(r"\b(?:calculator|windows calculator|calc app)\b", low) and re.search(
        r"\d|\b(?:calculate|compute|evaluate|plus|minus|times|multiply|divide|open|use)\b|[+*/×÷-]", low
    ) and not code_create:
        return IntentAnalysis("calculator_ui", "desktop_automation", low,
                              ("explicit_calculator_app",), max(1, action_count), True)

    # "Make a folder for my college documents" names the folder by its purpose; it
    # is not an instruction to create a directory literally named "for my ...".
    folder_for_purpose = bool(re.search(
        r"\b(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory)\s+for\s+(?:(?:my|the)\s+)?[A-Za-z0-9][A-Za-z0-9 _-]{1,80}[.!?]*$", low
    ))
    if folder_for_purpose:
        return IntentAnalysis("file_creation", "desktop_automation", low,
                              ("folder_named_from_purpose",), max(1, action_count), False)

    # Explicit file/folder creation with a concrete local destination is automation,
    # not chat or software-project generation. Code creation is handled separately.
    file_creation = bool(re.search(
        r"\b(?:create|make|save|write|add|new|research)\b.{0,120}\b(?:file|text file|note|folder|directory|document|summary|report|guide|tutorial|introduction|intro|comparison)\b", low
    )) or bool(re.search(
        r"\b(?:save|export)\s+(?:the\s+result|the\s+output|it|this|that)?\s*(?:as|on|in|to)\b", low
    ))
    has_destination = bool(re.search(r"\b(?:on|in|to)\s+(?:my\s+)?(?:desktop|documents|downloads|a folder|the folder)\b", low))
    has_named_output = bool(re.search(r"\b(?:named|called|as|name it as|name it)\s+[\"'`]?[^\"'`]{1,150}(?:\.[a-z0-9]{1,8})?", low) or re.search(r"\b[\w.-]+\.(?:txt|md|csv|json|ya?ml|xml|log|ini|toml|html|css|js|py|java|c|cpp|pdf|docx?|xlsx?|pptx?)\b", low, re.I))
    if (file_creation or (re.search(r"\b(?:save|export)\b", low) and (has_destination or has_named_output))) and (has_destination or has_named_output) and not code_create:
        return IntentAnalysis("file_creation", "desktop_automation", low,
                              ("local_artifact_creation", "explicit_destination_or_name"), max(1, action_count), True)

    # Clear direct desktop targets should not be diverted by generic words like code,
    # search, or latest. The actual allowed target/tool check is still in the executor.
    nav_match = re.match(r"^(?:please\s+)?(?:open|launch|start|run|navigate to|go to|visit|browse to)\b", low)
    if nav_match:
        try:
            from eva.automation.desktop import _resolve_website_targets, _resolve_mixed_open_targets
            frag = low[nav_match.end():].strip()
            resolved = _resolve_website_targets(frag) or _resolve_mixed_open_targets(frag)
        except Exception:
            resolved = []
        if resolved or re.search(
            r"\b(?:file explorer|explorer|desktop|documents|downloads|notepad|note pad|"
            r"calculator|chrome|edge|firefox|vs code|visual studio code|youtube|gmail|"
            r"google drive|spotify|settings|browser|instagram|facebook|github|whatsapp)\b", low
        ):
            intent_kind = "multi_app_open" if len(resolved) >= 2 else "desktop_action"
            return IntentAnalysis(intent_kind, "desktop_automation", low,
                                  ("explicit_desktop_action",), max(1, action_count), action_count > 1 or has_connector or len(resolved) >= 2)

    # A task can be a multi-step automation without containing a canonical template.
    # Examples: find a report, summarize it, and save both the summary and original.
    automation_context = bool(re.search(
        r"\b(?:file|files|folder|directory|documents?|assignments?|reports?|notes?|readme|"
        r"downloads|desktop|notepad|note pad|browser|chrome|website|url|clipboard|"
        r"screenshot|checklist|timetable|spreadsheet|pdf|image)\b", low
    ))
    if action_count >= 2 and has_connector and automation_context and not explicitly_online:
        if code_create and not inspect_verb:
            return IntentAnalysis("code_generation", "coding_engine", low,
                                  ("explicit_code_creation",), action_count, True)
        return IntentAnalysis("compound_automation", "desktop_automation", low,
                              ("multiple_actions", "automation_objects"), action_count, True)

    # A local owned/known document query such as "find my latest assignment" is
    # local-file intent even without the word "file". Do not treat "latest" alone
    # as a web-search instruction.
    if re.search(r"\b(?:find|locate|look for|show|open|read|summari[sz]e)\b", low) and re.search(
        r"\b(?:my|this|that|the)\s+(?:latest|recent|newest|oldest)?\s*(?:assignment|report|notes?|document|spreadsheet|resume|cv)\b", low
    ) and not explicitly_online:
        return IntentAnalysis("local_document_task", "desktop_automation", low,
                              ("possessive_document_reference",), action_count, action_count >= 2 or has_connector)

    if explicitly_browse:
        return IntentAnalysis("web_browsing", "desktop_automation", low,
                              ("explicit_public_page_browsing",), max(1, action_count), True)

    if explicitly_online:
        return IntentAnalysis("web_research", "web_search", low, ("explicit_online_intent",), action_count, False)

    # A request to do several desktop/file operations should be allowed to reach
    # TaskPlanner even when a phrase like "then" or a specific known template is absent.
    if action_count >= 2 and automation_context and not (code_create and not inspect_verb):
        return IntentAnalysis("compound_automation", "desktop_automation", low,
                              ("multiple_actions", "automation_objects"), action_count, True)

    return IntentAnalysis("undetermined", "", low, tuple(signals), action_count, False)
