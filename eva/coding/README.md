# EVA Coding Engine v2 — model-independent coding toolkit

EVA's Coding Engine is a deterministic coding workbench. It does **not** use an LLM, Ollama, cloud API, or paid service to construct code. It combines a persistent knowledge store, project inspection, language-aware validators, deterministic scaffolds, diagnostic rules, experience retrieval and review-first patching.

## Included capabilities

- **Persistent learning store:** SQLite records code/docs, source and license metadata, status, experiences, failed attempts and verified rules.
- **Knowledge retrieval:** token-overlap search ranks references and prior cases. Plans include relevant stored experiences and verified rules.
- **Bundled reference pack:** algorithms (binary search, merge sort, BFS/DFS, Dijkstra, complexity), design patterns (Strategy, Factory, Observer, Repository), testing/repair guidance and security practices. Seeded items are deliberately marked `unverified` until reviewed.
- **Project inspection and ingestion:** reads source/docs with limits; skips common generated folders and likely secrets; ingestion never executes imported files.
- **Deterministic construction:** Python CLI/package, Java console and static web scaffolds. Scaffolding refuses to overwrite a non-empty directory.
- **Unit-tested algorithm library:** binary search, stable merge sort, BFS and Dijkstra; export a selected implementation as a standalone Python file.
- **Static validation:** Python parsing/compilation and optional JavaScript, Java, C/C++ checks when tools are installed.
- **Diagnostics:** deterministic failure classification plus conservative repair guidance; suggestions do not claim root cause or automatically rewrite code.
- **Review-first patching:** produces a unified diff, requires explicit approval to apply, checks original file hash, creates a backup and rejects stale proposals.
- **Experience-based improvement:** failures and successful attempts are stored; prior cases can be retrieved for future planning. Promotion of rules requires original-test pass, regression-test pass and explicit review.
- **CLI, unit tests and smoke benchmarks.**

## Quick start (PowerShell, from the extracted EVA directory)

```powershell
python -m eva.coding.cli stats
python -m eva.coding.cli seed
python -m eva.coding.cli templates
python -m eva.coding.cli algorithms
python -m eva.coding.cli algorithm-export binary_search .\binary_search.py
python -m eva.coding.cli inspect .
python -m eva.coding.cli ingest . --max-files 500
python -m eva.coding.cli search "binary search sorted array" --language general
python -m eva.coding.cli plan . "debug Python undefined name error"
python -m eva.coding.cli diagnose "NameError: name 'total' is not defined"
```

Create a new project (target must not already contain files):

```powershell
python -m eva.coding.cli scaffold python-cli .\generated\sample_app --name "Sample App"
python -m eva.coding.cli scaffold java-console .\generated\java_demo --name "Java Demo"
python -m eva.coding.cli scaffold web-static .\generated\web_demo --name "Web Demo"
```

Generate a patch proposal from a replacement file, inspect the saved diff, then apply only after approval:

```powershell
python -m eva.coding.cli patch-propose .\src\module.py .\replacement.py .\patch-proposal.json
# Inspect patch-proposal.json and its diff first.
python -m eva.coding.cli patch-apply .\patch-proposal.json
```

Patching is guarded by an exact original-file hash and creates `filename.eva-backup`. The tool refuses to overwrite an existing backup. The CLI prompt is an explicit confirmation step, not an operating-system sandbox.

Run tests:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q eva tests
```

## Storage

By default the database is `%USERPROFILE%\.eva\coding\coding_knowledge.db` on Windows. Override with `--data-dir` or `EVA_CODING_DATA`. Knowledge files bundled with EVA live under `eva/coding/knowledge_pack/`; `seed` copies their content into SQLite as unverified knowledge. The database should be backed up before upgrades.

## Trust and safety model

- Imported or bundled knowledge starts unverified. Downloaded text is not automatically correct or licensed for reuse.
- Never execute code merely to ingest it.
- Generated source is checked before packaging: Python uses `compile()` without execution; JavaScript uses `node --check` when Node.js is installed; Java/C/C++ use compiler syntax checks when available; JSON and XML/SVG are parsed; CSS gets bounded delimiter, string, comment, and common declaration checks; HTML is passed through Python's HTML parser.
- When static validation finds a repairable failure, EVA tries deterministic fixes first, then allows at most one diagnostic-guided model repair, and validates again. Failed validation remains recorded in `.eva-project.json`; a ZIP with a failed status must not be treated as a successful build.
- HTML/CSS checks are intentionally conservative and are not full browser conformance validators. A static syntax pass does not prove correctness, runtime behavior, or security.
- Tests are executable programs and may have side effects. Review before running; confirmation alone is not a sandbox.
- Patch application requires explicit confirmation, checks the original hash, and makes a backup.
- No automatic arbitrary repair is enabled. Unknown or ambiguous failures are recorded/suggested for review rather than guessed at.

## Honest limitations

This is **not a trained coding model** and does not understand arbitrary natural-language requirements. It cannot reliably generate every kind of application or solve every novel bug. It creates useful starter projects, retrieves stored knowledge, validates syntax, proposes guarded patches and learns operationally through structured experience and verified rules. Broader generation requires more templates, language parsers, domain-specific algorithms, tests and benchmarks.

## v3: General multi-file blueprints and diagnostics

```powershell
python -m eva.coding.cli doctor
python -m eva.coding.cli blueprint-check .\my_blueprint.json
python -m eva.coding.cli blueprint-build .\my_blueprint.json .\generated\my_project --dry-run
python -m eva.coding.cli blueprint-build .\my_blueprint.json .\generated\my_project
```

A blueprint is explicit JSON containing project metadata and a list of `{ "path", "content" }` file entries. The builder can create multi-file layouts for any text-based language, but it does not translate plain-English requirements into arbitrary source code. It refuses path traversal, secret-like paths, case-colliding paths, and overwriting existing data. See `EVA_CODING_ENGINE_V3.md` and `EVA_CODING_STRESS_TESTS_V3.md`.
# Website generation design reference

Website requests are supported by `knowledge_pack/design/website_visual_system.md` and the static `website_reference/` prototype at the project root. The system captures the supplied school-admissions example's quality markers (confident hero, intentional artwork or verified photography, warm layered palette, useful sections, and clear action) and adapts them to each request. Retrieved reference text is explicitly treated as untrusted data. The prompt gives EVA a palette/token/spacing/type/layout/accessibility baseline while preserving safe explicit user preferences.

Remote image checks inspect direct image sources in HTML and CSS, allow only HTTPS direct `images.unsplash.com/photo-...` assets, and check at most five unique URLs with a four-second timeout each. The check reports HTTP/non-image failures and marks network exceptions as skipped; it does not claim skipped URLs are valid. A CSS fallback and runtime image error handler are still required because external assets can fail later.



### Python factorial validation

Simple Python factorial requests are normalized to a complete CLI program (input, validation, and output) and tested at runtime with inputs 0, 1, 5, and 20. This is a targeted safeguard, not a guarantee for arbitrary generated programs.
