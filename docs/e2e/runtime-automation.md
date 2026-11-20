# LB2142 runtime automation — working notes

Living notes for the LB2142 ("Selling with AI Quickstarts") solve/validate
automation, and the path from "seats provision" to a high-quality end-to-end
check. Update this file as the work moves; it is the context a new session
needs.

Started 2026-11-20, after a 3-seat workshop (`rh1-2027.lb2142-ai-quickstarts-tenant.test-cpmxz`)
failed every seat at the Showroom stage.

## Why this exists — the failure that started it

Every showroom pod sat at 3/4 with `runner` in `CrashLoopBackOff`. The runner
answered its own health endpoint with:

```
503 {"ansible":"ok","api":"ok","runtime_dir":"missing: /showroom/repo/runtime-automation","status":"degraded"}
```

`rh1-2027/account.yaml` enables runtime automation for every Summit lab, so the
runner container is always present. The chart probes `/health` for **liveness**,
so after three failures the container is killed, restarts, and the pod never
becomes Ready.

The immediate unblock is an image pin in the catalog item
(`ocp4_workload_showroom_runtime_automation_image: quay.io/rhpds/zerotouch-automation:v1.3.3`,
agnosticv PR #29172). The durable fix is this directory.

## Verified facts about the runner

Established by reading `quay.io/rhpds/zt-runner:v2.5.0` directly — do not
re-derive:

- `/health` is 503 unless `<repo>/runtime-automation` exists. The directory is
  the only thing it checks besides `ansible-playbook` being runnable.
- Path resolution: `/app/runtime-automation` if present, otherwise
  `/showroom/repo/runtime-automation`. Both are used in the field; the chart
  version in play here only mounts the full repo.
- `/config` scans `runtime-automation/module-*/` for `*.yml`. Module names have
  to start with `module-`; stage names are the filenames.
- Endpoints are `/stream/{setup,solve,validate}/<module>`, SSE, proxied by
  nginx with the `/stream` prefix stripped.
- `validation_check` and `validation_fail` action plugins ship in the image.
- Ansible core 2.16.18, kubernetes.core 2.4.2, community.general 8.6.8.
- Extravars are assembled from `/user_data/user_data.yml` (mounted ConfigMap)
  plus a kubeconfig Secret lookup: `k8s_kubeconfig`, `guid`, `student_user`,
  `student_ns`, `student_ns2`, and bastion connection details.

## Prerequisites for a real E2E

Each of these can independently make the lab look broken:

1. **Content ref contains `runtime-automation/`** — this directory, on a tag the
   catalog item references. The current test ref (`rh1-2027-lb2142-test-0.0.1`)
   predates both `runtime-automation/` and `qa-automation/`.
2. **Showroom collection >= v1.7.2, ideally v1.8.0** — the runtime automation
   *identity* (ServiceAccount plus `runtime-automation-kubeconfig` Secret) is
   created by the role from v1.7.2. The tenant `test` stage currently pins
   **v1.6.8**, which has neither the identity nor the image default, so
   playbooks get no `k8s_kubeconfig` and can only see the showroom namespace.
   `common.yaml` already pins v1.8.0 — the mismatch is test-only.
3. **Cluster-wide access for the identity** — seats run on a dedicated workshop
   cluster, so `ocp4_workload_showroom_runtime_automation_cluster_admin: true`
   is the right setting. In tenant (shared-cluster) mode, grant `admin` per
   namespace using `..._namespace_suffixes` instead.
4. **Runner image compatible with the content** — either a tree the deployed
   image accepts, or the image pinned (below).
5. **A content tag workflow** — the content ref is per stage; a new tag is
   needed for every change to these playbooks.

## Decisions so far

- **Reuse the standalone-playbook layout** (`module-NN/{setup,solve,validate}.yml`),
  not the legacy `main.yml` + shell-script layout. It matches what
  `zt-runner` v2.5.0 scans, keeps the Ansible logic in Ansible rather than
  shell, and makes `validation_check` output the natural result channel.
- **Module directory names must equal the lab module numbers** because the
  content calls `/stream/validate/module-02`.
- **Omit `solve.yml` where there is nothing to apply.** Solve exists to make a
  demo work when the learner has skipped ahead; inventing one where the module
  is conversational would be theatre.
- **Validate is read-only; setup is idempotent.** Setup runs on page load.
- **Fail with the component, the observed state, and the likely cause** — the
  pattern is the checklist `error_msg` used across these playbooks.
- **Never gate on a transient condition.** The standing example is the
  `saw_prepare` early-fail gate, which aborted a whole cluster on an Argo CD
  `InvalidSpecError` that would have cleared itself (red-hat-1-quickstarts
  issue #26).
- **Keep the readiness health check and the E2E separate.** `qa-automation/healthcheck.yml`
  is the "can this seat be signed off?" gate; `runtime-automation/` is the
  learner-facing solve/validate flow. They share helpers, not conclusions.

## Roadmap

- [x] Diagnose the cpmxz seat failures.
- [x] Stopgap: pin the runner image (agnosticv #29172).
- [x] Scaffold `runtime-automation/` with module 2, 3, 4 and 7 gates.
- [ ] Tag a new content ref and point the test stage at it.
- [ ] Bump the tenant `test` stage showroom pin to v1.8.0 so the identity
      exists (without it every `k8s_info` call only sees the showroom namespace).
- [ ] Verify `k8s_kubeconfig` reaches a playbook in a real seat.
- [ ] Run each gate against a live seat; fix the TODOs marked in the playbooks.
- [ ] Re-add `qa-automation/healthcheck.yml` to the same tag and confirm
      `/stream/qa/healthcheck`.
- [ ] Drop the image pin once the content tree is proven with the rolling image.

## Open questions

- **llama-stack endpoint** — the service name and port (`llamastack.<ns>.svc:8321`)
  are taken from the tenant chart, not yet confirmed from inside a runner pod.
  The same endpoint is used by the readiness health check.
- **IT Self Service "processed" signal** — module 4 currently asserts the seeded
  mailbox rather than a processed ticket. What is the stable observable?
- **SAW assistant API** — module 7 proves the VM runs, not that the assistant
  answers. Endpoint, auth and governance intercept need settling before the
  gate can claim more.
- **PPE module (5/6)** — PPE has a Showroom tab but no module in `nav.adoc`, so
  there is no natural runtime gate yet. Decide whether it gets one.
- **Solve stages** — once it is known which demos benefit from a "do it for me"
  path, add `solve.yml` where it earns its place.
- **Do the content playbooks rely on route URLs?** They are stable and
  human-meaningful; service DNS is faster but couples to chart internals. Route
  URLs are used for now, matching the readiness health check.
