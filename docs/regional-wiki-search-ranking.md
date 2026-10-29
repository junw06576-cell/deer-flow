# Regional Wiki retrieval and document-state repair

The deployed 2026-10-29 schema-1 Reader returned a valid release, page and
evidence, but broad family-doctor queries omitted the signing solution from
top five. Raw occurrences in title/summary/keywords rewarded repetition and
many table-group pages. Separately, directory placeholders supported normal
source/entity pages with no machine-readable content state. Both are
reproduced contributors; the screenshot conversation's tool trace is absent,
so they are not established as its only cause.

## Implementation

`backend/integrations/regional_wiki_search.py` is business code loaded by the
existing resolver. It uses topic coverage in separate fields, bounded field
presence, explicit abbreviation equivalences, document intent, and source
diversity. It never selects a fixed document ID. Only the search binding
changes; harness, middleware, frontend and the existing reader are unchanged.

New compiler exports carry `content_state_version=document-content-v2`,
document/page `content_state` and page `fact_eligible`. Whole-source identity
survives compatibility fragmentation. Facts scope excludes pure placeholder
or empty dependencies; catalog scope includes them with explicit state.
Mixed dependencies remain eligible, but still require page/evidence reading.
Legacy manifests are supported: small whole-source files are hash-verified
before template classification; a short fragment or summary is never treated
as proof that its original document is a placeholder. Unknown states remain
searchable and are not a semantic quality verdict.

The v2 classifier preserves information-bearing headings and mixed
template/body lines. It removes only an initial plain H1 document title and
decorations and recognizes complete known boilerplate lines. V1 negative
flags are not reused: small whole-source evidence is hash-verified and
reclassified; large/sharded sources remain unknown and retrievable. Pinned
manifests are never edited. Both repositories version an identical synthetic
classification fixture, including heading facts, inline mixed content and
legacy-policy migration regressions.

Product names and individual document states are separate. For example, an
introductory document may be a placeholder while its solution is substantive.
No source files or product directories are removed. Factual queries require
real topic matches; a generic word such as "方案" must not overwhelm the topic.

## Review and deployment

1. Review this branch together with the compiler document-state branch. The
   compiler branch retains the deployed maintenance fix as a prerequisite;
   do not replace that deployed baseline with an older main checkout.
2. Stage the compiler and converted snapshot in an unmounted local/isolated
   root. Validate hashes, schema, references, content states and source commit.
   Converter `compat-v1.3-content-state` can annotate an existing canonical
   export without model calls. A later ordinary compile emits no-fact
   `not_applicable` placeholder sources and drops solely placeholder-derived
   entities/concepts; mixed aggregates retain substantive support.
   The compiler's first upgraded semantic regression must finish (with durable
   budget-resume checkpoints) before the maintenance publication gate clears;
   a model-free cache replay does not meet that semantic gate.
3. Verify `integrations.regional_wiki_search` imports inside the actual Gateway
   image/mounted checkout. Change only the search tool's `use` to
   `integrations.regional_wiki_search:search_llm_wiki`; retain the existing
   other three tool bindings, tool group, mount and access controls. Do not
   overwrite an administrator's uncommitted checkout. YAML alone does not
   establish container code availability or enforce an Agent allowlist.
4. If the independent MCP service is used, ship its updated server and read-only
   integration mount too. It is a separate consumer; a Gateway config edit
   does not update it. Do not register two tools with the same name.
5. After approval, canary actual Gateway/MCP searches, then read the returned
   solution and evidence in a new Agent session. Verify family-doctor and
   maternal/child-health solutions alongside placeholder introductions,
   parameter/manual intent, domain filtering, absent topics and old-Agent
   regressions. Confirm effective UID read access to both current/new releases.

Retain the old bindings, Agent instructions, module version and immutable
compat pointer for rollback. Restore these together if the canary fails.
Do not update production pointers or restart containers as part of local tests.

## Verification boundary

Local regression uses the pinned 198-document release for source commit
`fe1dc8ed509f75530fcc54ead03b4f021d0cd25d`. Algorithm replay against the old
5,708-page compatibility export identifies 37 placeholder documents and
76 placeholder-only pages. The six family-doctor queries all put the signing
solution first; maternal/child-health solution queries also return a valid
solution first. Evidence/page IDs remain readable by the old Reader.

This is not container packaging, UID permission, Agent reasoning or screenshot
conversation acceptance. The current SSH user has no Docker socket access and
passwordless sudo is unavailable. Full backend-suite collection also requires
a test-only AppConfig; the default example first stopped collection because
`$OPENAI_API_KEY` was absent. A second run with a credential-free offline
config reached 568 passed / 3 skipped and two unrelated failures (AIO warm-pool
eviction and Browserless DNS). Both failures reproduce on pristine `main`.
The broad run was stopped while Readability tried a blocked `npm install`;
the complete backend suite is not green and is not claimed as passed.
After the three review fixes, another full-suite attempt with `--maxfail=2`
stopped at the same two failures (564 passed / 3 skipped); this remains a
baseline/environment limitation rather than a full-suite success.
The dedicated retrieval/Reader/MCP tests and compiler tests are reported
separately. No real key belongs in test config, reports or Git.
