# Why we need a proper test harness

1. Cluster hardware can be different, workloads can be different, personas can vary, testing requirements can vary. There are too many variations and unknowns. And even when the unknowns collapse into one single set of knowns, they can change due to drift or changing requirements or someone modifying something. We need to be able to quickly and safely adapt to each set of knowns as they become available without having to do massive reworks.
2. A test is not just running a pod against a set of parameters. It needs a purpose, an architecture defining which pods deploy in what order with what dependencies, infrastructure requirements (images, GPUs, services, readiness probes), pass/fail assertions, parameter sweeps, prerequisites, failure policies, and who can run it with what permissions.
3. Running a single test involves delivering source to the cluster, compiling binaries, deploying infrastructure, waiting for readiness, executing test pods, releasing resources between steps, tearing down, enforcing failure policies, aggregating results, and cleaning up: all of which must happen in the right order, across the right nodes, in the right namespaces.
4. Results need to be aggregated. They need to cover pass/fail cases, as well as quantitative cases where results are context driven, e.g. how many tokens/s is acceptable, what is the acceptable gap between back of the envelope math and actual measurements. Results need to outlive the session, be readable without the person who ran them, and be comparable across runs and environments.
5. Minimize the engineer hours spent setting up and running tests, or helping users run their workloads. A test harness that can double as a workload orchestrator means we can reduce our effort by providing recipes for what works and enabling users to tweak, customize, and modify them.
6. AI friendly: guardrails, validation, assertions, pydantic models, and detailed documentation so users can safely use AI to generate tests and suites.
# Design decisions

1. **Separate what varies.** The things that change between deployments are different from the things that change between tests. Cluster details like node names, GPU counts, and storage live in one file. Test logic lives in another. Which tests to run and in what order lives in a third. When you move to a new cluster, you update one file. When you add a new test, you don't touch the cluster config.

2. **One list, multiple outputs.** The generator computes one ordered list of steps from these inputs. From that same list, it produces manual scripts and Tekton pipelines independently. If we need to support another execution backend later, we write a new writer. Step computation doesn't change.

3. **Three test scopes.** Tests can run per-node in parallel, across specific sets of nodes, or at the project level with no node affinity. All three can be mixed in a single test suite. A hardware validation test can run on every node, followed by a network bandwidth test across node pairs, followed by a namespace-level RBAC check, all in one pipeline.

4. **Suites and libraries.** Test suites and test libraries are separate. A platform team can maintain a shared library of validated tests. An admin can have a private library with privileged diagnostics. Each team composes their own suite from whatever libraries they need, with their own scopes, failure policies, and RBAC.

5. **Override without duplication.** A suite entry can override specific fields in a test definition without duplicating the whole thing. Same test, different model, different GPU count, different timeout. The override is deep-merged so you only specify what changes.

6. **Fail at generation.** If a test requests 4 GPUs on a 2-GPU node, or uses a scope the test definition doesn't support, or produces a malformed manifest, that fails at generation time. Nothing gets applied to the cluster until the entire plan validates.

7. **Proper test framework.** Tests are written in Ginkgo, not shell scripts checking exit codes. You get structured specs, labeled test cases, built-in JUnit XML output, proper assertions, and the ability to report quantitative metrics alongside pass/fail results. The source compiles into a single binary with no runtime dependencies on the test pod.

8. **Per-test failure policies.** Each test declares what should happen when something fails: continue running, skip the rest of this test, or abort the whole suite. Guard tasks fan in after every test and enforce the policy. Cleanup and teardown always run regardless.

9. **Deploy once, sweep many.** GPU-backed servers deploy once and stay up through the entire parameter sweep. When an ephemeral test pod finishes, its resources are released immediately so the next sweep entry can use those GPUs. You don't redeploy the server between benchmark configurations.

10. **Any K8s resource.** DAG steps can deploy any Kubernetes resource, not just pods. InferencePools, ConfigMaps, CRDs. The harness doesn't need to know about them ahead of time.

11. **Namespace isolation.** Cross-namespace tests get independent infrastructure in each namespace. Kubernetes isolation boundaries stay intact. Teardown is split per namespace so cleanup selectors don't cross boundaries.

12. **Editable step list.** The computed step list is serialized to steps.json. You can inspect it, edit it, add or remove steps, and re-run the writers without recomputing from definitions.

13. **Tests as recipes.** The same YAML that defines a test also describes how to deploy the workload. The vLLM, KServe, llm-d, and Jupyter test definitions are working deployment recipes that someone can use as a starting point for their own workloads.

14. **Unified naming.** One naming convention (test ID, test name, node or set, DAG step) serves as pod name, PVC directory, script filename, and Tekton task name. Results are traceable end to end and collisions across nodes, sets, and sweep entries are structurally impossible.

15. **One binary per test.** The same compiled binary handles all parameter sweep entries and all nodes. Different runtime config, same test logic. No redundant compilation.

16. **Separate models storage.** Model weights live on a dedicated read-only volume, separate from per-run results. Shared across runs, so servers load from a pre-populated cache instead of downloading at runtime.

17. **All-or-nothing generation.** If anything fails during generation, you get nothing. The generator never produces partial output.

18. **Add capabilities when needed.** The harness didn't try to support every Kubernetes feature upfront. Sidecars were added when the llm-d test needed a routing proxy. Resource steps were added when the KServe test needed to deploy an InferenceService CRD. valueFrom env vars were added when pods needed to reference their own IP. Peer namespaces were added when multi-tenancy testing needed cross-namespace workloads. Each capability was added because a real test required it, not because it might be useful someday.

# Limitations

1. All test source is delivered via a single ConfigMap, which has a 1MB Kubernetes limit. The current test library fits comfortably, but a significantly larger one would hit it. The mitigation is splitting across multiple suites or revisiting the delivery mechanism.

2. Resource names are built by concatenating test ID, test name, node or set index, and DAG step. Node names are capped at 16 characters, but the full name can still exceed the 63-character Kubernetes limit with long test or step names.

3. The builder pod has a fixed name, so only one pipeline can run at a time in a given namespace.

4. Parameter sweep entries run sequentially, not in parallel. Each entry gets its own pod against the same persistent server.

5. Cluster-scoped tests with permutation placement can produce factorial numbers of node sets. 10 nodes with setSize 3 produces 720 permutations. setCutoff and combination mode exist to bound this.

6. Resource steps deploy arbitrary CRDs but can't inject nodeSelector because the path to nodeSelector differs by resource kind. The KServe test is restricted to project scope because of this.

7. Tekton execution is generated but not yet tested end to end.
