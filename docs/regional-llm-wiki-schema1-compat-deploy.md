# Regional LLM Wiki schema-1 compatibility mount

The base production `docker/docker-compose.yaml` now directly includes the
Gateway-only, read-only mount for the existing `regional-llm-wiki` Agent on the
accepted `main` baseline. No separate Wiki overlay is retained. This change
does not modify DeerFlow backend or frontend source, the old `/mnt/knowledge`
mount, or the sandbox mounts.

## Inputs and gate

The independent Wiki compiler must first publish and validate a schema-1
projection at `runtime/agent-access-compat-v1/`. This is a separate snapshot;
do not mount the canonical schema-2 `runtime/agent-access-runtime/` here.
The previously exposed model API key must be rotated before any DeerFlow mount;
the old key is permitted only in the isolated shadow run. Do not place a key
or model configuration in Compose.
The compat root must contain `current -> releases/<release_id>` and the current
manifest must have `schema_version: 1` and the same `release_id` as its release
directory name. The full offline validator must pass; `get_llm_wiki_release_state`
alone checks only the manifest's basic compatibility.

As `wikiuser`, run the compiler's local export command after deploying the
merged LLM Wiki compiler. It validates the pinned schema-2 source and the
schema-1 output; an unchanged valid export is reported as `reused: true`:

```bash
python3 /home/wikiuser/regional_llm_wiki-shadow/compiler/llm_wiki.py \
  export-agent-compat-v1 --root /home/wikiuser/regional_llm_wiki-shadow
```

Record its `release_id`, `source_agent_release_id`, page/evidence counts and
the source commit from `current/agent-access-manifest.json`. A failed export
or a non-`ok` `agent_compat_v1` maintenance status blocks mounting. The
compiler root shown above is the current shadow location; if the administrator
uses a different isolated root, substitute that exact path consistently.

The exporter currently creates private `0700` directories and `0600` files.
A read-only bind mount does not grant Unix read permission. Before recreating
Gateway, the administrator must inspect its effective UID/GID and provide
read-only access to this compatibility root only (for example a dedicated
reader group or host ACL). Do not loosen the canonical schema-2 export or give
the sandbox access. Check that Gateway can traverse `current` and read both a
page and an evidence shard inside the container, including after the next
release. One-time/default ACLs alone are not a proven persistent solution:
the exporter explicitly sets new directories/files to `0700`/`0600`, which
can restrict ACL masks. Do not broaden Gateway privileges or make the export
world-readable merely to bypass a UID mismatch.

## Compose activation

Set `REGIONAL_LLM_WIKI_COMPAT_ROOT` in the administrator's existing deployment
`.env` (or persist it through the existing deployment environment) to the
absolute host path of `runtime/agent-access-compat-v1`, for example:

```dotenv
REGIONAL_LLM_WIKI_COMPAT_ROOT=/home/wikiuser/regional_llm_wiki-shadow/runtime/agent-access-compat-v1
```

It is a path, not a secret. Keep runtime `config.yaml` set to:

```yaml
regional_llm_wiki:
  enabled: true
  runtime_root: /mnt/regional-llm-wiki-runtime
```

Use the existing base Compose deployment command and keep any existing DooD
or other overlays, project name, environment file, config/data paths and
authentication variables unchanged. Do not append a separate Wiki overlay.
Check the merged Compose configuration without printing secrets, then recreate
**only** Gateway. Do not add this mount to the general sandbox or change the
existing production Markdown sync service. Given `existing_compose_args` from
the administrator's established deployment (including the base `-f` file):

```bash
docker compose "${existing_compose_args[@]}" config --quiet
docker compose "${existing_compose_args[@]}" up -d --no-deps --force-recreate gateway
```

This is a command pattern, not a replacement for preparing the existing
deployment environment. Do not run the whole stack's `down` for this cutover.

In the merged Compose model, confirm that Gateway still has the old
`/mnt/knowledge` mount and additionally has exactly one read-only
`/mnt/regional-llm-wiki-runtime` mount; no sandbox or provisioner service may
receive the new mount. Compose merges service volumes by container target, so
the distinct target is intentional. Run `docker compose ... config` with the
same ordered `-f` files that deployment will use, and inspect only the volume
targets/read-only flags rather than printing the full configuration into logs.

The current `scripts/deploy.sh` already loads the base Compose file, so the new
mount is included without script/source changes. Remove any obsolete Wiki
`-f` argument from administrator wrappers. The required path variable applies
to every base Compose invocation, even if `regional_llm_wiki.enabled` is false;
an unset/empty value fails interpolation. The mount uses
`create_host_path: false`, so a missing compatibility export is an error rather
than an empty host directory. A directory existing is not proof of a validated
release: complete the release and permission gates before deployment.

## Acceptance and rollback

1. In Gateway, `current/agent-access-manifest.json` is readable and matches
   the expected immutable schema-1 release ID and source commit.
2. `get_llm_wiki_release_state`, `search_llm_wiki`, `read_llm_wiki_page`, and
   `read_llm_wiki_evidence` return `ok`. Include a long document's last shard.
3. The dedicated Agent exposes only its approved tools; an old Agent still
   behaves as before, including one regression question against its existing
   `/mnt/knowledge` source. The sandbox cannot read the compat mount.
4. After a publish switch, an in-flight answer can still read the prior
   immutable release. Retain at least one previous release.

If any pre-deployment gate fails, do not recreate Gateway or enable the new
Agent; leave the existing running stack in place. For a failed cutover, restore
the saved previous base Compose file (without the new Wiki mount), restore any
changed runtime config, and recreate Gateway only with the previous deployment
environment. Removing an overlay is no longer the rollback mechanism. Keep
the required path variable available until the previous base Compose file has
been restored. The canonical Wiki and old agents stay untouched. If the
compiler's compat export fails, its `current` remains at the previous validated
release and maintenance status records the failure.

The mount is isolated by service and the Agent's tool allowlist, not by a
separate Gateway process for each Agent. Gateway recreation may briefly
interrupt in-flight requests; schedule the cutover accordingly.
